"""Engines tab backend, mounted by the dashboard at /api/plugins/hermes_control/.

A thin translation to HTTP. Engine status is the same engine_state() the
dispatcher routes on, so this page and a dispatch summary never disagree; the
codex sign-in lives in hermes_control/device_login.py.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool


def _load_plugin_package() -> None:
    """Make the plugin package importable as ``hermes_control``.

    The dashboard imports this file by path, as a lone module, so a relative
    import cannot reach the package above it. Load that package once, from the
    directory above this one, under the name the test suite imports it by.
    """
    if "hermes_control" in sys.modules:
        return
    package_dir = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_control", package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["hermes_control"] = package
    try:
        spec.loader.exec_module(package)
    except BaseException:
        # A half-initialised package left in sys.modules would poison every retry.
        sys.modules.pop("hermes_control", None)
        raise


_load_plugin_package()

from hermes_control import (  # noqa: E402 - needs the package loaded above
    device_login, diagram, limits, model_catalog, model_policy, pauses, planner_status, review_diff, review_notes, runs)
from hermes_control.dispatch import NotReady, resume_blocker, resume_paused  # noqa: E402
from hermes_control.engines.codex import (  # noqa: E402
    REASONING_SUMMARY_CHOICES, read_reasoning_summary, save_reasoning_summary)
from hermes_control.engines.registry import ENGINE_NAMES, engine_state, get_adapter  # noqa: E402
from hermes_control.engines.settings import save_paused  # noqa: E402
from hermes_control.verify import find_sandbox  # noqa: E402


def _web_origin(value: str, *, public_url: bool = False) -> tuple[str, str, int]:
    """Parse a web origin without silently repairing malformed header values."""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid whitespace in origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "\\" in parsed.netloc or "%" in parsed.netloc
            or parsed.netloc.endswith(":")):
        raise ValueError("invalid web origin")
    if not public_url and (parsed.path or "?" in value or "#" in value):
        raise ValueError("origin must contain only scheme and authority")
    port = parsed.port  # Also rejects invalid and out-of-range ports.
    return parsed.scheme, parsed.hostname.lower(), port if port is not None else (
        80 if parsed.scheme == "http" else 443)


def _check_origin(request: Request) -> None:
    """Limit browser access to this dashboard; desktop origins still use its token."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    try:
        scheme = urlsplit(origin).scheme
        if scheme not in {"http", "https"}:
            if scheme or origin == "null":
                return
            raise ValueError("missing origin scheme")
        candidate = _web_origin(origin)
    except ValueError:
        raise HTTPException(status_code=403, detail="cross-origin request refused") from None

    try:
        if candidate == _web_origin(f"{request.url.scheme}://{request.headers.get('host', '')}"):
            return
    except ValueError:
        pass

    try:
        # Hermes may be absent or unable to resolve its public URL at request time.
        from hermes_cli.dashboard_auth.prefix import resolve_public_url

        public_url = resolve_public_url()
        if isinstance(public_url, str) and public_url and candidate == _web_origin(
                public_url, public_url=True):
            return
    except Exception:
        pass
    raise HTTPException(status_code=403, detail="cross-origin request refused")


router = APIRouter(dependencies=[Depends(_check_origin)])


@router.get("/diagrams")
def list_diagrams() -> dict[str, Any]:
    fields = ("id", "title", "repo", "commit", "kinds", "status", "reason", "created_at",
              "updated_at", "counts", "spec_author", "url")
    head_cache = {}
    return {"diagrams": [{**{key: record.get(key) for key in fields},
                          "stale": diagram.is_stale(record, head_cache=head_cache)}
                         for record in diagram.list_diagrams()]}


@router.get("/diagrams/{diagram_id}")
def get_diagram(diagram_id: str) -> dict[str, Any]:
    record = diagram.load_diagram(diagram_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such diagram")
    return {"diagram": record, "stale": diagram.is_stale(record)}

# Engines the page signs in itself. claude is absent on purpose: the dashboard
# never drives an Anthropic login (docs/hermes-control/engines-dashboard.md §3.3).
_SIGN_IN_IN_PAGE = frozenset({"codex"})
_GONE = "no such sign-in (the dashboard may have restarted); start again"


def _engine_row(name: str) -> dict[str, Any]:
    adapter = get_adapter(name)
    info, problem = engine_state(adapter)
    row = {"name": name, "installed": info is not None,
           "version": info.version if info else "", "path": info.path if info else "",
           "signed_in": info is not None and problem is None, "problem": problem,
           "sign_in_command": adapter.sign_in_command,
           "sign_in_in_page": name in _SIGN_IN_IN_PAGE}
    if name == "codex":
        row["reasoning_summary"] = read_reasoning_summary()
    return row


@router.get("/engines")
def engines() -> dict[str, Any]:
    sandbox = find_sandbox()
    return {"engines": [_engine_row(name) for name in ENGINE_NAMES],
            "sandbox": {"available": sandbox is not None, "path": sandbox or ""}}


def _reasoning_summary_response(value: str) -> dict[str, Any]:
    return {"reasoning_summary": value, "choices": list(REASONING_SUMMARY_CHOICES)}


@router.get("/reasoning-summary")
def get_reasoning_summary() -> dict[str, Any]:
    return _reasoning_summary_response(read_reasoning_summary())


@router.put("/reasoning-summary")
def put_reasoning_summary(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        value = save_reasoning_summary(body.get("reasoning_summary"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return _reasoning_summary_response(value)


@router.post("/engines/codex/login")
def start_codex_login() -> dict[str, Any]:
    try:
        return device_login.start_login(get_adapter("codex"))
    except device_login.LoginRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/engines/codex/login/{session_id}")
def codex_login_status(session_id: str) -> dict[str, Any]:
    try:
        return device_login.login_status(session_id, get_adapter("codex"))
    except KeyError:
        raise HTTPException(status_code=404, detail=_GONE) from None


@router.delete("/engines/codex/login/{session_id}")
def cancel_codex_login(session_id: str) -> dict[str, Any]:
    try:
        return device_login.cancel_login(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=_GONE) from None


@router.get("/models")
def models() -> dict[str, Any]:
    """Current model settings in their saved shape, plus each engine's effort levels.
    Broken settings are reported as ``problem`` rather than raised, so the page can
    show why and replace them."""
    try:
        settings, problem = model_policy.to_settings(model_policy.load_policy()), None
    except ValueError as exc:
        settings, problem = model_policy.to_settings({}), str(exc)
    return {"settings": settings, "problem": problem,
            "efforts": {name: list(get_adapter(name).efforts) for name in ENGINE_NAMES}}


@router.put("/models")
def update_models(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Validate and save, through Hermes's own plugin-settings writer."""
    if "settings" not in body:
        raise HTTPException(status_code=400, detail="settings is required")
    try:
        settings = model_policy.save_settings(body["settings"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:  # managed install or administrator-managed key
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return {"settings": settings,
            "note": "Tasks are checked against these at once; the planner sees the new list "
                    "in its next Hermes session."}


@router.get("/models/available")
def available_models(refresh: bool = False) -> dict[str, Any]:
    """What each engine's account can use, for the picker; cached ten minutes unless
    *refresh*. A list is guidance only: saving never requires a listed name."""
    return model_catalog.available(refresh=refresh)


@router.get("/planner")
def planner() -> dict[str, Any]:
    """Configured planner labels only; sign-in state is not checked."""
    return planner_status.status()


# --- usage limits and paused tasks (docs/limits-and-resume.md §6) -------------------

logger = logging.getLogger(__name__)


@router.get("/limits")
def get_limits(refresh: bool = False) -> dict[str, Any]:
    """Each engine's usage windows, its cap, and whether it is holding work back."""
    try:
        caps, problem = limits.read_caps(), None
    except ValueError as exc:
        caps, problem = limits.parse_caps(None), str(exc)
    return {"engines": {name: limits.describe(name, caps=caps, refresh=refresh)
                        for name in ENGINE_NAMES},
            "problem": problem}


@router.get("/status")
def get_status() -> dict[str, Any]:
    """Compact desktop status, using cached usage and no account details."""
    try:
        caps, problem = limits.read_caps(), None
    except ValueError as exc:
        caps, problem = limits.parse_caps(None), str(exc)
    except Exception as exc:  # noqa: BLE001 - keep desktop polling available
        problem = f"could not read the usage caps: {type(exc).__name__}"
        logger.warning(problem, exc_info=True)
        caps = limits.parse_caps(None)

    engines = []
    for name in ENGINE_NAMES:
        reading = limits.describe(name, caps=caps, refresh=False)
        window = max((w for w in reading["windows"] if w["used_percent"] is not None),
                     key=lambda w: w["used_percent"], default=None)
        percent = window["used_percent"] if window else None
        resets_at = window["resets_at"] if window else None
        cap = reading["cap"]
        if reading["paused"]:
            level = "paused"
        elif reading["blocked"] or reading["held_back"] is not None:
            level = "blocked"
            for until in (reading["blocked_until"], reading["held_until"]):
                if until is not None:
                    resets_at = until
                    break
        elif percent is not None and percent >= cap:
            level = "over"
        elif percent is not None and percent >= 0.75 * cap:
            level = "near"
        else:
            level = "ok"
        engines.append({"name": name, "percent": percent,
                        "window": window["label"] if window else None,
                        "cap": cap, "report_only": reading["report_only"],
                        "level": level, "resets_at": resets_at})

    try:
        recent = runs.list_runs(limit=5)
    except Exception as exc:  # noqa: BLE001 - keep desktop polling available
        run_problem = f"could not read the run logs: {type(exc).__name__}"
        logger.warning(run_problem, exc_info=True)
        problem = f"{problem}; {run_problem}" if problem is not None else run_problem
        recent = []
    last_run = None
    proof = None
    if recent:
        newest = recent[0]
        last_run = {"run_id": newest["run_id"], "state": newest["state"],
                    "updated_at": newest["updated_at"],
                    "task_ids": [task["id"] for task in newest["tasks"]]}
        try:
            view = runs.run_view(newest["run_id"])
            if view is not None:
                written = [task for task in view["tasks"] if task["policy"] == "write"]
                proof = {"run_id": newest["run_id"], "written": len(written),
                         "proven": sum(task["status"] == "done" for task in written),
                         "red_green": sum((task["summary"] or {}).get("verdict")
                                          == "red-green-proven" for task in written),
                         "not_proven": [task["id"] for task in written
                                        if task["status"] in {"unproven", "failed", "stopped"}]}
        except Exception as exc:  # noqa: BLE001 - keep desktop polling available
            run_problem = f"could not read the last run: {type(exc).__name__}"
            logger.warning(run_problem, exc_info=True)
            problem = f"{problem}; {run_problem}" if problem is not None else run_problem

    chip = {"kind": "idle", "engine": None, "percent": None, "window": None,
            "resets_at": None, "run_id": None, "task_id": None, "started_at": None}
    running = next((run for run in recent if run["state"] == "running"), None)
    eligible = [engine for engine in engines if not engine["report_only"]]
    blocked = next((engine for engine in eligible if engine["level"] == "blocked"), None)
    near = max((engine for engine in eligible if engine["level"] in {"over", "near"}),
               key=lambda engine: engine["percent"], default=None)
    fullest = max((engine for engine in eligible if engine["percent"] is not None),
                  key=lambda engine: engine["percent"], default=None)
    if running is not None:
        task = next((task for task in running["tasks"] if task["status"] == "running"),
                    next(iter(running["tasks"]), None))
        chip.update(kind="running", engine=task["engine"] if task else None,
                    run_id=running["run_id"], task_id=task["id"] if task else None,
                    started_at=running["started_at"])
    elif blocked is not None:
        chip.update(kind="blocked", engine=blocked["name"], resets_at=blocked["resets_at"])
    elif proof is not None and proof["not_proven"] and last_run["state"] != "running":
        chip.update(kind="unproven", run_id=proof["run_id"], task_id=proof["not_proven"][0])
    elif near is not None:
        chip.update(kind="near", engine=near["name"], percent=near["percent"],
                    window=near["window"], resets_at=near["resets_at"])
    elif fullest is not None:
        chip.update(engine=fullest["name"], percent=fullest["percent"])
    return {"chip": chip, "engines": engines, "last_run": last_run,
            "proof": proof, "problem": problem}


@router.put("/limits")
def put_limits(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"limits": limits.save_caps(body.get("limits"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.put("/engines/{name}/paused")
def put_engine_paused(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        if name not in ENGINE_NAMES:
            raise ValueError(f"unknown or disabled engine {name!r}")
        paused = body.get("paused")
        save_paused(name, paused)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return {"engine": name, "paused": paused}


# Resumes started from this dashboard: id -> {state, task_id, entry, error}. In
# memory only: the paused record on disk stays until a resume settles it.
_resumes: dict[str, dict[str, Any]] = {}
_resumes_lock = threading.Lock()
_KEEP_FINISHED = 20


def _paused_row(record: pauses.Pause) -> dict[str, Any]:
    reason, engine = resume_blocker(record)
    return {"id": record.id, "run_id": record.run_id, "task_id": record.task["id"],
            "goal": record.task["goal"][:400], "engine": record.engine,
            "reason": record.reason, "resets_at": record.resets_at,
            "paused_at": record.paused_at, "worktree": record.worktree_path() and
            str(record.worktree_path()), "resumes_session": bool(record.session_id),
            "ready": engine is not None, "resume_on": engine, "not_ready": reason or None}


@router.get("/paused")
def list_paused() -> dict[str, Any]:
    with _resumes_lock:
        resumes = [{"id": key, **value} for key, value in _resumes.items()]
    rows = [{**_paused_row(record), "state": next((r["state"] for r in resumes
                                                   if r["id"] == record.id), None)}
            for record in pauses.all_paused()]
    return {"paused": rows, "resumes": sorted(resumes, key=lambda r: -r["started_at"])}


def _run_resume(record_id: str) -> None:
    try:
        entry = resume_paused(record_id)
        outcome = {"state": "paused" if entry.get("paused") else "done", "entry": entry}
    except NotReady as exc:
        outcome = {"state": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported on the page, not lost in a thread
        logger.exception("hermes-control: resuming %s failed", record_id)
        outcome = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    with _resumes_lock:
        _resumes[record_id].update(outcome, finished_at=time.time())
        finished = sorted((k for k, v in _resumes.items() if v["state"] != "running"),
                          key=lambda k: _resumes[k]["started_at"])
        for key in finished[:-_KEEP_FINISHED]:
            _resumes.pop(key, None)


@router.post("/paused/{record_id}/resume")
def resume(record_id: str) -> JSONResponse:
    """Start a paused task again, in the background; GET /paused reports how it went."""
    record = pauses.load(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such paused task")
    reason, engine = resume_blocker(record)
    if engine is None:
        raise HTTPException(status_code=409, detail=reason)
    with _resumes_lock:
        if _resumes.get(record_id, {}).get("state") == "running":
            raise HTTPException(status_code=409, detail="this task is already resuming")
        _resumes[record_id] = {"state": "running", "task_id": record.task["id"],
                               "engine": engine, "started_at": time.time()}
    threading.Thread(target=_run_resume, args=(record_id,), daemon=True,
                     name=f"hermes-control-resume-{record_id}").start()
    return JSONResponse(status_code=202, content={"state": "running", "engine": engine})


@router.delete("/paused/{record_id}")
def discard(record_id: str) -> dict[str, Any]:
    """Forget a paused task. Its worktree stays for the user to inspect or remove."""
    with _resumes_lock:
        if _resumes.get(record_id, {}).get("state") == "running":
            raise HTTPException(status_code=409, detail="this task is resuming right now")
    if not pauses.remove(record_id):
        raise HTTPException(status_code=404, detail="no such paused task")
    return {"discarded": record_id}


# --- the Runs view -----------------------------------------------------------------

@router.get("/runs")
def list_runs(limit: int = 30) -> dict[str, Any]:
    """The newest runs first, with each task's status."""
    return {"runs": runs.list_runs(limit=max(1, min(limit, 100)))}


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    """One run, whole: the plan, each task's attempts, and where each task ended up."""
    try:
        from hermes_control import recover

        recover.reconcile(run_id)  # a run whose Hermes process died becomes paused; never raises
    except Exception:  # noqa: BLE001 - the page must load even if recovery cannot
        logger.warning("hermes-control: could not reconcile run %s", run_id, exc_info=True)
    view = runs.run_view(run_id)
    if view is None:
        raise HTTPException(status_code=404, detail="no such run")
    return view


@router.get("/runs/{run_id}/stream")
def run_stream(run_id: str) -> StreamingResponse:
    if runs.run_view(run_id) is None:
        raise HTTPException(status_code=404, detail="no such run")
    return StreamingResponse(runs.stream_run(run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/tasks/{task_id}/trace")
def run_task_trace(run_id: str, task_id: str, since: int | None = None) -> dict[str, Any]:
    view = runs.run_view(run_id)
    if view is None:
        raise HTTPException(status_code=404, detail="no such run")
    if task_id not in {task["id"] for task in view["tasks"]}:
        raise HTTPException(status_code=404, detail="no such task")
    entries = runs.task_trace(run_id, task_id, since_seq=since) or []
    from hermes_control.runlog import RunLog
    last_seq = max((event.seq for event in RunLog(run_id).read_all()
                    if event.task_id == task_id or (event.kind == "planner" and not event.task_id)),
                   default=0)
    return {"entries": entries, "last_seq": last_seq}


@router.get("/runs/{run_id}/tasks/{task_id}/diff")
def run_task_diff(run_id: str, task_id: str) -> dict[str, Any]:
    result = review_diff.build(run_id, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="no such run or task")
    return result


@router.get("/runs/{run_id}/tasks/{task_id}/review")
def run_task_review(run_id: str, task_id: str) -> dict[str, Any]:
    try:
        return review_notes.get(run_id, task_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="no such run or task") from None
    except ValueError:
        raise HTTPException(status_code=422, detail=review_notes.INVALID) from None


@router.put("/runs/{run_id}/tasks/{task_id}/review")
async def save_task_review(run_id: str, task_id: str, request: Request) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > review_notes.MAX_BODY:
            raise HTTPException(status_code=422, detail=review_notes.INVALID)

    def parse_and_put() -> dict[str, Any]:
        return review_notes.put(run_id, task_id, json.loads(data))

    try:
        return await run_in_threadpool(parse_and_put)
    except LookupError:
        raise HTTPException(status_code=404, detail="no such run or task") from None
    except review_notes.VersionConflict:
        raise HTTPException(status_code=409, detail="Changed in another tab; reload the review") from None
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(status_code=422, detail=review_notes.INVALID) from None
