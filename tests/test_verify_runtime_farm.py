"""Managed verify isolation, using only disposable launchers and homes."""

import importlib.machinery
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

REPO = Path(__file__).resolve().parents[1]


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "Hermes repo's checkout"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "hermes_cli/__init__.py").touch()
    site = tmp_path / "managed env/site-packages"
    site.mkdir(parents=True)
    (site / "__editable___hermes_agent_test_finder.py").write_text(
        "MAPPING = {'hermes_cli': '/ignored', 'tools': '/also/ignored'}\n"
        "raise RuntimeError('finder must never execute')\n")
    (root / "tools").mkdir()
    (root / "hermes_bootstrap.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(site)!r})\n")
    for name in ("tests", "scripts"):
        (root / name).mkdir()
        (root / name / "__init__.py").touch()
    bindir = tmp_path / "bin"
    python = executable(tmp_path / "bundled/bin/python3", "#!/bin/sh\nexec " +
                        shlex.quote(sys.executable) + ' -S "$@"\n')
    old = executable(root / "venv/bin/python", '#!/bin/sh\necho stale >> "$OLD_CALLS"\nexit 0\n')
    executable(bindir / "python3", "#!/bin/sh\nexec " +
               shlex.quote(sys.executable) + ' -I -S "$@"\n')
    executable(bindir / "hermes", '#!/bin/sh\ncase "$1" in\n'
               ' --print-runtime-command) printf "%s\\n" "$RUNTIME_ARGV";;\n'
               ' --version)\n'
               '  if [ "$VERSION_MODE" != missing ]; then\n'
               '   printf "Install directory: %s\\n" "$RUNTIME_ROOT"; fi\n'
               '  if [ "$VERSION_MODE" = nonzero ]; then exit 9; fi;;\n'
               'esac\n')
    executable(bindir / "uv", '#!/bin/sh\nwhile [ "$1" != --target ]; do shift; done\nmkdir -p "$2"\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "HERMES_HOME": str(tmp_path / "hermes-home"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{bindir}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "RUNTIME_ROOT": str(root), "RUNTIME_ARGV": json.dumps([str(python), "-I"]),
           "VERSION_MODE": "ok", "OLD_CALLS": str(tmp_path / "old-calls")}
    for name in ("HERMES_PYTHON", "HERMES_PYTHON_PATH", "PYTHONPATH", "CODEX_SANDBOX"):
        env.pop(name, None)
    return env, root, site, python, old


def resolve(env, timeout=8):
    return subprocess.run(["bash", "-eu", "-c",
                           'source "$1"; resolve_hermes_python; '
                           'printf "%s\\n%s\\n" "$HERMES_PYTHON" "$HERMES_PYTHON_PATH"',
                           "test", str(REPO / "scripts/lib/hermes-python.sh")],
                          env=env, text=True, capture_output=True, timeout=timeout)


def farm(runtime, tmp_path):
    env, root, site, python, _ = runtime
    recorded = tmp_path / "hermes-path"
    recorded.write_text(f"{root}\n{site}\n")
    out = tmp_path / "farm"
    result = subprocess.run([str(python), "-I", str(REPO / "scripts/hermes-src-farm.py"),
                             "--hermes-path", str(recorded), "--out", str(out)],
                            env=env, text=True, capture_output=True)
    return result, out


def test_farm_maps_top_level_names_and_root_python_files(runtime, tmp_path):
    _, root, site, _, _ = runtime
    (root / "standalone.py").write_text("VALUE = 1\n")
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    (root / f"native{suffix}").touch()
    (site / "__editable___hermes_agent_more_finder.py").write_text(
        "MAPPING: dict = {'native': 'ignored', 'hermes_cli.nested': 'ignored'}\n")
    result, out = farm(runtime, tmp_path)
    assert result.returncode == 0, result.stderr
    expected = {"hermes_cli", "tools", "hermes_bootstrap.py", "standalone.py", f"native{suffix}"}
    assert {p.name for p in out.iterdir()} == expected
    assert all((out / name).is_symlink() and (out / name).resolve() == root / name
               for name in expected)
    # Replacement must discard old entries, including old package collisions.
    (out / "tests").mkdir()
    result, _ = farm(runtime, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (out / "tests").exists() and not (out / "scripts").exists()


def test_farm_allows_explicitly_mapped_tests_and_scripts(runtime, tmp_path):
    _, _, site, _, _ = runtime
    (site / "__editable___hermes_agent_more_finder.py").write_text(
        "MAPPING = {'tests': 'ignored', 'scripts': 'ignored'}\n")
    result, out = farm(runtime, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (out / "tests").is_symlink() and (out / "scripts").is_symlink()


@pytest.mark.parametrize("failure", ["no-finder", "no-mapping", "missing-root"])
def test_farm_error_preserves_previous_directory(runtime, tmp_path, failure):
    _, root, site, _, _ = runtime
    out = tmp_path / "farm"
    out.mkdir()
    (out / "previous").write_text("keep me")
    finder = site / "__editable___hermes_agent_test_finder.py"
    if failure == "no-finder":
        finder.unlink()
    elif failure == "no-mapping":
        finder.write_text("OTHER = {}\n")
    else:
        root.rename(root.with_name("moved"))
    result, _ = farm(runtime, tmp_path)
    assert result.returncode != 0
    assert "finder" in result.stderr.lower() if failure != "missing-root" else "root" in result.stderr.lower()
    assert (out / "previous").read_text() == "keep me"
    assert not out.with_name("farm.new").exists()


def test_setup_builds_then_removes_farm(runtime):
    env, root, site, python, old = runtime
    command = ["bash", str(REPO / "scripts/verify-setup.sh"), "--no-agy-copy"]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    assert (dest / "hermes-path").read_text().splitlines() == [str(root), str(site)]
    assert (dest / "hermes-src/hermes_cli").is_symlink()
    assert "3 farm entries" in result.stdout
    env["HERMES_PYTHON"] = str(old)
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert not (dest / "hermes-path").exists()
    assert not (dest / "hermes-src").exists()
    assert "0 farm entries" in result.stdout


def test_setup_fails_closed_when_farm_builder_fails(runtime):
    env, root, site, python, _ = runtime
    env.pop("HERMES_CONTROL_CHECK_COPY", None)
    env.pop("CODEX_SANDBOX", None)
    (site / "__editable___hermes_agent_test_finder.py").unlink()
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    (dest / "hermes-src").mkdir(parents=True)
    (dest / "hermes-src/stale").touch()
    (dest / "hermes-path").write_text(f"{root}\n{site}\n")
    (dest / "python-path").write_text(f"{python}\n")
    result = subprocess.run(
        ["bash", str(REPO / "scripts/verify-setup.sh"), "--no-agy-copy"],
        env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 2, result.stderr
    assert "verify-setup: hermes-src-farm: no editable Hermes finder" in result.stderr
    assert (
        "verify-setup: Hermes source farm failed; the full Hermes import path cannot be used "
        "because the Hermes repo root's tests package would shadow this repo's tests; "
        "fix the Hermes install and re-run scripts/verify-setup.sh"
    ) in result.stderr
    assert not (dest / "hermes-src").exists()
    assert not (dest / "hermes-path").exists()
    assert not (dest / "python-path").exists()
    result = subprocess.run(
        ["/bin/sh", str(REPO / "scripts/sandbox-check.sh")],
        env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 2, result.stderr
    assert "not set up" in result.stderr


@pytest.mark.parametrize("state", ["farm", "legacy", "no-path", "agy-copy"])
def test_sandbox_paths_and_no_user_site(runtime, tmp_path, state):
    env, root, site, _, _ = runtime
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    if state == "agy-copy":
        dest = Path(env["XDG_CONFIG_HOME"]) / "hermes-control/check"
        env["HERMES_CONTROL_CHECK_COPY"] = "1"
    (dest / "site").mkdir(parents=True)
    python = executable(tmp_path / "echo-python", '#!/bin/sh\n'
                        'printf "%s\\n%s\\n" "$PYTHONPATH" "${PYTHONNOUSERSITE-unset}"\nexit 17\n')
    (dest / "python-path").write_text(f"{python}\n")
    expected = [str(dest / "site")]
    if state in ("farm", "legacy"):
        # Last line deliberately has no newline.
        (dest / "hermes-path").write_text(f"{root}\n{site}")
        if state == "farm":
            (dest / "hermes-src").mkdir()
            expected += [str(dest / "hermes-src"), str(site)]
        else:
            expected += [str(root), str(site)]
    elif state == "agy-copy":
        expected += [str(dest / "src")]
    result = subprocess.run(["/bin/sh", str(REPO / "scripts/sandbox-check.sh")],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 17, result.stderr
    assert result.stdout.splitlines() == [":".join(expected), "1"]


def test_bootstrap_disables_lazy_installs_and_ignores_path_without_python(runtime):
    env, root, site, python, _ = runtime
    env["HERMES_PYTHON_PATH"] = "/stale:path"
    with (root / "hermes_bootstrap.py").open("a") as f:
        f.write("import os\nassert os.environ.get('HERMES_DISABLE_LAZY_INSTALLS') == '1'\n")
    result = resolve(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(python), f"{root}:{site}"]


def test_bootstrap_hang_is_bounded_without_venv_fallback(runtime, tmp_path):
    env, root, _, _, _ = runtime
    (root / "hermes_bootstrap.py").write_text("import time\ntime.sleep(30)\n")
    # Exercise the real watchdog, accelerating just its 20 second deadline.
    executable(tmp_path / "bin/sleep", '#!/bin/sh\n'
               'echo "$1" >> "$SLEEP_CALLS"\n'
               'if [ "$1" = 20 ]; then exec /bin/sleep 0.2; fi\nexec /bin/sleep "$@"\n')
    env["SLEEP_CALLS"] = str(tmp_path / "sleeps")
    start = time.monotonic()
    result = resolve(env, timeout=5)
    assert time.monotonic() - start < 5
    assert result.returncode != 0
    assert "bootstrap" in result.stderr.lower()
    assert "HERMES_PYTHON_PATH" in result.stderr
    assert "20" in Path(env["SLEEP_CALLS"]).read_text().splitlines()
    assert not Path(env["OLD_CALLS"]).exists()


@pytest.mark.parametrize("kind", ["non-executable-file", "executable-directory"])
def test_runtime_unusable_executable_falls_back(runtime, tmp_path, kind):
    env, _, _, _, old = runtime
    candidate = tmp_path / "unusable-python"
    if kind == "non-executable-file":
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o644)
    else:
        candidate.mkdir(mode=0o755)
    env["RUNTIME_ARGV"] = json.dumps([str(candidate)])
    env["HERMES_PYTHON_PATH"] = "stale:path"
    result = resolve(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(old), ""]
    assert Path(env["OLD_CALLS"]).exists()


@pytest.mark.parametrize("failure", ["missing-directory", "bad-bootstrap"])
def test_valid_runtime_never_falls_back_after_discovery_failure(runtime, failure):
    env, root, _, _, _ = runtime
    if failure == "missing-directory":
        env["VERSION_MODE"] = "missing"
        step = "Install directory"
    else:
        (root / "hermes_bootstrap.py").write_text("raise RuntimeError('broken')\n")
        step = "bootstrap"
    result = resolve(env)
    assert result.returncode != 0
    assert step.lower() in result.stderr.lower()
    assert "HERMES_PYTHON" in result.stderr and "HERMES_PYTHON_PATH" in result.stderr
    assert not Path(env["OLD_CALLS"]).exists()


def test_install_directory_used_even_when_version_exits_nonzero(runtime):
    env, root, site, python, _ = runtime
    env["VERSION_MODE"] = "nonzero"
    result = resolve(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(python), f"{root}:{site}"]
    assert not Path(env["OLD_CALLS"]).exists()


def test_explicit_python_validates_with_managed_path(runtime):
    env, root, site, _, _ = runtime
    # Unlike the bootstrap wrapper this Python uses PYTHONPATH normally.
    python = executable(root.parent / "explicit/python3", "#!/bin/sh\nexec " +
                        shlex.quote(sys.executable) + ' "$@"\n')
    (site / "managed_dependency.py").write_text("VALUE = 42\n")
    (root / "hermes_cli/__init__.py").write_text("import managed_dependency\n")
    env.update(HERMES_PYTHON=str(python), HERMES_PYTHON_PATH=f"{root}:{site}")
    result = resolve(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(python), f"{root}:{site}"]


def test_explicit_python_rejects_bad_first_path_entry(runtime):
    env, root, site, _, old = runtime
    env.update(HERMES_PYTHON=str(old), HERMES_PYTHON_PATH=f"{site}:{root}")
    result = resolve(env)
    assert result.returncode != 0
    assert "HERMES_PYTHON_PATH" in result.stderr and "first" in result.stderr
    assert not Path(env["OLD_CALLS"]).exists()


def test_import_report_includes_pythonpath_site_packages(runtime):
    env, _, site, _, _ = runtime
    (site / "managed_dependency.py").write_text("VALUE = 42\n")
    env["PYTHONPATH"] = str(site)
    code = ("import runpy, managed_dependency; "
            f"runpy.run_path({str(REPO / 'scripts/agy_check_imports.py')!r})"
            "['pytest_sessionfinish'](None, 0)")
    result = subprocess.run([sys.executable, "-S", "-c", code], env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "managed_dependency" in result.stdout.splitlines()
