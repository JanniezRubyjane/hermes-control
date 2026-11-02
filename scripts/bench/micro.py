"""Offline controller benchmarks. Run directly with Python; no engine quota is used.

Only the verify sandbox and its probe use the real codex binary; workers are
local stubs. dispatch-write-verify is skipped when codex is unavailable.

Setup is outside each timed interval. Runlog timings are per append (1000
appends per interval); *_ms fields always use milliseconds, and *_us expose
the runlog result in microseconds. --home affects only stats/health reads.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

# Support `python scripts/bench/micro.py` from an uninstalled checkout.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Engine registration reads settings at import time too. Isolate that read
# before any scenario is selected, including when HERMES_HOME is unset.
with tempfile.TemporaryDirectory(prefix="hc-bench-import-") as _import_home:
    with patch.dict(os.environ, HERMES_HOME=_import_home):
        from hermes_control import dispatch, health, limits, verify, watch
        from hermes_control.engines.base import EngineAdapter, EngineInfo
        from hermes_control.events import ControlResult, RawEvent
        from hermes_control.runlog import RunLog

SCENARIOS = (
    "dispatch-readonly", "dispatch-write-noverify", "dispatch-write-verify",
    "runlog-append", "stats", "health",
)
APPENDS = 1000


class StubAdapter(EngineAdapter):
    """An immediate local worker using the real subprocess and event paths."""

    name = "claude"
    sign_in_command = ""
    efforts = ()

    def probe(self):
        return EngineInfo(name=self.name, version="bench", path=sys.executable)

    def auth_problem(self, info):
        return None

    def build_argv(self, task, *, cwd):
        script = ""
        if task.policy.write:
            script += (
                "from pathlib import Path; "
                f"Path({('bench-' + task.id + '.txt')!r}).write_text('bench\\n'); "
            )
        script += "print('{\"ok\": true}')"
        return [sys.executable, "-c", script]

    def parse_line(self, line):
        return [RawEvent(kind="result", payload=json.loads(line))]

    def extract_result(self, task, raws, diff):
        return ControlResult(
            task_id=task.id, engine=task.engine,
            ok=any(raw.kind == "result" and raw.payload.get("ok") for raw in raws),
            text="bench", session_id=None, diff=diff,
        )


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "bench@example.invalid")
    _git(path, "config", "user.name", "hc-bench")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.hooksPath", "/dev/null")
    (path / "seed.txt").write_text("bench\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-qm", "bench seed")
    return path


def _dispatch(repo, name):
    identity = uuid.uuid4().hex
    write = name != "dispatch-readonly"
    task = dict(id=identity, goal="Measure controller overhead", engine="claude",
                why="Offline benchmark", policy="write" if write else "read-only")
    if name == "dispatch-write-verify":
        task["verify"] = "true"
    result = dispatch.dispatch([task], parent_cwd=str(repo), run_id=identity)
    entries = result.get("tasks", [])
    if result.get("error") or len(entries) != 1 or not entries[0]["ok"]:
        raise RuntimeError(f"benchmark dispatch failed: {result}")
    entry = entries[0]
    if write and not entry.get("commit"):
        raise RuntimeError(f"benchmark did not land a commit: {entry}")
    if task.get("verify") and not (entry.get("verify") or {}).get("passed"):
        raise RuntimeError(f"benchmark verification failed: {entry}")


def _measure(operation, reps, *, divisor=1):
    operation()  # Exactly one untimed warm-up.
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000 / divisor)
    ordered = sorted(samples)
    return dict(median_ms=statistics.median(samples),
                p90_ms=ordered[math.ceil(0.9 * reps) - 1], min_ms=ordered[0],
                reps=reps, skipped=False)


def run_bench(*, reps=10, only=None, home=None, root=None):
    """Return JSON-compatible measurements; root can locate temporary test data.

    Every scenario gets an isolated repo/home. Stats/health read copies of the
    explicit home's run logs and state.db. Only the verify sandbox and its probe
    use the real codex binary; no engine quota is used.
    """
    if reps < 1:
        raise ValueError("reps must be positive")
    selected = list(SCENARIOS if only is None else only)
    unknown = set(selected) - set(SCENARIOS)
    if unknown:
        raise ValueError(f"unknown scenarios: {', '.join(sorted(unknown))}")
    read_home = Path(home).expanduser().resolve() if home is not None else None
    if read_home is not None and not read_home.is_dir():
        raise ValueError("--home must be an existing directory")
    result = dict(sha=_git(REPO, "rev-parse", "HEAD"),
                  dirty=bool(_git(REPO, "status", "--porcelain")),
                  python=platform.python_version(),
                  scenarios={})
    with tempfile.TemporaryDirectory(prefix="hc-bench-", dir=root) as temporary:
        for name in selected:
            sandbox = "codex" if name == "dispatch-write-verify" and verify.find_sandbox() else None
            if name == "dispatch-write-verify" and sandbox is None:
                result["scenarios"][name] = dict(
                    median_ms=None, p90_ms=None, min_ms=None, reps=0, sandbox=None,
                    skipped="verification sandbox unavailable (find_sandbox returned None)",
                )
                continue
            directory = Path(temporary) / name
            directory.mkdir()
            repo = _repo(directory / "repo")
            isolated_home = directory / "home"
            isolated_home.mkdir()
            if read_home is not None and name in ("stats", "health"):
                control = isolated_home / "control"
                control.mkdir()
                for log in (read_home / "control").glob("run-*.jsonl"):
                    shutil.copyfile(log, control / log.name)
                database = read_home / "state.db"
                if database.is_file():
                    shutil.copyfile(database, isolated_home / "state.db")
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, HERMES_HOME=str(isolated_home)))
                if name.startswith("dispatch-"):
                    stack.enter_context(patch.object(dispatch, "get_adapter",
                                                    lambda name: StubAdapter()))
                    # Account telemetry is an engine interaction, not controller work.
                    stack.enter_context(patch.object(
                        limits, "usage", lambda engine, **kw: limits.Usage(engine=engine)))
                    operation = lambda: _dispatch(repo, name)
                elif name == "runlog-append":
                    def operation():
                        log = RunLog(uuid.uuid4().hex)
                        for _ in range(APPENDS):
                            log.append_next("bench", [RawEvent(kind="text", payload={"text": "bench"})],
                                            engine="claude", now=time.time())
                else:
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    operation = (lambda: watch.stats(limit=30)) if name == "stats" else (
                        lambda: health.report(repo=repo))
                measured = _measure(operation, reps, divisor=APPENDS if name == "runlog-append" else 1)
                if name == "runlog-append":
                    measured.update({key.replace("_ms", "_us"): measured[key] * 1000
                                     for key in ("median_ms", "p90_ms", "min_ms")})
                measured["sandbox"] = sandbox
                result["scenarios"][name] = measured
    return result


def compare(current, baseline):
    """Print comparable medians; return 1 only when both regression gates pass."""
    failed = False
    print("scenario                     baseline      current       delta  status")
    for name, measured in current["scenarios"].items():
        previous = baseline.get("scenarios", {}).get(name)
        if measured.get("skipped") or (previous and previous.get("skipped")):
            print(f"{name:28} skipped")
            continue
        if not previous:
            print(f"{name:28} no baseline")
            continue
        before, after = previous["median_ms"], measured["median_ms"]
        unit, threshold = "ms", 50
        if name == "runlog-append":
            before = previous.get("median_us", before * 1000)
            after = measured.get("median_us", after * 1000)
            unit, threshold = "us", 5
        delta = after - before
        regression = delta > before * 0.10 and delta > threshold
        failed |= regression
        print(f"{name:28} {before:10.3f} {after:12.3f} {delta:+10.3f} {unit} "
              f"{'FAIL' if regression else 'ok'}")
    return int(failed)


def _load_baseline(path):
    """Reject malformed measurements before comparison or benchmark work."""
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or not isinstance(baseline.get("scenarios"), dict):
        raise ValueError("expected an object with a scenarios object")
    for name, measured in baseline["scenarios"].items():
        if not isinstance(measured, dict):
            raise ValueError(f"{name}: expected a measurement object")
        if measured.get("skipped"):
            continue
        for field in ("median_ms", "median_us"):
            if field == "median_us" and (name != "runlog-append" or field not in measured):
                continue
            value = measured.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name}: {field} must be a finite nonnegative number")
    return baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--only", help=",".join(SCENARIOS))
    parser.add_argument("--home", type=Path, help="copy run logs and state.db from this home for stats/health only")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    selected = args.only.split(",") if args.only is not None else None
    if args.reps < 1 or (selected is not None and set(selected) - set(SCENARIOS)):
        parser.error("--reps must be positive and --only must name known scenarios")
    if args.home is not None:
        args.home = args.home.expanduser().resolve()
        if not args.home.is_dir():
            parser.error("--home must be an existing directory")
    baseline = None
    if args.baseline is not None:
        try:
            baseline = _load_baseline(args.baseline)
        except (OSError, ValueError, OverflowError) as exc:
            parser.error("--baseline: " + " ".join(str(exc).splitlines()))
    result = run_bench(reps=args.reps, only=selected, home=args.home)
    serialized = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return compare(result, baseline) if baseline is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
