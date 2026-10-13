"""Run the six real Hermes routing probes. This spends provider quota."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from build_project import build

PROMPTS = Path(__file__).with_name("prompts.yaml")


def profile_name() -> str:
    return f"hc-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def create_eval_home(source: Path, out: Path) -> Path:
    home = source / "profiles" / profile_name()
    try:
        home.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"evaluation profile already exists: {home}") from exc
    for name in ("config.yaml", ".env"):
        if (source / name).is_file():
            shutil.copy2(source / name, home / name)
    for name in ("plugins/hermes_control", "skills/software-development/hermes-control"):
        link = source / name
        if link.is_symlink():
            destination = home / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(link.resolve(), target_is_directory=True)
    return home


def copy_evidence(home: Path, out: Path, prompt_id: str) -> tuple[Path, Path, list[Path]]:
    evidence = out / "evidence" / prompt_id
    evidence.mkdir(parents=True, exist_ok=False)
    for name in ("config.yaml",):
        if (home / name).is_file():
            shutil.copy2(home / name, evidence / name)
    logs = []
    for path in sorted((home / "control").glob("run-*.jsonl")):
        target = evidence / "control" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        logs.append(target)
    db_copy = evidence / "state.db"
    if (home / "state.db").is_file():
        with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as source_db:
            with sqlite3.connect(db_copy) as target_db:
                source_db.backup(target_db)
    return evidence, db_copy, logs


def verify_command(source: Path) -> str:
    verify = source / "control/verify"
    path = verify / "python-path"
    if not path.is_file() or not (verify / "site").is_dir():
        raise ValueError("verification environment missing; run scripts/verify-setup.sh")
    lines = path.read_text(encoding="utf-8").splitlines()
    python = lines[0].strip() if lines else ""
    if not python or not Path(python).is_file():
        raise ValueError("verification Python missing; run scripts/verify-setup.sh")
    return f"PYTHONPATH={shlex.quote(str(verify / 'site'))} {shlex.quote(python)} -m pytest -q -p no:cacheprovider"


def answer_with_reason(home: Path, transcript: str, started: float, ended: float) -> tuple[str, str]:
    db_path = home / "state.db"
    if not db_path.is_file():
        return "", "state.db missing"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        sessions = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        body = next((name for name in ("content", "text", "message") if name in columns), None)
        if not body or not {"session_id", "role"} <= columns:
            return "", "messages schema incomplete"
        footers = re.findall(r"(?m)^Session:[ \t]*(\S+)[ \t]*$", transcript)
        session_id = footers[-1] if footers else None
        if session_id:
            if "id" not in sessions or not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return "", "session footer not found in state.db"
        elif "id" in sessions:
            clock = next((name for name in ("created_at", "started_at") if name in sessions), None)
            if clock:
                candidates = []
                parent = next((name for name in ("parent_session_id", "parent_id", "parent_session", "parent")
                               if name in sessions), None)
                session_fields = f"id, {clock}" + (f", {parent}" if parent else "")
                for row in db.execute(f"SELECT {session_fields} FROM sessions"):
                    candidate, raw_time = row[:2]
                    if parent and row[2] not in (None, ""):
                        continue
                    try:
                        stamp = (datetime.fromisoformat(raw_time.replace("Z", "+00:00")).timestamp()
                                 if isinstance(raw_time, str) else float(raw_time))
                    except (ValueError, TypeError):
                        continue
                    if started <= stamp <= ended:
                        candidates.append((stamp, candidate))
                if len(candidates) != 1:
                    return "", "ambiguous session"
                session_id = candidates[0][1]
        if not session_id:
            return "", "ambiguous session"
        tool_filter = (" AND (tool_calls IS NULL OR tool_calls='' OR tool_calls='[]' OR tool_calls='null')"
                       if "tool_calls" in columns else "")
        rows = db.execute(f"SELECT {body} FROM messages WHERE session_id=? AND role='assistant'{tool_filter} ORDER BY rowid DESC LIMIT 1",
                          (session_id,))
        row = rows.fetchone()
        if row is None:
            return "", "no assistant final message"
        answer = row[0].strip() if isinstance(row[0], str) else ""
        return (answer, "" if answer else "final assistant message is empty")
    finally:
        db.close()


def final_answer(home: Path, transcript: str, started: float, ended: float) -> str:
    return answer_with_reason(home, transcript, started, ended)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="output directory (default: system temp)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="allow quota-consuming Hermes calls")
    parser.add_argument("--keep-home", action="store_true", help="keep the temporary Hermes profile")
    args = parser.parse_args(argv)
    if not args.live and not args.dry_run:
        parser.error("refusing to run without --live; use --dry-run to preview")
    if args.live and args.dry_run:
        parser.error("choose either --live or --dry-run")
    # JSON syntax is valid YAML and works from plain system Python.
    prompts = json.loads(PROMPTS.read_text(encoding="utf-8"))
    if args.out:
        out = args.out.resolve()
    elif args.dry_run:
        out = Path(tempfile.gettempdir()) / "hermes-routing-eval-<new>"
    else:
        out = Path(tempfile.mkdtemp(prefix="hermes-routing-eval-"))
    source = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    try:
        verify = verify_command(source)
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        for item in prompts:
            project = out / "projects" / item["id"]
            prompt = item["prompt"].replace("{verify}", verify)
            print(f"{item['id']}: build {project}; hermes chat -q {prompt!r} "
                  f"--oneshot --in {project}")
        print(f"records: {out / 'prompts.jsonl'}")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    record_file = out / "prompts.jsonl"
    if record_file.exists():
        parser.error(f"{record_file} already exists; choose a new --out")
    try:
        home = create_eval_home(source, out)
    except FileExistsError as exc:
        parser.error(str(exc))
    verify_python = (source / "control/verify/python-path").read_text(encoding="utf-8").splitlines()[0].strip()
    try:
        for item in prompts:
            project = build(out / "projects" / item["id"])
            prompt = item["prompt"].replace("{verify}", verify)
            command = ["hermes", "chat", "-q", prompt, "--oneshot", "--in", str(project)]
            started = time.time()
            try:
                completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                           env={**os.environ, "HERMES_HOME": str(home),
                                                "HERMES_DISABLE_LAZY_INSTALLS": "1"})
                returncode, transcript, stderr = completed.returncode, completed.stdout, completed.stderr
            except OSError as exc:
                returncode, transcript, stderr = 127, "", str(exc)
            ended = time.time()
            answer, reason = answer_with_reason(home, transcript, started, ended)
            evidence, db_copy, logs = copy_evidence(home, out, item["id"])
            record = {"id": item["id"], "expect": item["expect"], "engine": item.get("engine"),
                      "prompt": prompt, "repo": str(project), "eval_home": str(home),
                      "evidence_home": str(evidence), "state_db": str(db_copy),
                      "run_logs": [str(path) for path in logs], "verify_python": verify_python,
                      "started_at": started, "ended_at": ended, "returncode": returncode,
                      "answer": answer, "answer_reason": reason,
                      "transcript": transcript, "stderr": stderr}
            with record_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{item['id']}: exit {returncode}; record in {record_file}", flush=True)
    finally:
        if not args.keep_home:
            shutil.rmtree(home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
