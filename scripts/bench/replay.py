"""Extract private replay tasks from first-parent Hermes-Control task merges."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import random
import re
import secrets
import shlex
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "bench"

from hermes_control import verify
from . import grader
from .spec import TaskSpec

STRATA = ("small", "medium", "large")
PYTEST_PASSED = r"^(?:=+ )?(\d+) passed\b"
STATUSES = ("valid", "invalid-green-on-base", "invalid-red-on-merge", "not-validated",
            "invalid-index")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(grader.git_argv(repo, *args), env=grader.git_env(),
                          check=True, capture_output=True).stdout


def _paths(data: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape")
            for item in data.split(b"\0") if item]


def _test_file(name: str) -> bool:
    path = PurePosixPath(name)
    return (len(path.parts) > 1 and path.parts[0] == "tests"
            and path.name.startswith("test_") and path.suffix == ".py"
            and not any(part in {"..", ".git"} for part in path.parts))


def _changes(repo: Path, base: str, merge: str) -> dict[str, int]:
    result = {}
    for record in _git(repo, "diff", "--numstat", "--no-renames", "-z",
                       base, merge, "--").split(b"\0"):
        if record:
            added, deleted, name = record.split(b"\t", 2)
            if not name.startswith(b"tests/"):
                result[name.decode("utf-8", errors="surrogateescape")] = sum(
                    int(n) for n in (added, deleted) if n != b"-")
    return result


def _candidates(repo: Path) -> list[dict]:
    merges = []
    # Include integration merges in the rework timeline, but never as tasks.
    log = _git(repo, "log", "--first-parent", "--merges", "--reverse", "-z",
               "--format=%H%x00%P%x00%ct%x00%s", "refs/heads/main", "--")
    fields = log.rstrip(b"\0").split(b"\0") if log else []
    for offset in range(0, len(fields), 4):
        sha, parents, timestamp, subject = (part.decode("utf-8", errors="replace")
                                           for part in fields[offset:offset + 4])
        parents = parents.split()
        changes = _changes(repo, parents[0], sha)
        merges.append(dict(merge_sha=sha, base=parents[0], parents=parents,
                           timestamp=int(timestamp), subject=subject, changes=changes))
    tasks = []
    for index, merge in enumerate(merges):
        if not merge["subject"].startswith("Merge hermes-control task "):
            continue
        hidden = sorted(name for name in _paths(_git(
            repo, "diff", "--name-only", "--diff-filter=AM", "--no-renames", "-z",
            merge["base"], merge["merge_sha"], "--")) if _test_file(name))
        if not hidden:
            continue
        commits = _git(repo, "rev-list", "--reverse", "--topo-order", "--no-merges",
                       f"{merge['merge_sha']}^1..{merge['merge_sha']}^2").decode().splitlines()
        if not commits:
            continue
        parts = []
        test_contract = False
        for commit in commits:
            paths = _paths(_git(repo, "diff-tree", "--no-commit-id", "--name-only",
                                "-r", "-z", "--no-renames", commit))
            if paths and all(_test_file(path) for path in paths):
                test_contract = True
            text = _git(repo, "show", "-s", "--format=%B", commit).decode(
                "utf-8", errors="replace")
            # --format adds one record newline beyond the full commit message.
            if text.endswith("\n"):
                text = text[:-1]
            subject, _, body = text.partition("\n")
            if re.fullmatch(r"hermes-control: \S+ \([a-z]+\)", subject):
                text = body.lstrip("\n") if body.strip() else text
            parts.append(text)
        goal = parts[0] if len(parts) == 1 else (
            "Implement every part below in this one change; none of them is in the repository yet.\n\n"
            + "\n\n".join(f"Part {number} of {len(parts)}:\n{text}"
                          for number, text in enumerate(parts, 1)))
        rework = any(0 <= later["timestamp"] - merge["timestamp"] <= 7 * 86400
                     and merge["changes"].keys() & later["changes"].keys()
                     for later in merges[index + 1:])
        tasks.append({**merge, "goal": goal, "hidden": hidden, "rework": rework,
                      "commits": len(commits), "test_contract": test_contract,
                      "first_line": parts[0].splitlines()[0] if parts[0] else "",
                      "size": sum(merge["changes"].values()),
                      "id": "r-" + merge["merge_sha"][:12]})
    # Rank terciles keep similarly sized cohorts even when sizes tie.
    for index, task in enumerate(sorted(tasks, key=lambda t: (t["size"], t["merge_sha"]))):
        task["stratum"] = STRATA[min(2, index * 3 // len(tasks))]
    return tasks


def _sample(tasks: list[dict], limit: int | None, seed: int, strata: bool) -> list[dict]:
    if limit is None or limit >= len(tasks):
        return tasks
    rng = random.Random(seed)
    if not strata:
        chosen = rng.sample(tasks, limit)
    else:
        reworks = [task for task in tasks if task["rework"]]
        chosen = rng.sample(reworks, min(limit, len(reworks)))
        ids = {task["id"] for task in chosen}
        buckets = {name: [task for task in tasks
                          if task["stratum"] == name and task["id"] not in ids]
                   for name in STRATA}
        for bucket in buckets.values():
            rng.shuffle(bucket)
        counts = Counter(task["stratum"] for task in chosen)
        while len(chosen) < limit:
            available = [name for name in STRATA if buckets[name]]
            name = min(available, key=lambda name: counts[name])
            chosen.append(buckets[name].pop())
            counts[name] += 1
    selected = {task["id"] for task in chosen}
    return [task for task in tasks if task["id"] in selected]


def _visible_tests(repo: Path, task: dict) -> list[str]:
    base_files = set(_paths(_git(repo, "ls-tree", "-r", "--name-only", "-z", task["base"])))
    existing = [name for name in task["hidden"] if name in base_files]
    if existing:
        return existing
    for module in sorted(task["changes"], key=lambda p: (-task["changes"][p], p)):
        if PurePosixPath(module).suffix != ".py":
            continue
        filename = "test_" + PurePosixPath(module).stem + ".py"
        matches = sorted(name for name in base_files
                         if _test_file(name) and PurePosixPath(name).name == filename)
        if matches:
            preferred = "tests/" + filename
            return [preferred if preferred in matches else matches[0]]
    return ["tests/test_skill.py"] if "tests/test_skill.py" in base_files else []


def _canary(data: bytes) -> bytes:
    # Preserve a shebang and Python's first/second-line encoding declaration,
    # including a UTF-8 BOM, without decoding historical source encodings.
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    lines = data[len(bom):].splitlines(keepends=True)
    insertion = 1 if lines and lines[0].startswith(b"#!") else 0
    for index, line in enumerate(lines[:2]):
        if re.match(br"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", line):
            insertion = max(insertion, index + 1)
    prefix = b"".join(lines[:insertion])
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\n"
    return (bom + prefix + f"# hc-bench-canary: {secrets.token_hex(8)}\n".encode()
            + b"".join(lines[insertion:]))


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def extract(repo: Path, out: Path | None = None, *, limit: int | None = None,
            seed: int = 0, strata: bool = False, ids: list[str] | None = None) -> list[dict]:
    """Read main's merge history without checking out or modifying the origin."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be nonnegative")
    repo = Path(repo).expanduser().resolve()
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").decode().rstrip("\n")).resolve()
    out = (Path(out) if out is not None else Path("~/.hermes/control/bench/R")).expanduser().resolve()
    if out.is_relative_to(repo):
        raise ValueError("private replay output must be outside the repository being extracted")
    tasks = _candidates(repo)
    if ids is not None:
        unknown = set(ids) - {task["id"] for task in tasks}
        if unknown:
            raise ValueError("unknown replay candidate id: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task["id"] in ids]
    tasks = _sample(tasks, limit, seed, strata)
    # Existing output must not redirect writes back into the origin (or elsewhere).
    if (out / "index.json").is_symlink() or any(
            (out / task["id"]).is_symlink()
            or any(path.is_symlink() for path in (out / task["id"]).rglob("*"))
            for task in tasks):
        raise ValueError("replay output must not contain symlinks")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = []
    for task in tasks:
        directory = out / task["id"]
        directory.mkdir(exist_ok=True, mode=0o700)
        for name in task["hidden"]:
            target = directory / "hidden" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_canary(_git(repo, "show", f"{task['merge_sha']}:{name}")))
        command = "/bin/sh scripts/sandbox-check.sh "
        visible = _visible_tests(repo, task)
        data = dict(id=task["id"], source="R", kind=("bug" if re.match(
            r"fix|bug|regress", task["first_line"], re.I) else "routine"),
            policy="write", goal=task["goal"], protected_paths=[], hidden_dir="hidden",
            repo=dict(base_commit=task["base"], origin_repo=str(repo)),
            verify=command + shlex.join(visible) if visible else "true",
            hidden_cmd=command + shlex.join(task["hidden"]))
        if not visible:
            data["verify_note"] = "No suitable visible tests exist at the base commit."
        TaskSpec.from_dict(data, directory=directory)
        _write_json(directory / "spec.json", data)
        index.append({key: task[key] for key in (
            "id", "merge_sha", "subject", "size", "stratum", "rework", "commits",
            "test_contract")})
    _write_json(out / "index.json", index)
    return index


def validate(out: Path) -> dict[str, int]:
    """Require hidden tests to fail on the base and pass on the real merge."""
    out = Path(out).expanduser().resolve()
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    sandbox = verify.find_sandbox()
    counts = dict.fromkeys(STATUSES, 0)
    try:
        for position, entry in enumerate(index):
            if not isinstance(entry, dict):
                entry = index[position] = {"entry": entry}
            entry.pop("validation_reason", None)
            entry.pop("hidden_passed", None)
            status = "not-validated"
            data = None
            spec_path = None
            try:
                if not isinstance(entry["id"], str):
                    raise TypeError("id and merge_sha must be text")
                task_id = entry["id"]
                if task_id in {"", ".", ".."} or "/" in task_id or "\\" in task_id:
                    raise TypeError("id must be a single plain path component")
                directory = out / task_id
                if directory.is_symlink():
                    raise TypeError("replay task directory must not be a symlink")
                spec_path = directory / "spec.json"
                if spec_path.is_symlink():
                    raise ValueError("replay spec.json must not be a symlink")
                raw = json.loads(spec_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("replay spec.json must be an object")
                data = raw
                data.pop("hidden_expect", None)
                if not isinstance(entry["merge_sha"], str):
                    raise TypeError("id and merge_sha must be text")
                if sandbox is None:
                    entry["validation_reason"] = "sandbox unavailable"
                else:
                    spec = TaskSpec.from_dict(data, directory=spec_path.parent)
                    repo = Path(spec.repo["origin_repo"])
                    with tempfile.TemporaryDirectory(prefix="hc-bench-replay-") as temporary:
                        outcomes = []
                        for name, commit in (("base", spec.repo["base_commit"]),
                                             ("merge", entry["merge_sha"])):
                            checkout = Path(temporary) / name
                            grader.export_checkout(repo, commit, checkout)
                            grader._overlay(spec.hidden_dir, checkout)
                            outcomes.append(verify.run_verify(
                                spec.hidden_cmd, checkout, codex_path=sandbox))
                        base, merge = outcomes
                        if any(outcome.exit_code is None for outcome in outcomes):
                            entry["validation_reason"] = "; ".join(
                                outcome.unproven_reason or "hidden command did not complete"
                                for outcome in outcomes if outcome.exit_code is None)
                        elif merge.exit_code != 0:
                            status = "invalid-red-on-merge"
                        elif base.exit_code == 0:
                            status = "invalid-green-on-base"
                        else:
                            status = "valid"
                            matches = list(re.finditer(PYTEST_PASSED, merge.output_tail,
                                                       re.MULTILINE))
                            if matches:
                                count = int(matches[-1].group(1))
                                data["hidden_expect"] = {"pattern": PYTEST_PASSED, "min": count}
                                entry["hidden_passed"] = count
                            else:
                                entry["validation_reason"] = "no pytest summary in merge output"
            except (KeyError, TypeError) as exc:
                status = "invalid-index"
                entry["validation_reason"] = str(exc)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                entry["validation_reason"] = str(exc)
            if data is not None and spec_path is not None:
                try:
                    TaskSpec.from_dict(data, directory=spec_path.parent)
                except (OSError, TypeError, ValueError) as exc:
                    status = "not-validated"
                    entry.pop("hidden_passed", None)
                    entry["validation_reason"] = str(exc)
                    data.pop("hidden_expect", None)
                try:
                    _write_json(spec_path, data)
                except OSError as exc:
                    status = "not-validated"
                    entry.pop("hidden_passed", None)
                    reason = entry.get("validation_reason")
                    entry["validation_reason"] = f"{reason}; {exc}" if reason else str(exc)
            entry["status"] = status
            counts[status] += 1
    finally:
        _write_json(out / "index.json", index)
    print(" ".join(f"{name}: {count}" for name, count in counts.items()))
    return counts


def link(out: Path, to: Path, *, include_test_contract: bool = False) -> dict[str, int]:
    """Build a private suite from validated, eligible replay tasks."""
    out = Path(out).expanduser().resolve()
    to = Path(to).expanduser().resolve()
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    if not isinstance(index, list):
        raise ValueError("replay index must be a list")
    seen = set()
    for entry in index:
        if not isinstance(entry, dict):
            raise ValueError("replay index entry must be an object")
        task_id = entry.get("id")
        if (not isinstance(task_id, str) or task_id in {"", ".", ".."}
                or "/" in task_id or "\\" in task_id):
            raise ValueError("id must be a single plain path component")
        if task_id in seen:
            raise ValueError(f"duplicate replay id: {task_id}")
        seen.add(task_id)
    counts = {"linked": 0, "skipped-test-contract": 0, "skipped-not-valid": 0}
    targets = []
    # Inspect origins before creating anything, including for excluded entries.
    for entry in index:
        task_id = entry["id"]
        source = out / task_id
        spec_path = source / "spec.json"
        eligible = (entry.get("status") == "valid"
                    and (entry.get("test_contract") is not True or include_test_contract))
        if eligible and (source.is_symlink() or not source.is_dir()
                         or source.resolve().parent != out or not spec_path.is_file()):
            raise ValueError(f"{task_id}: replay task must be a real directory directly "
                             "under out containing spec.json")
        directory = source.resolve()
        if spec_path.exists():
            data = json.loads(spec_path.read_text(encoding="utf-8"))
            repo = Path(data["repo"]["origin_repo"]).expanduser().resolve()
            if to.is_relative_to(repo):
                raise ValueError("replay suite must be outside the repository")
        if entry.get("status") != "valid":
            counts["skipped-not-valid"] += 1
        elif entry.get("test_contract") is True and not include_test_contract:
            counts["skipped-test-contract"] += 1
        else:
            targets.append((task_id, directory))
    # Also forbid destinations in another checkout, including this tool's repo.
    if any((parent / ".git").exists() for parent in (to, *to.parents)):
        raise ValueError("replay suite must be outside the repository")
    if to.exists() and (not to.is_dir() or any(to.iterdir())):
        raise ValueError("replay suite destination exists and is not empty")
    to.mkdir(parents=True, exist_ok=True, mode=0o700)
    to.chmod(0o700)
    for task_id, directory in targets:
        (to / task_id).symlink_to(directory, target_is_directory=True)
        counts["linked"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extraction = commands.add_parser("extract")
    extraction.add_argument("--repo", type=Path, required=True)
    extraction.add_argument("--out", type=Path, default=Path("~/.hermes/control/bench/R"))
    extraction.add_argument("--limit", type=int)
    extraction.add_argument("--seed", type=int, default=0)
    extraction.add_argument("--strata", action="store_true")
    extraction.add_argument("--id", dest="ids", action="append")
    validation = commands.add_parser("validate")
    validation.add_argument("--out", type=Path, required=True)
    linking = commands.add_parser("link")
    linking.add_argument("--out", type=Path, required=True)
    linking.add_argument("--to", type=Path, required=True)
    linking.add_argument("--include-test-contract", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            index = extract(args.repo, args.out, limit=args.limit, seed=args.seed,
                            strata=args.strata, ids=args.ids)
            print(f"extracted: {len(index)}")
        elif args.command == "link":
            counts = link(args.out, args.to, include_test_contract=args.include_test_contract)
            print(" ".join(f"{name}: {count}" for name, count in counts.items()))
        else:
            validate(args.out)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"replay: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
