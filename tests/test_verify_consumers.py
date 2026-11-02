"""Verify consumers use disposable runtimes, never the installed Hermes launcher."""

import importlib.util
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
    site = tmp_path / "managed site"
    site.mkdir()
    (site / "__editable___hermes_agent_test_finder.py").write_text(
        "MAPPING = {'hermes_cli': 'stale', 'missing': 'stale'}\n")
    (root / "hermes_bootstrap.py").write_text(
        f"import sys\nsys.path.insert(0, {str(site)!r})\n")
    bindir = tmp_path / "bin"
    python = executable(bindir / "python", 'if [ "$1" = -c ]; then exit 0; fi\n'
                        'if [ "${FARM_NOTE:-}" = 1 ]; then\n'
                        ' echo farm-note >&2; echo 2; exit 0\nfi\n'
                        'if [ "${FARM_FAIL:-}" = 1 ]; then echo farm-broken >&2; exit 1; fi\n'
                        'exec ' + shlex.quote(sys.executable) + ' -S "$@"\n')
    executable(bindir / "uv", 'if [ "$1" = pip ]; then\n'
               ' while [ "$1" != --target ]; do shift; done\n mkdir -p "$2"\n exit 0\nfi\n'
               'case "$*" in *check-pytest.py*) exit 0 ;; esac\n'
               'printf "%s\\n%s\\n" "${PYTHONPATH-unset}" "${PYTHONNOUSERSITE-unset}"\n'
               'if [ -n "${HERMES_PYTHON_PATH:-}" ]; then\n'
               ' find "$TMPDIR" -name hermes-path -type f -print\nfi\nexit 17\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "HERMES_HOME": str(tmp_path / "hermes-home"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{bindir}:/usr/bin:/bin", "TMPDIR": str(tmp_path / "temporary"),
           "HERMES_PYTHON": str(python), "HERMES_PYTHON_PATH": f"{root}:{site}"}
    Path(env["TMPDIR"]).mkdir()
    for name in ("PYTHONPATH", "PYTHONNOUSERSITE", "FARM_FAIL", "FARM_NOTE"):
        env.pop(name, None)
    return env, root, site, python


def run(script, env, *args):
    return subprocess.run(["/bin/bash", str(script), *args], env=env,
                          text=True, capture_output=True, timeout=10)


def test_check_managed_and_legacy_paths_and_cleanup(runtime):
    env, root, site, _ = runtime
    # Legacy behavior preserves an inherited path and user-site policy.
    legacy = run(REPO / "scripts/check.sh", {**env, "HERMES_PYTHON_PATH": "",
                 "PYTHONPATH": "inherited", "PYTHONNOUSERSITE": "legacy"}, "-q")
    assert legacy.returncode == 17, legacy.stderr
    assert legacy.stdout.splitlines() == ["inherited", "legacy"]
    result = run(REPO / "scripts/check.sh", env, "-q")
    assert result.returncode == 17, result.stderr
    lines = result.stdout.splitlines()
    farm, tail = lines[0].split(":", 1)
    assert tail == str(site)
    assert Path(farm) != root
    assert lines[1] == "1"
    assert len(lines) == 3  # Temporary hermes-path exists while uv runs.
    assert not Path(lines[2]).exists()
    assert not Path(farm).exists()
    assert not list(Path(env["TMPDIR"]).iterdir())


def test_check_farm_failure_stops_before_uv(runtime):
    env, _, _, _ = runtime
    result = run(REPO / "scripts/check.sh", {**env, "FARM_FAIL": "1"})
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert "check: Hermes source farm failed" in result.stderr
    assert "farm-broken" in result.stderr
    assert not list(Path(env["TMPDIR"]).iterdir())


@pytest.mark.parametrize("state", [
    "current", "python", "path", "missing-path", "missing-farm", "missing-yaml",
    "legacy", "managed-to-legacy",
    pytest.param("unreadable-path", marks=pytest.mark.skipif(
        os.geteuid() == 0, reason="root can read files with mode 000")),
])
def test_update_refreshes_each_runtime_change_and_preserves_current_setup(runtime, tmp_path, state):
    env, root, site, python = runtime
    repo = tmp_path / "consumer repo"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/hc-update", scripts / "hc-update")
    shutil.copy2(REPO / "scripts/lib/hermes-python.sh", scripts / "lib/hermes-python.sh")
    marker = tmp_path / "setup-calls"
    env["SETUP_CALLS"] = str(marker)
    executable(scripts / "verify-setup.sh", 'echo setup >> "$SETUP_CALLS"\n')
    executable(scripts / "check.sh", 'exit 0\n')
    executable(tmp_path / "bin/hermes", 'exit 0\n')
    executable(tmp_path / "bin/git", 'echo 0\n')
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    (dest / "site").mkdir(parents=True)
    if state != "missing-yaml":
        (dest / "site/yaml").mkdir()
    (dest / "python-path").write_text(f"{python if state != 'python' else '/old/python'}\n")
    recorded = dest / "hermes-path"
    recorded.write_text(f"{root}\n{site if state != 'path' else '/old/site'}\n")
    farm = dest / "hermes-src"
    farm.mkdir(exist_ok=True)
    if state in ("missing-path", "legacy"):
        recorded.unlink()
    if state == "missing-farm":
        farm.rmdir()
    if state == "unreadable-path":
        recorded.chmod(0)
    marker.unlink(missing_ok=True)
    result = run(scripts / "hc-update", {**env, "HERMES_PYTHON_PATH":
                 "" if state in ("legacy", "managed-to-legacy") else env["HERMES_PYTHON_PATH"]})
    assert result.returncode == 0, result.stderr
    assert marker.exists() == (state not in ("current", "legacy")), state


@pytest.mark.parametrize("has_yaml", [False, True])
def test_update_reruns_setup_when_verify_overlay_lacks_yaml(runtime, tmp_path, has_yaml):
    env, root, site, python = runtime
    repo = tmp_path / "consumer repo"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/hc-update", scripts / "hc-update")
    shutil.copy2(REPO / "scripts/lib/hermes-python.sh", scripts / "lib/hermes-python.sh")
    marker = tmp_path / "setup-calls"
    env["SETUP_CALLS"] = str(marker)
    executable(scripts / "verify-setup.sh", 'echo setup >> "$SETUP_CALLS"\n')
    executable(scripts / "check.sh", 'exit 0\n')
    executable(tmp_path / "bin/hermes", 'exit 0\n')
    executable(tmp_path / "bin/git", 'echo 0\n')
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    (dest / "site").mkdir(parents=True)
    (dest / "site/pytest.py").write_text("# pytest only\n")
    if has_yaml:
        (dest / "site/yaml").mkdir()
    (dest / "python-path").write_text(f"{python}\n")
    (dest / "hermes-path").write_text(f"{root}\n{site}\n")
    (dest / "hermes-src").mkdir()
    result = run(scripts / "hc-update", env)
    assert result.returncode == 0, result.stderr
    assert marker.exists() is (not has_yaml)


@pytest.mark.parametrize("state", ["neither", "path", "farm"])
def test_eval_verify_command_paths_and_user_site(runtime, monkeypatch, state):
    env, root, site, python = runtime
    monkeypatch.syspath_prepend(str(REPO / "scripts/eval"))
    spec = importlib.util.spec_from_file_location("consumer_eval_run", REPO / "scripts/eval/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(env["HERMES_HOME"])
    dest = home / "control/verify"
    (dest / "site").mkdir(parents=True)
    (dest / "python-path").write_text(f"{python}\n")
    expected = [str(dest / "site")]
    if state != "neither":
        (dest / "hermes-path").write_text(f"{root}\n{site}")
        if state == "farm":
            (dest / "hermes-src").mkdir()
            expected += [str(dest / "hermes-src"), str(site)]
        else:
            expected += [str(root), str(site)]
    assert shlex.split(module.verify_command(home)) == [
        "PYTHONNOUSERSITE=1", "PYTHONPATH=" + ":".join(expected), str(python),
        "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def test_farm_skips_reserved_modules_unless_mapped_and_warns(runtime, tmp_path):
    env, root, site, python = runtime
    for name in ("tests.py", "scripts.py", "standalone.py", "tests.pyc", "scripts.pyc"):
        (root / name).touch()
    recorded = tmp_path / "hermes-path"
    recorded.write_text(f"{root}\n{site}\n")
    out = tmp_path / "farm"
    command = [str(python), str(REPO / "scripts/hermes-src-farm.py"),
               "--hermes-path", str(recorded), "--out", str(out)]
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not any(p.name.startswith(("tests.", "scripts.")) for p in out.iterdir())
    assert (out / "standalone.py").is_symlink()
    assert result.stderr == "hermes-src-farm: warning: MAPPING name missing not found under the repo root\n"
    (site / "__editable___hermes_agent_extra_finder.py").write_text(
        "MAPPING = {'tests': 'stale', 'scripts': 'stale'}\n")
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (out / "tests.py").is_symlink() and (out / "scripts.py").is_symlink()


def test_setup_passes_farm_stderr_without_polluting_count(runtime):
    env, _, _, _ = runtime
    result = run(REPO / "scripts/verify-setup.sh", {**env, "FARM_NOTE": "1"}, "--no-agy-copy")
    assert result.returncode == 0, result.stderr
    warning = "farm-note"
    assert warning in result.stderr
    assert warning not in result.stdout
    assert "; 2 farm entries" in result.stdout


@pytest.mark.parametrize("parser", ["broken", "missing"])
def test_runtime_output_requires_working_python3(runtime, tmp_path, parser):
    env, root, _, python = runtime
    env.pop("HERMES_PYTHON")
    env.pop("HERMES_PYTHON_PATH")
    executable(tmp_path / "bin/hermes", 'case "$1" in\n'
               ' --print-runtime-command) printf "%s\\n" "$ARGV";;\n'
               ' --version) printf "Install directory: %s\\n" "$ROOT";;\nesac\n')
    # A usable legacy interpreter would mask a silent managed-parser fallback.
    executable(root / "venv/bin/python", 'exit 0\n')
    env.update(ARGV=json.dumps([str(python)]), ROOT=str(root))
    if parser == "broken":
        executable(tmp_path / "bin/python3", 'exit 9\n')
    else:
        # Keep required shell utilities but deliberately provide no python3.
        for name in ("mktemp", "cat", "rm", "sleep", "dirname", "basename"):
            (tmp_path / "bin" / name).symlink_to(shutil.which(name))
        env["PATH"] = str(tmp_path / "bin")
    result = run_resolver(env)
    assert result.returncode != 0
    assert all(word in result.stderr for word in ("python3", "HERMES_PYTHON", "HERMES_PYTHON_PATH"))


@pytest.mark.parametrize("managed", [True, False])
def test_setup_agy_copy_python_environment(runtime, tmp_path, managed):
    env, _, _, python = runtime
    recorded = tmp_path / "agy-environment"
    original = python.read_text().removeprefix("#!/bin/sh\n")
    executable(python, 'case "$1" in\n'
               ' */agy-check-setup.py)\n'
               '  printf "%s\\n%s\\n" "${PYTHONPATH-unset}" '
               '"${PYTHONNOUSERSITE-unset}" > "$AGY_ENVIRONMENT"\n'
               '  exit 0;;\nesac\n' + original)
    env.update(AGY_ENVIRONMENT=str(recorded), PYTHONPATH="inherited",
               PYTHONNOUSERSITE="legacy")
    if not managed:
        env["HERMES_PYTHON_PATH"] = ""
    result = run(REPO / "scripts/verify-setup.sh", env)
    assert result.returncode == 0, result.stderr
    assert recorded.read_text().splitlines() == (
        [env["HERMES_PYTHON_PATH"], "1"] if managed else ["inherited", "legacy"])


def run_resolver(env):
    return subprocess.run(["/bin/bash", "-eu", "-c",
                           'source "$1"; resolve_hermes_python', "test",
                           str(REPO / "scripts/lib/hermes-python.sh")],
                          env=env, text=True, capture_output=True, timeout=10)


def test_bootstrap_output_is_parsed_with_runtime_python(runtime, tmp_path):
    env, root, site, python = runtime
    env.pop("HERMES_PYTHON")
    env.pop("HERMES_PYTHON_PATH")
    # Permit the health check and one argv parse, but no probe-output parse.
    executable(tmp_path / "bin/python3", 'if [ "$#" -eq 3 ]; then exec '
               + shlex.quote(sys.executable) + ' -I -S "$@"; fi\n'
               'if [ -e "$PARSED" ]; then exit 9; fi\n'
               'touch "$PARSED"\nexec ' + shlex.quote(sys.executable) + ' -I -S "$@"\n')
    env["PARSED"] = str(tmp_path / "parsed")
    executable(tmp_path / "bin/hermes", 'case "$1" in\n'
               ' --print-runtime-command) printf "%s\\n" "$ARGV";;\n'
               ' --version) printf "Install directory: %s\\n" "$ROOT";;\nesac\n')
    env.update(ARGV=json.dumps([str(python)]), ROOT=str(root))
    result = run_resolver(env)
    assert result.returncode == 0, result.stderr
