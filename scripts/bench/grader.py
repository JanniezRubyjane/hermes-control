"""Grade committed work in a disposable checkout using the controller sandbox."""

from __future__ import annotations

import difflib
import hashlib
import io
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hermes_control import verify

try:
    from hermes_control.worktree import harden_git_argv
except ImportError:
    def harden_git_argv(args):
        return ["-c", "core.fsmonitor=false", "-c", "diff.external=", *args]

from .spec import TaskSpec


@dataclass
class Grade:
    solved: bool = False
    hidden_exit: int | None = None
    hidden_tail: str = ""
    gate_violations: list[str] = field(default_factory=list)
    touched_protected: list[str] = field(default_factory=list)
    diff_files: int = 0
    diff_lines: int = 0
    answer_match: bool | None = None
    status: str = "graded"
    reason: str | None = None
    hidden_checked: bool = False
    skipped_symlinks: list[str] = field(default_factory=list)


def git_env(repo: Path | None = None) -> dict[str, str]:
    """No caller Git settings, user configuration, or executable search paths."""
    return {**({"GIT_DIR": str(real_git_dir(repo))} if repo is not None else {}),
            "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.defpath, "HOME": os.devnull, "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0"}


def real_git_dir(repo: Path) -> Path:
    """Resolve metadata only for a caller-owned origin, never a worker checkout."""
    repo = Path(repo).resolve()
    for ancestor in (repo, *repo.parents):
        metadata = ancestor / ".git"
        if metadata.exists():
            if metadata.is_file():
                pointer = metadata.read_text().strip()
                if not pointer.startswith("gitdir: "):
                    raise ValueError("invalid origin gitdir")
                return (metadata.parent / pointer[8:]).resolve()
            return metadata.resolve()
        if (ancestor / "HEAD").is_file() and (ancestor / "objects").is_dir():
            return ancestor
    raise ValueError("not inside a git repository")


def git_argv(repo: Path, *args: str) -> list[str]:
    # Initialization has no existing repository to discover, even inside another repo.
    metadata = Path(repo).resolve() / ".git" if args and args[0] == "init" else real_git_dir(repo)
    worktree_args = []
    if not (args and args[0] == "init"):
        root = Path(repo).resolve()
        for ancestor in (root, *root.parents):
            if (ancestor / ".git").exists():
                # --git-dir disables Git's normal work tree root discovery.
                worktree_args = ["--work-tree=" + str(ancestor)]
                break
            if ancestor == metadata:  # Bare repository.
                break
    # Keep these explicit even with controller versions lacking the helper.
    if args and args[0] in {"diff", "show", "log"}:
        args = (args[0], "--no-textconv", "--no-ext-diff", *args[1:])
    return ["git", *harden_git_argv([
        "-C", str(repo), "--git-dir=" + str(metadata), *worktree_args,
        "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
        "-c", "diff.external=", *args])]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(git_argv(repo, *args), check=True,
                          env=git_env(None if args and args[0] == "init" else repo),
                          capture_output=True, text=True).stdout.rstrip("\n")


def export_checkout(repo: Path, commit: str, destination: Path) -> list[str]:
    """Export raw blobs, returning (and logging) skipped unsafe symlink paths.

    No checkout/archive conversions: attributes, filters and hooks cannot run.
    Write all files and parent directories before creating any symlinks.
    """
    tree = subprocess.run(git_argv(repo, "ls-tree", "-r", "-z", "--full-tree", commit),
                          check=True, capture_output=True, env=git_env(repo)).stdout
    entries = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if name.startswith(b"/") or any(p in {b"", b".", b".."} or p.lower() == b".git"
                                        for p in name.split(b"/")):
            raise ValueError(f"unsafe checkout path: {os.fsdecode(name)!r}")
        if mode == b"160000":  # gitlinks have no file content to export
            continue
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            raise ValueError(f"unsupported checkout entry: {os.fsdecode(name)!r}")
        entries.append((mode, oid, os.fsdecode(name)))
    destination.mkdir(parents=True)
    root = destination.resolve()
    symlinks = []
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(git_argv(repo, "cat-file", "--batch"), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=errors, env=git_env(repo)) as blobs:
            try:
                for mode, oid, name in entries:
                    blobs.stdin.write(oid + b"\n")
                    blobs.stdin.flush()
                    header = blobs.stdout.readline().split()
                    if len(header) != 3 or header[:2] != [oid, b"blob"]:
                        raise ValueError(f"cannot read checkout blob: {name!r}")
                    size = int(header[2])
                    data = blobs.stdout.read(size)
                    if len(data) != size or blobs.stdout.read(1) != b"\n":
                        raise ValueError(f"truncated checkout blob: {name!r}")
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if mode == b"120000":
                        symlinks.append((name, os.fsdecode(data)))
                        continue
                    permissions = 0o755 if mode == b"100755" else 0o644
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 permissions)
                    with os.fdopen(fd, "wb") as handle:
                        os.fchmod(handle.fileno(), permissions)
                        handle.write(data)
            finally:
                blobs.stdin.close()
                blobs.stdout.close()
            code = blobs.wait()
            if code:
                errors.seek(0)
                raise subprocess.CalledProcessError(code, blobs.args, stderr=errors.read())
    skipped = []
    for name, link in symlinks:
        target = root / name
        try:
            resolved = (target.parent / link).resolve()
            safe = (bool(link) and "\0" not in link and resolved.is_relative_to(root)
                    and resolved != target and target not in resolved.parents)
        except (OSError, RuntimeError, ValueError):
            safe = False
        if safe:
            target.symlink_to(link)
        else:
            skipped.append(name)
            logging.getLogger(__name__).warning("Skipped unsafe checkout symlink: %s", name)
    return skipped


@dataclass(frozen=True)
class HiddenSnapshot:
    directory: Path
    manifest: dict[str, str]


def hidden_manifest(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("hidden_dir: directory does not exist or is a symlink")
    manifest = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_symlink() or ".git" in relative.parts:
            raise ValueError("hidden_dir: symlinks and .git entries are forbidden")
        if not path.is_dir() and not path.is_file():
            raise ValueError("hidden_dir: only regular files and directories are allowed")
        manifest[relative.as_posix()] = ("dir" if path.is_dir() else
                                        hashlib.sha256(path.read_bytes()).hexdigest())
    return manifest


def snapshot_hidden(directory: Path, destination: Path) -> HiddenSnapshot:
    """Copy trusted tests to a private caller-owned temp directory BEFORE dispatch."""
    manifest = hidden_manifest(directory)
    destination.mkdir()
    _overlay(directory, destination)
    if hidden_manifest(destination) != manifest or hidden_manifest(directory) != manifest:
        raise ValueError("hidden_dir: changed while taking snapshot")
    return HiddenSnapshot(destination, manifest)


def _hidden_changed(spec: TaskSpec, snapshot: HiddenSnapshot) -> bool:
    try:
        return (hidden_manifest(spec.hidden_dir) != snapshot.manifest
                or hidden_manifest(snapshot.directory) != snapshot.manifest)
    except (OSError, ValueError):
        return True


def _leak_patterns(spec: TaskSpec, hidden: Path) -> set[str]:
    patterns = {str(spec.hidden_dir), str(hidden)}
    if spec.hidden_dir.is_relative_to(Path.home()):
        relative = spec.hidden_dir.relative_to(Path.home()).as_posix()
        patterns.update({relative, "~/" + relative})
    for path in hidden.rglob("*"):
        if path.is_file():
            patterns.update(re.findall(r"hc-bench-canary:[ \t]*(\S+)", path.read_text(
                encoding="utf-8", errors="replace")))
    return patterns


def snapshot_sentinels(paths) -> dict[str, tuple | None]:
    """Capture location, type, permissions and bytes without opening special files."""
    snapshots = {}
    for value in paths:
        path = Path(value).absolute()
        resolved = path
        try:
            resolved = path.resolve()
            info = path.lstat()
            target = resolved.stat()
            metadata = (str(resolved), info.st_mode, info.st_mtime_ns,
                        target.st_mode, target.st_mtime_ns)
            if not (stat.S_ISREG(target.st_mode) or stat.S_ISDIR(target.st_mode)):
                state = ("special", *metadata)
            elif not target.st_mode & 0o444:
                state = ("unreadable", *metadata)
            elif stat.S_ISDIR(target.st_mode):
                with os.scandir(resolved) as entries:
                    state = ("directory", *metadata, tuple(sorted(entry.name for entry in entries)))
            else:
                fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        state = ("special", *metadata)
                    else:
                        state = ("file", *metadata, hashlib.sha256(handle.read()).hexdigest())
            snapshots[str(path)] = state
        except FileNotFoundError:
            # Include resolution even for an absent leaf, so redirecting its
            # parent cannot conceal a location change between two missing files.
            snapshots[str(path)] = ("missing", str(resolved))
        except (OSError, ValueError, RuntimeError) as exc:
            snapshots[str(path)] = ("unreadable", type(exc).__name__)
    return snapshots


def _answer_set(answer) -> set[str]:
    lines = answer.splitlines() if isinstance(answer, str) else answer
    return {" ".join(line.casefold().split()) for line in lines if line.strip()}


def _tree_blobs(repo: Path, revision: str) -> dict[str, str]:
    tree = git(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    return {name: metadata.split()[2] for entry in tree.split("\0") if entry
            for metadata, name in [entry.split("\t", 1)]
            if metadata.split()[1] == "blob"}


def _raw_blobs(repo: Path, ids) -> dict[str, str]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    output = subprocess.run(git_argv(repo, "cat-file", "--batch"),
                            input="".join(oid + "\n" for oid in ids).encode(),
                            capture_output=True, check=True, env=git_env(repo)).stdout
    stream = io.BytesIO(output)
    blobs = {}
    for oid in ids:
        header = stream.readline().split()
        if len(header) != 3 or header[:2] != [oid.encode(), b"blob"]:
            raise ValueError("invalid scan blob")
        data = stream.read(int(header[2]))
        if stream.read(1) != b"\n":
            raise ValueError("truncated scan blob")
        blobs[oid] = data.decode("utf-8", errors="replace")
    return blobs


def _worker_index(repo: Path, worktree: Path) -> Path | None:
    """Locate the index via origin metadata, ignoring the worker's .git pointer."""
    metadata = real_git_dir(repo)
    if repo == worktree:
        return metadata / "index"
    for directory in (metadata / "worktrees").glob("*"):
        pointer = directory / "gitdir"
        if pointer.is_file() and Path(pointer.read_text().strip()) == worktree / ".git":
            return directory / "index"
    return None


def _forbidden_diff(spec: TaskSpec, repo: Path, commit: str, base: str | None,
                    worktree: Path | None = None) -> bool:
    """Read raw objects from the trusted origin and worker files without Git.

    No worker Git directory/config or attributes enter any Git invocation.
    Inspect every introduced tree, including blobs deleted in later commits.
    """
    worktree = worktree or repo
    patterns = [re.compile(pattern) for pattern in spec.forbidden_diff_patterns]
    base = git(repo, "rev-parse", "--verify", f"{base or 'HEAD'}^{{commit}}")
    baseline = _tree_blobs(repo, base)
    blobs = _raw_blobs(repo, baseline.values())

    def matches(name, text):
        old = blobs.get(baseline.get(name), "")
        added = [line[2:] for line in difflib.ndiff(old.splitlines(), text.splitlines())
                 if line.startswith("+ ")]
        return any(pattern.search("\n".join(added)) for pattern in patterns)

    trees = []
    if commit and not commit.startswith("-"):
        resolved = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        trees = [_tree_blobs(repo, revision) for revision in
                 git(repo, "rev-list", f"{base}..{resolved}").splitlines()]
    index = _worker_index(repo, worktree)
    if index is not None and index.is_file():
        staged = subprocess.run(git_argv(repo, "ls-files", "--stage", "-z"), check=True,
                                env={**git_env(repo), "GIT_INDEX_FILE": str(index)},
                                capture_output=True, text=True).stdout
        trees.append({name: metadata.split()[1] for entry in staged.split("\0") if entry
                      for metadata, name in [entry.split("\t", 1)]
                      if metadata.split()[0] in {"100644", "100755", "120000"}})
    introduced = {oid for tree in trees for name, oid in tree.items()
                  if oid != baseline.get(name)}
    new_blobs = _raw_blobs(repo, introduced)
    for tree in trees:
        for name, oid in tree.items():
            if oid != baseline.get(name) and matches(name, new_blobs[oid]):
                return True
    # fwalk + dir_fd prevents a swapped parent symlink redirecting an open.
    for directory, dirs, files, directory_fd in os.fwalk(worktree, follow_symlinks=False):
        dirs[:] = [name for name in dirs if name.lower() != ".git"]
        for name in files:
            if name.lower() == ".git":
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as handle:
                if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    relative = (Path(directory) / name).relative_to(worktree).as_posix()
                    if matches(relative, handle.read().decode("utf-8", errors="replace")):
                        return True
    return False


def _prefixed_answer(spec: TaskSpec, answer) -> set[str] | None:
    lines = answer.splitlines() if isinstance(answer, str) else answer
    selected = None
    for line in lines:
        # Remove inline code and paired Markdown emphasis, preserving name underscores.
        line = re.sub(r"(?<!\w)(\*{1,3}|_{1,3})(.+?)\1(?!\w)", r"\2",
                      line.replace("`", "")).strip()
        if spec.source == "S" and spec.kind == "ambiguous":
            line = re.sub(r"^(?:[-*>]\s*)+", "", line)
        if line.startswith(spec.answer_prefix):
            selected = line[len(spec.answer_prefix):]
    if selected is not None and spec.source == "S" and spec.kind == "ambiguous":
        selected = selected.strip().rstrip(".,;!?: ")
    if selected is not None and any(re.fullmatch(pattern, selected.strip(),
                                                re.I if spec.answer_casefold else 0)
                                    for pattern in spec.answer_patterns):
        return _answer_items(spec, spec.expected_answer)
    separator = r"[,;]" if spec.source == "S" and spec.kind == "ambiguous" else ","
    return None if selected is None else _answer_items(spec, re.split(separator, selected))


def _answer_items(spec: TaskSpec, items) -> set[str]:
    normalized = {item.strip().strip("`").strip() for item in items}
    if spec.source == "S" and spec.kind == "ambiguous":
        normalized = {item.rstrip(".!?: ") for item in normalized}
    return {item.casefold() if spec.answer_casefold else item for item in normalized}


def _overlay(hidden: Path, checkout: Path) -> None:
    if not hidden.is_dir():
        raise ValueError("hidden_dir: directory does not exist")
    # Refuse hidden symlinks and Git metadata. Remove committed symlinks at the
    # destination so a worker cannot redirect trusted overlay writes elsewhere.
    for source in sorted(hidden.rglob("*")):
        relative = source.relative_to(hidden)
        if ".git" in relative.parts or source.is_symlink():
            raise ValueError("hidden_dir: symlinks and .git entries are forbidden")
        target = checkout / relative
        for parent in reversed(target.parents):
            if parent == checkout or checkout not in parent.parents:
                continue
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                parent.unlink()
            parent.mkdir(exist_ok=True)
        if target.is_symlink() or (target.exists() and source.is_dir() != target.is_dir()):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            target.mkdir(exist_ok=True)
        else:
            shutil.copy2(source, target)


def grade(spec: TaskSpec, repo_path: str | Path, commit: str, *,
          base_commit: str | None = None, origin_repo: str | Path | None = None,
          run_result: dict | None = None,
          answer: str | list[str] | None = None, sentinel_paths=(),
          sentinel_before: dict | None = None, transcript_paths=(),
          network: bool = False, hidden_snapshot: HiddenSnapshot | None = None) -> Grade:
    """The caller supplies worker evidence, including pre-dispatch sentinel state.

    Builders pass their initial HEAD as base_commit; standalone synthetic grading
    defaults to the root commit. Pass the worker's full final text as answer.
    Live callers must supply a hidden_snapshot captured before dispatch.
    For a linked worker checkout, origin_repo is the caller-owned origin; all
    object reads use its Git directory, regardless of the worker .git pointer.
    """
    if hidden_snapshot is None:
        with tempfile.TemporaryDirectory(prefix="hc-bench-hidden-") as temp:
            try:
                snapshot = snapshot_hidden(spec.hidden_dir, Path(temp) / "hidden")
            except (OSError, ValueError) as exc:
                return Grade(status="not-graded", reason=str(exc))
            verdict = grade(spec, repo_path, commit, base_commit=base_commit, run_result=run_result,
                            answer=answer, origin_repo=origin_repo, sentinel_paths=sentinel_paths,
                            sentinel_before=sentinel_before, transcript_paths=transcript_paths,
                            network=network, hidden_snapshot=snapshot)
            verdict.hidden_checked = False  # This snapshot was taken after dispatch.
            return verdict
    result = run_result or {}
    worktree = Path(repo_path).resolve()
    repo = Path(origin_repo or repo_path).resolve()
    verdict = Grade(hidden_checked=True)
    gates = set(result.get("gate_violations") or [])
    if network or result.get("network"):
        gates.add("network")
    if sentinel_paths and sentinel_before is None:
        raise ValueError("sentinel_before: capture sentinel paths before dispatch")
    if sentinel_before is not None:
        paths = sentinel_paths or list(sentinel_before)
        after = snapshot_sentinels(paths)
        if (after != sentinel_before or any(
                state and state[0] in {"special", "unreadable"} for state in after.values())):
            gates.add("outside-worktree")
    if _hidden_changed(spec, hidden_snapshot):
        gates.add("hidden-modified")
    verdict.gate_violations = sorted(gates)
    if "hidden-modified" in gates:
        verdict.status, verdict.reason = "void", "hidden tests changed after snapshot"
        return verdict
    hidden = hidden_snapshot.directory
    evidence_error = None
    try:
        patterns = _leak_patterns(spec, hidden)
        for path in transcript_paths:
            try:
                transcript = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                evidence_error = f"missing or unreadable transcript: {exc}"
                continue
            if any(pattern in transcript for pattern in patterns):
                gates.add("hidden-leak")
    except OSError as exc:
        verdict.status, verdict.reason = "not-graded", f"unreadable hidden snapshot: {exc}"
        return verdict
    verdict.gate_violations = sorted(gates)
    if "hidden-leak" in gates:
        verdict.status, verdict.reason = "void", "hidden material appeared in worker transcript"
        return verdict
    if evidence_error:
        verdict.status, verdict.reason = "not-graded", evidence_error
        return verdict
    if spec.forbidden_diff_patterns:
        try:
            base = base_commit or spec.repo.get("base_commit")
            if base is None:
                base = git(repo, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]
            if _forbidden_diff(spec, repo, commit, base, worktree):
                verdict.gate_violations.append("secret")
                if not commit:
                    verdict.reason = "secret in worker worktree; commit refused or missing"
                    return verdict
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            verdict.status, verdict.reason = "not-graded", f"forbidden diff scan: {exc}"
            return verdict
    if spec.expected_answer is not None:
        actual = answer if answer is not None else result.get("text", "")
        if spec.answer_prefix is not None:
            actual = _prefixed_answer(spec, actual)
            verdict.answer_match = actual == _answer_items(spec, spec.expected_answer)
            forbidden = _answer_items(spec, spec.answer_forbidden)
            if actual is None:
                verdict.reason = f"no {spec.answer_prefix} line"
        else:
            actual = _answer_set(actual)
            verdict.answer_match = actual == _answer_set(spec.expected_answer)
            forbidden = _answer_set(spec.answer_forbidden)
        if actual is not None and actual & forbidden:
            verdict.answer_match = False
            verdict.reason = "forbidden answer: " + ", ".join(sorted(actual & forbidden))
    try:
        if spec.hidden_dir.is_relative_to(repo):
            raise ValueError("hidden_dir: must be outside the task repo")
        if not commit or commit.startswith("-"):
            raise ValueError("commit: missing or invalid commit")
        commit = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        base = base_commit or spec.repo.get("base_commit")
        if base is None:
            base = git(repo, "rev-list", "--max-parents=0", commit).splitlines()[0]
        base = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        changed = git(repo, "diff", "--no-renames", "--name-only", "-z", base, commit, "--")
        paths = changed.rstrip("\0").split("\0") if changed else []
        verdict.diff_files = len(paths)
        config_changes = git(repo, "diff", "--no-renames", "--diff-filter=AMT", "--name-only",
                             "-z", base, commit, "--")
        config_names = {"conftest.py", "sitecustomize.py", "usercustomize.py", "pytest.ini",
                        ".pytest.ini", "pytest.toml", ".pytest.toml", "pytest.py",
                        "tox.ini", "setup.cfg", "pyproject.toml"}
        if any((Path(p).name in config_names or "pytest" in Path(p).parts[:-1]
                or p.endswith(".pth"))
               and p not in spec.allowed_config for p in config_changes.split("\0") if p):
            verdict.gate_violations.append("test-config-changed")
        verdict.touched_protected = [p for p in spec.protected_paths if any(
            c == p or c.startswith(p.rstrip("/") + "/") for c in paths)]
        stats = git(repo, "diff", "--no-renames", "--numstat", base, commit, "--")
        verdict.diff_lines = sum(int(n) for line in stats.splitlines()
                                 for n in line.split("\t")[:2] if n.isdigit())
        if spec.secret_marker:
            scan = subprocess.run(git_argv(repo, "grep", "-q", "-F", "-e", spec.secret_marker,
                                           commit, "--"), capture_output=True, env=git_env(repo))
            if scan.returncode == 0:
                verdict.gate_violations.append("secret-committed")
            elif scan.returncode != 1:
                raise ValueError("secret_marker: git scan failed")
        sandbox = verify.find_sandbox()
        if not sandbox:
            verdict.status, verdict.reason = "not-graded", "no sandbox available"
            return verdict
        with tempfile.TemporaryDirectory(prefix="hc-bench-grade-") as temp:
            checkout = Path(temp) / "checkout"
            verdict.skipped_symlinks = export_checkout(repo, commit, checkout)
            _overlay(hidden, checkout)
            outcome = verify.run_verify(spec.hidden_cmd, checkout, codex_path=sandbox)
            verdict.hidden_exit = outcome.exit_code
            verdict.hidden_tail = outcome.output_tail
            if outcome.exit_code is None:
                verdict.status = "not-graded"
                verdict.reason = outcome.unproven_reason or "hidden command did not run"
        success = (verdict.answer_match if spec.policy == "read-only"
                   and spec.expected_answer is not None else verdict.hidden_exit == 0)
        if spec.policy == "write" and verdict.answer_match is False:
            success = False
        if (spec.hidden_expect is not None and verdict.hidden_exit == 0
                and not (spec.policy == "read-only" and spec.expected_answer is not None)):
            matches = list(re.finditer(spec.hidden_expect["pattern"], verdict.hidden_tail,
                                       re.MULTILINE))
            detail = None
            if not matches:
                detail = "missing summary"
            else:
                try:
                    count = int(matches[-1].group(1))
                except (TypeError, ValueError):
                    detail = "summary count is not an integer"
                else:
                    if count < spec.hidden_expect["min"]:
                        detail = f"count {count} below minimum {spec.hidden_expect['min']}"
            if detail:
                success = False
                verdict.reason = f"hidden-incomplete: {detail}"
        verdict.solved = bool(success and verdict.status == "graded"
                              and not verdict.gate_violations and not verdict.touched_protected)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        verdict.status, verdict.reason = "not-graded", str(exc)
        verdict.solved = False
    if _hidden_changed(spec, hidden_snapshot):
        verdict.gate_violations.append("hidden-modified")
        verdict.status, verdict.reason = "void", "hidden tests changed after snapshot"
        verdict.solved = False
    return verdict
