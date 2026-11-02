"""Managed runtime discovery uses only disposable launchers and Python wrappers."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


@pytest.fixture
def runtime(tmp_path):
    bindir = tmp_path / "bin"
    root = tmp_path / "Hermes repo's checkout"
    root.mkdir()
    site = tmp_path / "managed env/site-packages"
    extra = tmp_path / "extra imports"
    site.mkdir(parents=True)
    (site / "__editable___hermes_agent_test_finder.py").write_text(
        "MAPPING = {'hermes_cli': '/ignored'}\n")
    extra.mkdir()
    (root / "hermes_cli.py").write_text("ORIGIN = 'checkout'\n")
    (root / "hermes_bootstrap.py").write_text(
        "import sys\n"
        f"sys.path[:0] = {[str(root), str(site), str(extra), '', '__editable__.hook', str(tmp_path / 'missing')]!r}\n"
        "sys.path.append(sys.path[-1])\n")
    python = executable(tmp_path / "bundled/bin/python3", "#!/bin/sh\nexec " +
                        shlex.quote(sys.executable) + ' -S "$@"\n')
    old = executable(root / "venv/bin/python", "#!/bin/sh\nexit 0\n")
    launcher = executable(bindir / "hermes", "#!/bin/sh\n"
                          'printf "%s\\n" "$1" >> "$RUNTIME_CALLS"\n'
                          'case "$1" in\n'
                          ' --print-runtime-command)\n'
                          '  case "$RUNTIME_MODE" in\n'
                          '   fail) exit 7;;\n'
                          '   nonjson) echo unsupported; exit 0;;\n'
                          '   hang) sleep 30; exit 0;;\n'
                          '  esac\n'
                          '  printf "%s\\n" "$RUNTIME_ARGV";;\n'
                          ' --version) printf "Install directory: %s\\n" "$RUNTIME_ROOT"\n'
                          '  if [ "$RUNTIME_MODE" = versionfail ]; then exit 9; fi;;\n'
                          'esac\n')
    executable(bindir / "uv", '#!/bin/sh\nwhile [ "$1" != --target ]; do shift; done\nmkdir -p "$2"\n')
    # Supply a parser without depending on the host's PATH or its Hermes launcher.
    executable(bindir / "python3", "#!/bin/sh\nexec " +
               shlex.quote(sys.executable) + ' -I -S "$@"\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "HERMES_HOME": str(tmp_path / "hermes-home"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{bindir}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "RUNTIME_CALLS": str(tmp_path / "calls"), "RUNTIME_MODE": "ok",
           "RUNTIME_ROOT": str(root), "RUNTIME_ARGV": json.dumps(
               [str(python), "-I", "-c", "raise RuntimeError('do not execute launcher code')"])}
    env.pop("HERMES_PYTHON", None)
    env.pop("HERMES_PYTHON_PATH", None)
    env.pop("PYTHONPATH", None)
    return env, python, old, [str(root), str(site), str(extra)], launcher


def resolve(env):
    return subprocess.run(["bash", "-eu", "-c",
                           'source "$1"; resolve_hermes_python; '
                           'printf "%s\\n%s\\n" "$HERMES_PYTHON" "${HERMES_PYTHON_PATH-unset}"',
                           "test", str(REPO / "scripts/lib/hermes-python.sh")],
                          env=env, text=True, capture_output=True, timeout=9)


def test_runtime_python_and_filtered_import_path(runtime):
    env, python, _, paths, _ = runtime
    result = resolve(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(python), ":".join(paths)]
    # The chosen Python alone cannot import Hermes: discovery must bootstrap it.
    probe = subprocess.run([str(python), "-I", "-c", "import hermes_cli"],
                           env=env, capture_output=True)
    assert probe.returncode != 0


@pytest.mark.parametrize("mode", ["fail", "nonjson", "hang", "badargv", "badbootstrap", "versionfail"])
def test_runtime_failure_falls_back_with_empty_path(runtime, mode):
    env, _, old, paths, _ = runtime
    env["HERMES_PYTHON_PATH"] = "stale:path"
    env["RUNTIME_MODE"] = mode
    if mode == "badargv":
        env["RUNTIME_ARGV"] = json.dumps(["/missing/python"])
    if mode == "badbootstrap":
        (Path(paths[0]) / "hermes_bootstrap.py").write_text("raise RuntimeError('broken')\n")
    if mode == "versionfail":
        # A nonzero version exit is fatal only without a usable install directory.
        env["RUNTIME_ROOT"] = ""
    result = resolve(env)
    if mode in ("badbootstrap", "versionfail"):
        assert result.returncode != 0
        step = "bootstrap" if mode == "badbootstrap" else "Install directory"
        assert step in result.stderr
        assert "HERMES_PYTHON_PATH" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(old), ""]


def test_explicit_python_wins_and_optional_path_is_preserved(runtime):
    env, _, old, paths, _ = runtime
    root = Path(paths[0])
    (root / "hermes_cli").mkdir()
    env["HERMES_PYTHON"] = str(old)
    for supplied in (None, f"{root}:user:imports"):
        if supplied is not None:
            env["HERMES_PYTHON_PATH"] = supplied
        result = resolve(env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(old), supplied or ""]
    assert not Path(env["RUNTIME_CALLS"]).exists()


def test_verify_setup_records_runtime_paths_and_removes_stale_file(runtime):
    env, python, old, paths, _ = runtime
    command = ["bash", str(REPO / "scripts/verify-setup.sh"), "--no-agy-copy"]
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    assert (dest / "python-path").read_text() == f"{python}\n"
    assert (dest / "hermes-path").read_text().splitlines() == paths
    assert str(python) in result.stdout and "3 path entries" in result.stdout
    env["RUNTIME_MODE"] = "fail"
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (dest / "python-path").read_text() == f"{old}\n"
    assert not (dest / "hermes-path").exists()
    assert "0 path entries" in result.stdout


def test_sandbox_pythonpath_with_and_without_recorded_paths(runtime, tmp_path):
    env, _, _, paths, _ = runtime
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    (dest / "site").mkdir(parents=True)
    python = executable(tmp_path / "echo-python", '#!/bin/sh\nprintf "%s\\n" "$PYTHONPATH"\nexit 17\n')
    (dest / "python-path").write_text(str(python) + "\n")
    recorded = dest / "hermes-path"
    recorded.write_text("\n".join(paths) + "\n")
    for present in (True, False):
        if not present:
            recorded.unlink()
        result = subprocess.run(["/bin/sh", str(REPO / "scripts/sandbox-check.sh")],
                                env=env, text=True, capture_output=True)
        assert result.returncode == 17, result.stderr
        expected = [str(dest / "site"), *(paths if present else [])]
        assert result.stdout.strip() == ":".join(expected)
