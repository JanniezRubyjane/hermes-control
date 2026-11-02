#!/usr/bin/env python3
"""Expose Hermes source modules without unrelated repo-root packages."""

import argparse
import ast
import importlib.machinery
import os
from pathlib import Path
import shutil
import sys
import tempfile


def mapping_names(finder):
    for node in ast.parse(finder.read_text(), filename=str(finder)).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "MAPPING" for target in targets):
            mapping = ast.literal_eval(node.value)
            if not isinstance(mapping, dict):
                raise ValueError(f"MAPPING is not a dict in finder {finder}")
            return mapping.keys()
    return None


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def build(recorded, out):
    lines = recorded.read_text().splitlines()
    root = Path(lines[0]).resolve() if lines and lines[0] else None
    if root is None or not root.is_dir():
        raise ValueError("Hermes repo root is not a directory")
    names = set()
    found = False
    for entry in lines[1:]:
        for finder in sorted(Path(entry).glob("__editable___hermes_agent_*_finder.py")):
            keys = mapping_names(finder)
            if keys is not None:
                found = True
                names.update(name for name in keys if isinstance(name, str) and name
                             and "." not in name and Path(name).name == name)
    if not found:
        raise ValueError("no editable Hermes finder with MAPPING found in hermes-path lines 2..")
    links = {}
    for name in sorted(names):
        if (root / name).is_dir():
            links[name] = root / name
        else:
            for suffix in importlib.machinery.all_suffixes():
                module = root / (name + suffix)
                if module.is_file():
                    links[module.name] = module
                    break
            else:
                print(f"hermes-src-farm: warning: MAPPING name {name} not found under the repo root",
                      file=sys.stderr)
    for module in sorted(root.glob("*.py")):
        if module.stem in {"tests", "scripts"} and module.stem not in names:
            continue
        if module.is_file():
            links.setdefault(module.name, module)

    staging = out.with_name(out.name + ".new")
    remove(staging)
    staging.mkdir(parents=True)
    try:
        for name, source in links.items():
            (staging / name).symlink_to(source, target_is_directory=source.is_dir())
        # Keep the previous farm available for rollback until the replacement
        # succeeds; rename cannot replace a nonempty directory directly.
        with tempfile.TemporaryDirectory(prefix=out.name + ".old-", dir=out.parent) as backup:
            previous = Path(backup) / "farm"
            if out.exists() or out.is_symlink():
                os.replace(out, previous)
            try:
                os.replace(staging, out)
            except OSError:
                if previous.exists() or previous.is_symlink():
                    os.replace(previous, out)
                raise
    finally:
        remove(staging)
    return len(links)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-path", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build(args.hermes_path, args.out))
    except (OSError, ValueError, SyntaxError) as error:
        print(f"hermes-src-farm: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
