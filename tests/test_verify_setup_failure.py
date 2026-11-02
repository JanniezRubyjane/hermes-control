"""Failed setup stays visible to every consumer, using disposable runtimes."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "Hermes repo's checkout"
    root.mkdir()
    (root / "hermes_cli.py").touch()
    site = tmp_path / "managed env/site-packages"
    site.mkdir(parents=True)
    (site / "__editable___hermes_agent_test_finder.py").write_text(
        "MAPPING = {'hermes_cli': 'ignored'}\n"
        "raise RuntimeError('finder must never execute')\n")
    (root / "hermes_bootstrap.py").write_text(
        f"import sys\nsys.path.insert(0, {str(site)!r})\n")
    bindir = tmp_path / "bin"
    python = executable(tmp_path / "bundled/bin/python3",
                        "exec " + shlex.quote(sys.executable) + ' -S "$@"\n')
    executable(bindir / "python3",
               "exec " + shlex.quote(sys.executable) + ' -I -S "$@"\n')
    executable(bindir / "hermes", 'case "$1" in\n'
               ' --print-runtime-command) printf "%s\\n" "$RUNTIME_ARGV";;\n'
               ' --version) printf "Install directory: %s\\n" "$RUNTIME_ROOT";;\n'
               ' update) exit 0;;\nesac\n')
    executable(bindir / "uv", 'while [ "$1" != --target ]; do shift; done\n'
               'mkdir -p "$2"\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "HERMES_HOME": str(tmp_path / "hermes-home"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{bindir}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "RUNTIME_ROOT": str(root), "RUNTIME_ARGV": json.dumps([str(python), "-I"])}
    for name in ("HERMES_PYTHON", "HERMES_PYTHON_PATH", "PYTHONPATH",
                 "CODEX_SANDBOX", "HERMES_CONTROL_CHECK_COPY"):
        env.pop(name, None)
    return env, root, site, python


def run(script, env, *args, cwd=None):
    shell = "/bin/sh" if script.name == "sandbox-check.sh" else "/bin/bash"
    return subprocess.run([shell, str(script), *args], env=env,
                          text=True, capture_output=True, timeout=10, cwd=cwd)


def recorded_python(dest, tmp_path, name):
    (dest / "site").mkdir(parents=True)
    marker = tmp_path / f"{name}-calls"
    python = executable(tmp_path / f"{name}-python",
                        "echo called >> " + shlex.quote(str(marker)) + "\n")
    (dest / "python-path").write_text(f"{python}\n")
    return marker


def test_update_reports_setup_failure_and_skips_check(runtime, tmp_path):
    env, _, _, _ = runtime
    repo = tmp_path / "consumer repo"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/hc-update", scripts / "hc-update")
    shutil.copy2(REPO / "scripts/lib/hermes-python.sh", scripts / "lib/hermes-python.sh")
    executable(scripts / "verify-setup.sh", 'echo setup-broken >&2\nexit 2\n')
    marker = tmp_path / "check-calls"
    executable(scripts / "check.sh", "touch " + shlex.quote(str(marker)) + "\n")
    result = run(scripts / "hc-update", env)
    assert result.returncode == 2, result.stderr
    assert "setup-broken" in result.stderr
    assert not marker.exists()
    assert (
        "hc-update: verify setup failed (exit 2) after Hermes was updated; "
        f"see the verify-setup lines above and Troubleshooting in {repo}/README.md"
    ) in result.stderr


@pytest.mark.parametrize("state", ["failed-setup", "agy-copy", "never-set-up"])
def test_sandbox_selects_home_by_verify_directory(runtime, tmp_path, state):
    env, _, _, _ = runtime
    fallback = Path(env["HOME"]) / ".hermes/control/verify"
    fallback_calls = recorded_python(fallback, tmp_path, "fallback")
    if state != "never-set-up":
        (Path(env["HERMES_HOME"]) / "control/verify/site").mkdir(parents=True)
    if state == "agy-copy":
        env["HERMES_CONTROL_CHECK_COPY"] = "1"
        copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
        copy_calls = recorded_python(copy, tmp_path, "copy")
    result = run(REPO / "scripts/sandbox-check.sh", env)
    if state == "failed-setup":
        assert result.returncode == 2, result.stderr
        assert "not set up" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
    assert fallback_calls.exists() == (state == "never-set-up")
    if state == "agy-copy":
        assert copy_calls.exists()
        assert "sandbox-check: using agy copy" in result.stderr


@pytest.mark.parametrize("kind", ["read-only", "symlink", "dangling-symlink", "no-agy-copy"])
@pytest.mark.parametrize("home", ["unset", "symlink"])
def test_failed_farm_invalidates_previous_agy_copy(runtime, tmp_path, kind, home):
    env, root, site, python = runtime
    default_home = Path(env["HOME"]) / ".hermes"
    if home == "unset":
        env.pop("HERMES_HOME")
    else:
        default_home.mkdir(parents=True)
        alias = tmp_path / "default home alias"
        alias.symlink_to(default_home, target_is_directory=True)
        env["HERMES_HOME"] = f"{alias}/"
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    dest = Path(env.get("HERMES_HOME", default_home)) / "control/verify"
    (dest / "hermes-src").mkdir(parents=True)
    (dest / "hermes-src/stale").touch()
    (dest / "hermes-path").write_text(f"{root}\n{site}\n")
    (dest / "python-path").write_text(f"{python}\n")
    copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
    target = tmp_path / "copy target"
    if kind in ("symlink", "dangling-symlink"):
        copy.parent.mkdir(parents=True)
        copy.symlink_to(target, target_is_directory=True)
        tree = target
    else:
        tree = copy
    if kind != "dangling-symlink":
        calls = recorded_python(tree, tmp_path, "stale-copy")
        (tree / "site/previous").write_text("previous Hermes")
        (tree / "stamp").write_text(json.dumps({"hermes_python": "old Hermes Python"}))
    if kind == "read-only":
        for entry in tree.rglob("*"):
            entry.chmod(entry.stat().st_mode & ~0o222)
        tree.chmod(tree.stat().st_mode & ~0o222)
    args = ["--no-agy-copy"] if kind == "no-agy-copy" else []
    try:
        result = run(REPO / "scripts/verify-setup.sh", env, *args)
        assert result.returncode == 2, result.stderr
        assert "verify-setup: hermes-src-farm: no editable Hermes finder" in result.stderr
        assert "verify-setup: Hermes source farm failed" in result.stderr
        assert all(not (dest / name).exists()
                   for name in ("python-path", "hermes-path", "hermes-src"))
        if kind == "no-agy-copy":
            assert (copy / "site/previous").read_text() == "previous Hermes"
            assert (
                f"verify-setup: --no-agy-copy: the agy copy at {copy} was left in place "
                "and was built for the previous Hermes"
            ) in result.stderr
        else:
            assert not copy.exists() and not copy.is_symlink()
            assert (
                f"verify-setup: removed the agy copy at {copy} because it was built for "
                'the previous Hermes; agy workers get "not set up" until setup succeeds'
            ) in result.stderr
            if kind == "symlink":
                assert (target / "site/previous").read_text() == "previous Hermes"
                assert (target / "python-path").is_file()
            env["HERMES_CONTROL_CHECK_COPY"] = "1"
            result = run(REPO / "scripts/sandbox-check.sh", env)
            assert result.returncode == 2, result.stderr
            assert "not set up" in result.stderr
        if kind != "dangling-symlink":
            assert not calls.exists()
    finally:
        # Let pytest clean up even when the initial regression assertion fails.
        if kind == "read-only" and tree.exists():
            tree.chmod(0o755)
            for entry in tree.rglob("*"):
                entry.chmod(entry.stat().st_mode | 0o200)


def test_failed_farm_reports_copy_removal_failure_and_keeps_exit_two(runtime, tmp_path):
    env, _, site, _ = runtime
    env["HERMES_HOME"] = str(Path(env["HOME"]) / ".hermes")
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
    recorded_python(copy, tmp_path, "stale-copy")
    env["COPY_TO_KEEP"] = str(copy)
    executable(tmp_path / "bin/rm", 'for arg do\n'
               ' if [ "$arg" = "$COPY_TO_KEEP" ]; then exit 7; fi\ndone\n'
               'exec /bin/rm "$@"\n')
    result = run(REPO / "scripts/verify-setup.sh", env)
    assert result.returncode == 2, result.stderr
    assert copy.exists()
    assert (
        f"verify-setup: could not remove the agy copy at {copy}; remove it by hand"
    ) in result.stderr


@pytest.mark.parametrize("default_exists", [False, True])
@pytest.mark.parametrize("no_agy_copy", [False, True])
@pytest.mark.parametrize("stamp", ["other", "missing", "malformed", "missing-key",
                                  "invalid-shape", "unreadable"])
def test_failed_farm_in_other_home_preserves_shared_agy_copy(
    runtime, tmp_path, default_exists, no_agy_copy, stamp,
):
    env, _, site, _ = runtime
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    default_home = Path(env["HOME"]) / ".hermes"
    if default_exists:
        recorded_python(default_home / "control/verify", tmp_path, "default")
    copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
    calls = recorded_python(copy, tmp_path, "shared-copy")
    (copy / "site/previous").write_text("previous Hermes")
    stamp_value = str(tmp_path / "another Hermes/bin/python")
    if stamp == "other":
        (copy / "stamp").write_text(json.dumps({"hermes_python": stamp_value}))
    elif stamp == "malformed":
        (copy / "stamp").write_text("{broken json")
    elif stamp == "missing-key":
        (copy / "stamp").write_text("{}")
    elif stamp == "invalid-shape":
        (copy / "stamp").write_text("[]")
    elif stamp == "unreadable":
        (copy / "stamp").mkdir()
    args = ["--no-agy-copy"] if no_agy_copy else []
    result = run(REPO / "scripts/verify-setup.sh", env, *args)
    assert result.returncode == 2, result.stderr
    assert "verify-setup: Hermes source farm failed" in result.stderr
    assert (copy / "site/previous").read_text() == "previous Hermes"
    assert (copy / "python-path").is_file()
    assert not calls.exists()
    if no_agy_copy:
        message = (
            f"verify-setup: --no-agy-copy: the agy copy at {copy} was left in place "
            "and was built for the previous Hermes"
        )
    else:
        message = (
            f"verify-setup: the agy copy at {copy} was left in place because "
            f"HERMES_HOME ({env['HERMES_HOME']}) is not the default home "
            f"{default_home} and the copy was built for another Hermes Python "
            f"({stamp_value if stamp == 'other' else 'unknown'})"
        )
    assert message in result.stderr.splitlines()


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("no_agy_copy", [False, True])
def test_failed_farm_in_other_home_removes_copy_built_by_same_python(
    runtime, tmp_path, symlink, no_agy_copy,
):
    env, _, site, python = runtime
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
    tree = tmp_path / "copy target" if symlink else copy
    calls = recorded_python(tree, tmp_path, "shared-copy")
    (tree / "site/previous").write_text("previous Hermes")
    (tree / "stamp").write_text(json.dumps({"hermes_python": str(python)}))
    if symlink:
        copy.parent.mkdir(parents=True)
        copy.symlink_to(tree, target_is_directory=True)
    args = ["--no-agy-copy"] if no_agy_copy else []
    result = run(REPO / "scripts/verify-setup.sh", env, *args)
    assert result.returncode == 2, result.stderr
    assert "verify-setup: Hermes source farm failed" in result.stderr
    if no_agy_copy:
        assert (copy / "site/previous").read_text() == "previous Hermes"
        assert (
            f"verify-setup: --no-agy-copy: the agy copy at {copy} was left in place "
            "and was built for the previous Hermes"
        ) in result.stderr.splitlines()
    else:
        assert not copy.exists() and not copy.is_symlink()
        assert (
            f"verify-setup: removed the agy copy at {copy} because it was built for "
            'the previous Hermes; agy workers get "not set up" until setup succeeds'
        ) in result.stderr.splitlines()
    if symlink:
        assert (tree / "site/previous").read_text() == "previous Hermes"
        assert (tree / "stamp").is_file()
        assert (tree / "python-path").is_file()
    assert not calls.exists()


def test_failed_farm_recognizes_relative_default_home_with_cdpath(runtime, tmp_path):
    env, _, site, _ = runtime
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    default_home = Path(env["HOME"]) / ".hermes"
    default_home.mkdir(parents=True)
    decoy = tmp_path / "cdpath/.hermes"
    decoy.mkdir(parents=True)
    env["CDPATH"] = str(decoy.parent)
    env["HERMES_HOME"] = ".hermes"
    copy = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
    calls = recorded_python(copy, tmp_path, "shared-copy")
    result = run(REPO / "scripts/verify-setup.sh", env, cwd=env["HOME"])
    assert result.returncode == 2, result.stderr
    assert not copy.exists()
    assert f"verify-setup: removed the agy copy at {copy}" in result.stderr
    assert result.stdout == ""
    assert not (decoy / "control").exists()
    assert not calls.exists()


def test_verify_setup_installs_pyyaml_into_overlay_for_hermes_without_yaml(runtime, tmp_path):
    env, _, _, _ = runtime
    executable(
        tmp_path / "bin/uv",
        'target=""\nhas_pytest=false\nhas_pyyaml=false\n'
        'while [ "$#" -gt 0 ]; do\n'
        ' case "$1" in\n'
        '  --target) target="$2"; shift 2 ;;\n'
        '  pytest==9.1.1) has_pytest=true; shift ;;\n'
        '  PyYAML==6.0.3) has_pyyaml=true; shift ;;\n'
        '  *) shift ;;\n'
        ' esac\n'
        'done\n'
        'mkdir -p "$target"\n'
        'if [ "$has_pytest" = true ]; then\n'
        ' printf "import yaml\\nassert yaml.__version__ == \\"6.0.3\\"\\n" > "$target/pytest.py"\n'
        'fi\n'
        'if [ "$has_pyyaml" = true ]; then\n'
        ' mkdir -p "$target/yaml"\n'
        ' printf "__version__ = \\"6.0.3\\"\\n" > "$target/yaml/__init__.py"\n'
        'fi\n',
    )
    setup = run(REPO / "scripts/verify-setup.sh", env, "--no-agy-copy")
    assert setup.returncode == 0, setup.stderr
    check = run(REPO / "scripts/sandbox-check.sh", env)
    assert check.returncode == 0, check.stderr
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    assert f"verify-setup: pytest 9.1.1 and PyYAML 6.0.3 in {dest}/site" in setup.stdout
    assert (dest / "site/yaml/__init__.py").is_file()
