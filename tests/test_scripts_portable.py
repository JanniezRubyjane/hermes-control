"""Exercise the shell entry points with a stranger's launcher and home."""
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def machine(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    modules = tmp_path / "modules"
    (modules / "hermes_cli").mkdir(parents=True)
    (modules / "hermes_cli/__init__.py").touch()
    log = tmp_path / "calls"
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "HERMES_HOME": str(tmp_path / "custom-hermes"),
           "PATH": f"{bindir}:/usr/bin:/bin", "PYTHONPATH": str(modules),
           "CALLS": str(log)}
    env.pop("HERMES_PYTHON", None)
    executable(bindir / "uv", '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$CALLS"\n')
    return tmp_path, bindir, log, env


def run(script, env, *args, repo=REPO):
    return subprocess.run(["bash", str(repo / "scripts" / script), *args],
                          env=env, text=True, capture_output=True)


@pytest.mark.parametrize("script", ["check.sh", "verify-setup.sh"])
@pytest.mark.parametrize("kind", ["shebang", "env_shebang", "sibling", "sibling3", "explicit"])
def test_interpreter_resolution(machine, script, kind):
    root, bindir, log, env = machine
    python = root / "venv/bin/python"
    python.parent.mkdir(parents=True)
    executable(python, "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    launcher = root / "launchers/hermes"
    header = f"#!{python}" if kind == "shebang" else "#!/bin/sh"
    if kind == "env_shebang":
        (bindir / "python3").symlink_to(python)
        header = "#!/usr/bin/env python3"
    if kind.startswith("sibling"):
        sibling = launcher.parent / ("python3" if kind == "sibling3" else "python")
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.symlink_to(python)
        python = sibling
    if kind == "explicit":
        env["HERMES_PYTHON"] = str(python)
    executable(launcher, header + "\n")
    (bindir / "hermes").symlink_to(launcher)

    result = run(script, env)

    assert result.returncode == 0, result.stderr
    assert str(python if kind != "env_shebang" else bindir / "python3") in log.read_text()
    if script == "verify-setup.sh":
        assert (Path(env["HERMES_HOME"]) / "control/verify/python-path").exists()
        assert not (Path(env["HOME"]) / ".hermes").exists()


@pytest.mark.parametrize("script", ["check.sh", "verify-setup.sh"])
def test_missing_interpreter_is_actionable(machine, script):
    _, bindir, _, env = machine
    executable(bindir / "hermes", "#!/bin/sh\nexit 0\n")
    result = run(script, env)
    assert result.returncode == 1
    assert "set HERMES_PYTHON to the Python that runs Hermes" in result.stderr


def test_interpreter_must_import_hermes(machine):
    _, _, _, env = machine
    root = Path(env["HOME"]).parent
    env["HERMES_PYTHON"] = str(executable(
        root / "python", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -I -S "$@"\n'))
    result = run("check.sh", env)
    assert result.returncode == 1
    assert "set HERMES_PYTHON" in result.stderr


def test_sandbox_uses_custom_home(machine):
    root, _, log, env = machine
    python = executable(root / "python", '#!/bin/sh\nprintf "pytest %s\\n" "$*" >> "$CALLS"\n')
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    (dest / "site").mkdir(parents=True)
    (dest / "python-path").write_text(str(python) + "\n")
    result = run("sandbox-check.sh", env)
    assert result.returncode == 0, result.stderr
    assert "pytest" in log.read_text()


def update_repo(machine):
    root, bindir, log, env = machine
    repo = root / "repo"
    shutil.copytree(REPO / "scripts", repo / "scripts")
    executable(repo / "scripts/check.sh", '#!/bin/sh\necho tests >> "$CALLS"\n')
    executable(repo / "scripts/verify-setup.sh", '#!/bin/sh\necho setup >> "$CALLS"\n')
    python = root / "python"
    executable(python, "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    executable(root / "hermes", '#!/bin/sh\nprintf "hermes %s\\n" "$*" >> "$CALLS"\n')
    (bindir / "hermes").symlink_to(root / "hermes")
    return repo


def test_update_check_runs_no_tests_or_setup(machine):
    repo = update_repo(machine)
    _, _, log, env = machine
    result = run("hc-update", env, "--check", repo=repo)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "hermes update --check\n"
    assert "Hermes-Control" in result.stdout
    assert not Path(env["HERMES_HOME"]).exists()


@pytest.mark.parametrize("changed", [False, True])
def test_update_refreshes_setup_when_interpreter_changed(machine, changed):
    repo = update_repo(machine)
    root, _, log, env = machine
    dest = Path(env["HERMES_HOME"]) / "control/verify"
    dest.mkdir(parents=True)
    (dest / "python-path").write_text(str(root / ("old-python" if changed else "python")) + "\n")
    result = run("hc-update", env, repo=repo)
    assert result.returncode == 0, result.stderr
    assert ("setup" in log.read_text()) is changed
    assert "tests" in log.read_text()


def test_install_and_uninstall_preserve_foreign_links_and_logs(machine):
    root, bindir, _, env = machine
    executable(bindir / "hermes", '#!/bin/sh\nexit 0\n')
    assert run("install.sh", env).returncode == 0
    home = Path(env["HERMES_HOME"])
    plugin = home / "plugins/hermes_control"
    skill = home / "skills/software-development/hermes-control"
    update = Path(env["HOME"]) / ".local/bin/hc-update"
    assert plugin.resolve() == REPO / "hermes_control"
    skill.unlink()
    foreign = root / "foreign"
    foreign.mkdir()
    skill.symlink_to(foreign)
    logs = home / "control/runs/log.json"
    logs.parent.mkdir(parents=True)
    logs.write_text("keep me")
    result = run("uninstall.sh", env)
    assert result.returncode == 0, result.stderr
    assert not plugin.is_symlink() and not update.is_symlink()
    assert skill.is_symlink() and skill.resolve() == foreign
    assert logs.read_text() == "keep me"
    assert "kept" in result.stdout.lower()
    (home / "config.yaml").touch()
    owned = home / "control/run-example.jsonl"
    owned.write_text("remove me")
    assert run("uninstall.sh", env, "--purge").returncode == 0
    assert not owned.exists()
    assert logs.read_text() == "keep me"
    assert skill.is_symlink()


def test_planner_without_runtime_provider(monkeypatch):
    from hermes_control import planner_status
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", None)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "model": {"provider": "anthropic", "default": "sonnet"}})
    result = planner_status.status()
    assert result["signed_in"] is None
    assert result["provider"] == "anthropic"
    assert result["model"] == "sonnet"
    assert result["detail"] == "sign-in state not checked"


def test_update_check_reports_repo_behind_without_changing_it(machine):
    repo = update_repo(machine)
    _, _, log, env = machine

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Portability test")
    git("config", "user.email", "portable@example.invalid")
    git("add", ".")
    git("commit", "-qm", "base")
    git("branch", "upstream")
    branch = git("branch", "--show-current")
    git("config", f"branch.{branch}.remote", ".")
    git("config", f"branch.{branch}.merge", "refs/heads/upstream")
    git("checkout", "-q", "upstream")
    git("commit", "--allow-empty", "-qm", "new upstream")
    git("checkout", "-q", branch)
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}

    result = run("hc-update", env, "--check", repo=repo)

    assert result.returncode == 0, result.stderr
    assert "behind its upstream by 1" in result.stdout
    assert log.read_text() == "hermes update --check\n"
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.parametrize("header", ["#!/bin/sh", "#!/usr/bin/env sh"])
def test_non_python_shebang_is_never_executed(machine, header):
    root, bindir, log, env = machine
    # This fake sh would succeed as a Python if accidentally tested with -c.
    fake_sh = executable(bindir / "sh", '#!/bin/sh\necho bad >> "$CALLS"\nexit 0\n')
    if header == "#!/bin/sh":
        header = f"#!{fake_sh}"
    executable(bindir / "hermes", header + "\n")
    result = run("check.sh", env)
    assert result.returncode == 1
    assert not log.exists()


@pytest.mark.parametrize("header", ["#!/usr/bin/env python3", "#!/usr/bin/env -S python3"])
@pytest.mark.parametrize("script", ["check.sh", "verify-setup.sh"])
def test_shebang_python_without_hermes_falls_back_to_sibling(machine, header, script):
    root, bindir, log, env = machine
    executable(bindir / "python3", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -I -S "$@"\n')
    launcher = executable(root / "venv/bin/hermes", header + "\n")
    sibling = executable(launcher.parent / "python", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    (bindir / "hermes").symlink_to(launcher)
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert str(sibling) in log.read_text()


@pytest.mark.parametrize("inherited", [False, True])
def test_sandbox_default_home_fallback_preserves_environment(machine, inherited):
    root, _, log, env = machine
    previous = env["HERMES_HOME"] if inherited else "unset"
    if not inherited:
        env.pop("HERMES_HOME")
    dest = Path(env["HOME"]) / ".hermes/control/verify"
    (dest / "site").mkdir(parents=True)
    python = executable(root / "python", '#!/bin/sh\nprintf "%s\\n" "${HERMES_HOME-unset}" >> "$CALLS"\n')
    (dest / "python-path").write_text(str(python) + "\n")
    result = subprocess.run(["/bin/sh", str(REPO / "scripts/sandbox-check.sh")],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == previous


@pytest.mark.parametrize("failure", [False, True])
def test_uninstall_disables_plugin_even_if_disable_fails(machine, failure):
    _, bindir, log, env = machine
    executable(bindir / "hermes", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\nexit ' + str(int(failure)) + '\n')
    result = run("uninstall.sh", env)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "plugins disable hermes_control\n"
    assert "hermes plugins disable hermes_control" in result.stdout
    if failure:
        assert "failed" in result.stderr


@pytest.mark.parametrize("marker", ["config.yaml", "state.db"])
def test_purge_removes_only_plugin_owned_files(machine, marker):
    _, _, _, env = machine
    home = Path(env["HERMES_HOME"])
    home.mkdir()
    (home / marker).touch()
    owned = ["verify/python-path", "verify/site/module.py", "run-a.jsonl", "run-a.live.lock",
             "run-a.summary.lock", "run-a.resume-task.lock", "usage-claude.json", "usage-claude.json.tmp123",
             "paused/task.json", "paused/task.tmp", "checks/task.sh", "slots/claude/slot-0.lock", "slots/codex/slot-1.lock"]
    foreign = ["foreign.json", "runs/log.json", "paused/note.txt", "checks/note.txt", "slots/other/slot-0.lock"]
    for name in owned + foreign:
        path = home / "control" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    result = run("uninstall.sh", env, "--purge")
    assert result.returncode == 0, result.stderr
    assert all(not (home / "control" / name).exists() for name in owned)
    assert all((home / "control" / name).read_text() == name for name in foreign)
    assert (home / marker).exists()


def test_purge_refuses_non_hermes_home(machine):
    _, _, _, env = machine
    home = Path(env["HERMES_HOME"])
    data = home / "control/run-a.jsonl"
    data.parent.mkdir(parents=True)
    data.write_text("keep")
    result = run("uninstall.sh", env, "--purge")
    assert result.returncode == 2
    assert "refusing purge" in result.stderr
    assert data.read_text() == "keep"


def install_directory_launcher(machine):
    root, bindir, _, env = machine
    install = root / "Hermes install"
    bootstrap = executable(root / "bootstrap/python3", "#!/bin/sh\nexec " +
                           shlex.quote(sys.executable) + ' -I -S "$@"\n')
    code = "import sys; print('Install directory: ' + sys.argv[1])"
    executable(bindir / "hermes", "#!/bin/sh\nexec " + shlex.quote(str(bootstrap)) +
               " -c " + shlex.quote(code) + " " + shlex.quote(str(install)) + ' "$@"\n')
    return install, bootstrap


@pytest.mark.parametrize("script", ["check.sh", "verify-setup.sh"])
@pytest.mark.parametrize("relative", ["venv/bin/python", ".venv/bin/python", "venv/bin/python3"])
def test_shell_launcher_reports_install_interpreter(machine, script, relative):
    install, bootstrap = install_directory_launcher(machine)
    _, _, log, env = machine
    assert subprocess.run([str(bootstrap), "-c", "import hermes_cli"], env=env,
                          capture_output=True).returncode != 0
    python = executable(install / relative, "#!/bin/sh\nexec " +
                        shlex.quote(sys.executable) + ' "$@"\n')

    result = run(script, env)

    assert result.returncode == 0, result.stderr
    assert str(python) in log.read_text()


@pytest.mark.parametrize("script", ["check.sh", "verify-setup.sh"])
def test_install_directory_without_venv_lists_attempts(machine, script):
    install, _ = install_directory_launcher(machine)
    _, bindir, _, env = machine

    result = run(script, env)

    assert result.returncode == 1
    assert "set HERMES_PYTHON to the Python that runs Hermes (must import hermes_cli)" in result.stderr
    for path in (bindir / "python", bindir / "python3", install / "venv/bin/python",
                 install / ".venv/bin/python", install / "venv/bin/python3"):
        assert str(path) in result.stderr


def test_install_directory_rejects_python_without_hermes(machine):
    install, bootstrap = install_directory_launcher(machine)
    _, _, log, env = machine
    (install / "venv/bin").mkdir(parents=True)
    (install / "venv/bin/python").symlink_to(bootstrap)
    python = executable(install / ".venv/bin/python", "#!/bin/sh\nexec " +
                        shlex.quote(sys.executable) + ' "$@"\n')
    executable(install / "venv/bin/python3", "#!/bin/sh\nexit 0\n")

    result = run("check.sh", env)

    assert result.returncode == 0, result.stderr
    assert str(python) in log.read_text()


@pytest.mark.parametrize("body", ["exit 7", "exec /bin/sleep 30", "/bin/sleep 30"])
def test_version_failure_or_timeout_is_actionable(machine, body):
    _, bindir, _, env = machine
    executable(bindir / "hermes", "#!/bin/sh\n" + body + "\n")

    result = subprocess.run(["bash", str(REPO / "scripts/check.sh")], env=env,
                            text=True, capture_output=True, timeout=9)

    assert result.returncode == 1
    assert "set HERMES_PYTHON" in result.stderr
    assert "--version (Install directory)" in result.stderr
