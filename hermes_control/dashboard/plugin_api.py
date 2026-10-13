"""Engines tab backend, mounted by the dashboard at /api/plugins/hermes_control/.

A thin translation to HTTP. Engine status is the same engine_state() the
dispatcher routes on, so this page and a dispatch summary never disagree; the
codex sign-in lives in hermes_control/device_login.py.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse


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
    device_login, limits, model_catalog, model_policy, pauses, planner_status, runs)
from hermes_control.dispatch import NotReady, resume_blocker, resume_paused  # noqa: E402
from hermes_control.engines.codex import (  # noqa: E402
    REASONING_SUMMARY_CHOICES, read_reasoning_summary, save_reasoning_summary)
from hermes_control.engines.registry import ENGINE_NAMES, engine_state, get_adapter  # noqa: E402
from hermes_control.verify import find_sandbox  # noqa: E402

router = APIRouter()

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


@router.put("/limits")
def put_limits(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"limits": limits.save_caps(body.get("limits"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


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
