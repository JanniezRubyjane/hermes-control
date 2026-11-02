"""Build the offline toolchain Gemini can read, using only the Hermes interpreter.

Invoked by verify-setup.sh after its existing pytest install. Editable finder
code is parsed without execution; secret rules come from the plugin.
"""

import argparse
import ast
from datetime import datetime, timezone
from importlib import machinery, metadata
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile


def excluded(name):
    return (name in {".git", "tests", "node_modules", "__pycache__"}
            or name.startswith(".env") or name.endswith(".env"))


HERMES_EXTENSIONS = {".py", ".pyi", ".so", ".dylib", ".json", ".yaml", ".yml",
                     ".toml", ".txt", ".md", ".typed"}


def copy(source, target, *, extensions=None):
    """Dereference links: the result must not need a path denied by agy's sandbox."""
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            # Exclusions apply below this copy root, never to its ancestors or
            # the ancestors of a dereferenced symlink target.
            if not excluded(child.name):
                copy(child, target / child.name, extensions=extensions)
    elif source.is_file() and (extensions is None or source.suffix in extensions):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def remove(path):
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        # Previous successful builds are read-only, including their directories.
        for directory, dirs, files in os.walk(path):
            directory = Path(directory)
            directory.chmod(directory.stat().st_mode | stat.S_IRWXU)
            for name in files:
                child = directory / name
                if not child.is_symlink():
                    child.chmod(child.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(path)


def python_config(*, allow_standalone=False):
    config = Path(sys.prefix) / "pyvenv.cfg"
    if not config.is_file() and not allow_standalone:
        raise ValueError("Hermes Python needs a venv pyvenv.cfg to copy its base CPython; "
                         "use --no-agy-copy to install only primary verify")
    if config.is_file():
        values = dict(line.split("=", 1) for line in config.read_text().splitlines() if "=" in line)
        values = {key.strip(): value.strip() for key, value in values.items()}
        # uv's home is <base>/bin; resolve the DIRECTORY rather than the venv launcher.
        base = Path(values["home"]).parent.resolve()
        version = values.get("version", sys.version.split()[0])
    else:
        base = Path(sys.prefix).resolve()
        version = sys.version.split()[0]
    interpreter = f"python{'.'.join(version.split('.')[:2])}"
    if not (base / "bin" / interpreter).is_file():
        raise ValueError(f"base CPython is missing bin/{interpreter}")
    return base, version, interpreter


def third_party(sites, target):
    names = [line.partition("#")[0].strip() for line in
             Path(__file__).with_name("agy-check-packages.txt").read_text().splitlines()]
    names = {name for name in names if name}
    missing = []
    for name in sorted(names):
        if not name.isidentifier():
            raise ValueError(f"invalid third-party import root: {name}")
        spec = machinery.PathFinder.find_spec(name, sites)
        if spec is None:
            missing.append(name)
            continue
        if spec.submodule_search_locations is not None:
            for location in spec.submodule_search_locations:
                copy(Path(location), target / name)
        elif spec.origin:
            source = Path(spec.origin)
            copy(source, target / source.name)
    if missing:
        raise ValueError("listed third-party package(s) missing from Hermes site-packages: "
                         + ", ".join(missing) + "; refresh the Hermes environment or "
                         "scripts/agy-check-packages.txt (see docs/usage.md)")

    # Distribution names need not match imports (e.g. PyYAML -> yaml). RECORD also
    # covers namespace packages and distributions without top_level.txt.
    for site in sites:
        for info in Path(site).glob("*.dist-info"):
            distribution = metadata.Distribution.at(info)
            roots = set((distribution.read_text("top_level.txt") or "").split())
            for file in distribution.files or ():
                top = file.parts[0]
                roots.add(top)
                if top.endswith(".py"):
                    roots.add(top[:-3])
                for suffix in machinery.EXTENSION_SUFFIXES:
                    if top.endswith(suffix):
                        roots.add(top[:-len(suffix)])
            if roots & names:
                copy(info, target / info.name)


def import_source(stem):
    if stem.is_dir():
        return stem
    return next((Path(str(stem) + suffix) for suffix in machinery.all_suffixes()
                 if Path(str(stem) + suffix).is_file()), None)


def hermes_sources(sites, target, *, root=None):
    mappings = {}
    for site in sites:
        for finder in sorted(Path(site).glob("__editable___hermes_agent_*_finder.py")):
            for node in ast.parse(finder.read_text()).body:
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                else:
                    continue
                if any(isinstance(item, ast.Name) and item.id == "MAPPING" for item in targets):
                    mappings.update(ast.literal_eval(node.value))
    if not mappings:
        raise ValueError("Hermes editable finder __editable___hermes_agent_*_finder.py "
                         "has no MAPPING in site-packages")
    roots = set()
    for name, location in sorted(mappings.items()):
        if not all(part.isidentifier() for part in name.split(".")):
            raise ValueError(f"invalid Hermes editable import name: {name}")
        if root is not None:
            stem = root.joinpath(*name.split("."))
            source = import_source(stem)
            if source is None:
                if not Path(location).exists() and import_source(Path(location)) is None:
                    continue
                raise ValueError(f"Hermes editable package is missing: {name}")
        else:
            source = Path(location)
        if not source.exists():
            # Editable finders record module stems as well as package directories.
            source = next((Path(location + suffix) for suffix in machinery.all_suffixes()
                           if Path(location + suffix).is_file()), None)
            if source is None:
                raise ValueError(f"Hermes editable package is missing: {name}")
        destination = target.joinpath(*name.split("."))
        if source.is_file():
            if root is not None:
                destination = destination.with_name(destination.name + source.name[len(stem.name):])
            else:
                destination = destination.with_suffix(source.suffix)
        copy(source, destination, extensions=(None if root is not None and source.is_file()
                                             else HERMES_EXTENSIONS))
        roots.add(source.parent)
    # MAPPING is generated at install time and can miss newly added root modules.
    # Hermes discovers every root *.py dynamically; mirror that project layout.
    projects = {root} if root is not None else set()
    if root is None:
        for source_root in roots:
            project = next((parent for parent in (source_root, *source_root.parents)
                            if (parent / "pyproject.toml").is_file()), None)
            if project is not None:
                projects.add(project)
    for project in sorted(projects):
        for module in sorted(project.glob("*.py")):
            if module.stem in {"tests", "scripts"}:
                continue  # Explicitly mapped modules were already copied above.
            if module.is_file() and not excluded(module.name):
                copy(module, target / module.name, extensions=HERMES_EXTENSIONS)
    return {root} if root is not None else roots


def hermes_head(roots):
    for root in sorted(roots):
        try:
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return result.stdout.strip()
    return None


# This scan belongs to the staged toolchain build; it never prints matched bytes.
SECRET_RULES = {
    "private-key": rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "aws-key": rb"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "github-token": rb"gh[pousr]_[A-Za-z0-9]{20,}",
    "github-pat": rb"github_pat_[A-Za-z0-9_]{20,}",
    "slack-token": rb"xox[abprs]-[A-Za-z0-9-]{10,}",
    "google-api-key": rb"AIza[A-Za-z0-9_-]{35}",
}


def scan(staging):
    # Running a helper from scripts/ does not otherwise put the repo on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from hermes_control import secret_scan

    rules = {name: re.compile(pattern) for name, pattern in SECRET_RULES.items()}
    found = False
    for file in sorted(staging.rglob("*")):
        if file.is_file():
            content = file.read_bytes()
            matches = {name for name, rule in rules.items() if rule.search(content)}
            if b"\0" not in content:
                text = content.decode("utf-8", errors="replace")
                matches.update(name for name, rule in secret_scan._RULES if rule.search(text))
            for name in sorted(matches):
                print(f"{file.relative_to(staging)}: {name}", file=sys.stderr)
                found = True
    if found:
        raise ValueError("agy copy rejected by secret scan; staged copy discarded")


def smoke_test(staging, interpreter):
    env = {**os.environ, "PYTHONPATH": f"{staging}/site:{staging}/src",
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONHOME", None)
    try:
        result = subprocess.run(
            [str(staging / "python/bin" / interpreter), "-c",
             "import pytest, yaml, fastapi, hermes_cli.config, hermes_state, hermes_state_common"],
            env=env, cwd=staging, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("agy copy smoke test failed: staged Python could not import "
                         "pytest, yaml, fastapi, hermes_cli.config, hermes_state and hermes_state_common") from None
    if result.returncode:
        raise ValueError("agy copy smoke test failed: staged Python could not import "
                         "pytest, yaml, fastapi, hermes_cli.config, hermes_state and hermes_state_common")


def promote(staging, destination):
    # Both renames stay on the destination filesystem. Keep the previous copy
    # intact until the fully built, scanned and smoke-tested staging can replace it.
    previous = None
    if destination.exists() or destination.is_symlink():
        previous = Path(tempfile.mkdtemp(prefix=".check-old-", dir=destination.parent))
        previous.rmdir()
        destination.rename(previous)
    try:
        staging.rename(destination)
    except OSError:
        if previous is not None:
            previous.rename(destination)
        raise
    if previous is not None:
        remove(previous)


def build(args):
    destination = args.destination.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".check-", dir=destination.parent))
    try:
        root = None
        if args.hermes_path is not None:
            paths = args.hermes_path.read_text().splitlines()
            if not paths or not Path(paths[0]).is_dir():
                raise ValueError("hermes-path must start with the Hermes repo directory")
            root = Path(paths[0])
            sites = list(dict.fromkeys(path for path in paths[1:]
                                       if Path(path).name == "site-packages" and Path(path).is_dir()))
        else:
            sites = list(dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib")))
        base, version, interpreter = python_config(allow_standalone=root is not None)
        for name in ("python", "site", "src"):
            (staging / name).mkdir()
        copy(base, staging / "python")
        copy(args.verify / "site", staging / "site")
        third_party(sites, staging / "site")
        roots = hermes_sources(sites, staging / "src", root=root)
        (staging / "python-path").write_text(str(destination / "python/bin" / interpreter) + "\n")
        stamp = {"hermes_python": args.hermes_python, "base_python_version": version,
                 "hermes_agent_head": hermes_head(roots),
                 "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        (staging / "stamp").write_text(json.dumps(stamp, indent=2) + "\n")
        scan(staging)
        smoke_test(staging, interpreter)
        # Preserve executable bits for CPython and shared libraries, remove all writes.
        for path in [*staging.rglob("*"), staging]:
            path.chmod(path.stat().st_mode & ~0o222)
        promote(staging, destination)
    finally:
        remove(staging)
    print(f"verify-setup: read-only agy copy in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-python", required=True)
    parser.add_argument("--verify", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--hermes-path", type=Path)
    try:
        build(parser.parse_args())
    except (OSError, ValueError, KeyError, SyntaxError) as error:
        print(f"verify-setup: {error}", file=sys.stderr)
        sys.exit(1)
