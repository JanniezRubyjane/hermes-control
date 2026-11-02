"""Offline checks use a disposable, read-only toolchain in a sandbox-readable home."""

import json
from importlib import machinery
import os
from pathlib import Path
import runpy
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
DESELECTED = [
    "tests/test_community_files.py::test_tracked_public_files_have_no_private_markers",
    "tests/test_community_files.py::test_tracked_public_files_have_no_thai_script",
    "tests/test_community_files.py::test_tracked_public_pngs_have_only_approved_chunks",
    "tests/test_watch.py::test_the_real_hermes_cli_runs_it",
]


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


@pytest.fixture
def machine(tmp_path):
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home),
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "HERMES_HOME": str(tmp_path / "hermes"),
           "TMPDIR": str(tmp_path), "CALLS": str(tmp_path / "calls")}
    env.pop("HERMES_PYTHON", None)
    env.pop("HERMES_PYTHON_PATH", None)
    env.pop("PYTHONPATH", None)
    env.pop("CODEX_SANDBOX", None)
    env.pop("HERMES_CONTROL_CHECK_COPY", None)
    yield tmp_path, env
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


def copy_path(env):
    return Path(env.get("XDG_CONFIG_HOME", str(Path(env["HOME"]) / ".config"))) / "hermes-control/check"


def run_check(env, *args):
    return subprocess.run(["/bin/sh", str(REPO / "scripts/sandbox-check.sh"), *args],
                          env=env, text=True, capture_output=True)


def install_check_stub(path, env, label):
    (path / "site").mkdir(parents=True)
    python = executable(path / "python/bin/python", "#!/bin/sh\n" +
                        "printf '%s\\n' " + shlex.quote(label) +
                        ' "$PYTHONPATH" "$@" > "$CALLS"\nexit 17\n')
    (path / "python-path").write_text(str(python) + "\n")


def test_primary_preferred_even_with_copy(machine):
    root, env = machine
    primary = Path(env["HERMES_HOME"]) / "control/verify"
    install_check_stub(primary, env, "primary")
    install_check_stub(copy_path(env), env, "copy")
    result = run_check(env, "tests/test_limits.py", "-k", "some expression")
    assert result.returncode == 17
    assert result.stderr == ""
    assert (root / "calls").read_text().splitlines() == [
        "primary", str(primary / "site"), "-m", "pytest", "-q", "-p",
        "no:cacheprovider", "tests/test_limits.py", "-k", "some expression"]


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("xdg", [False, True])
def test_copy_fallback_and_arguments(machine, unreadable, xdg):
    root, env = machine
    env["HERMES_CONTROL_CHECK_COPY"] = "1"
    if not xdg:
        env.pop("XDG_CONFIG_HOME")
    primary = Path(env["HERMES_HOME"]) / "control/verify"
    if unreadable:
        install_check_stub(primary, env, "primary")
        (primary / "python-path").chmod(0)
        assert not os.access(primary / "python-path", os.R_OK)
    dest = copy_path(env)
    install_check_stub(dest, env, "copy")
    result = run_check(env, "tests/test_limits.py", "-k", "some expression")
    assert result.returncode == 17
    assert len(result.stderr.splitlines()) == 1
    assert "agy copy" in result.stderr and "4 tests" in result.stderr
    assert "post-commit verify" in result.stderr
    args = (root / "calls").read_text().splitlines()
    assert args[:7] == ["copy", f"{dest}/site:{dest}/src", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    assert args[-3:] == ["tests/test_limits.py", "-k", "some expression"]
    assert args[7:-3] == [item for name in DESELECTED for item in ("--deselect", name)]


def test_neither_setup_keeps_message_and_status(machine):
    _, env = machine
    result = run_check(env)
    assert result.returncode == 2
    assert result.stderr == "sandbox-check: not set up; run scripts/verify-setup.sh once, outside the sandbox\n"


@pytest.fixture
def setup_machine(machine):
    root, env = machine
    repo = root / "repo"
    shutil.copytree(REPO / "scripts", repo / "scripts")
    # A tiny relocatable CPython stand-in and a venv with real packaging shapes.
    base = root / "base python"
    executable(base / "bin/python3.11", "#!/bin/sh\nexit 0\n")
    (base / "lib").mkdir()
    (base / "lib/stdlib.py").write_text("# stdlib\n")
    alias = root / "base-link"
    alias.symlink_to(base, target_is_directory=True)
    venv = root / "venv"
    site = venv / "site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {alias}/bin\nversion = 3.11.9\n")
    package = site / "example_dep"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 42\n")
    # PathFinder discovers extension modules without loading their code.
    (site / "native_dep.so").write_bytes(b"fake native library")
    (site / "single_dep.py").write_text("VALUE = 43\n")
    namespace = site / "namespace/subpackage"
    namespace.mkdir(parents=True)
    (namespace / "__init__.py").touch()
    metadata = site / "unrelated_distribution-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: unrelated-distribution\nVersion: 1.0\n")
    (metadata / "RECORD").write_text("example_dep/__init__.py,,\nsingle_dep.py,,\nnative_dep.so,,\nnamespace/subpackage/__init__.py,,\n")
    source = root / "hermes-agent"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n')
    (source / "hermes_cli/__init__.py").touch()
    (source / "root_module.py").write_text("VALUE = 44\n")
    (site / "__editable___hermes_agent_1_finder.py").write_text(
        "MAPPING: dict[str, str] = " + repr({"hermes_cli": str(source / "hermes_cli"),
                                            "root_module": str(source / "root_module")}) + "\n")
    # The wrapper models the venv's prefix and site without copying the host Python.
    shim = root / "shim.py"
    shim.write_text(
        "import runpy, sys, sysconfig\n"
        # The helper now imports the plugin's scanner. Keep the launcher's import
        # paths even when its interpreter is a relocated base with no venv site.
        f"sys.path[:0] = {sys.path!r}\n"
        f"sys.prefix = {str(venv)!r}\n"
        "original = sysconfig.get_path\n"
        f"sysconfig.get_path = lambda name, *a, **kw: {str(site)!r} if name in ('purelib', 'platlib') else original(name, *a, **kw)\n"
        "sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
    python = executable(venv / "bin/python", "#!/bin/sh\n" +
                        'if [ "$1" = -c ]; then exit 0; fi\nexec ' +
                        shlex.quote(sys.executable) + " " + shlex.quote(str(shim)) + ' "$@"\n')
    bindir = root / "bin"
    executable(bindir / "uv", '#!/bin/sh\nwhile [ "$1" != --target ]; do shift; done\nmkdir -p "$2"\nprintf "# pytest\\n" > "$2/pytest.py"\n')
    env.update(HERMES_PYTHON=str(python), PATH=f"{bindir}:{env['PATH']}")
    (repo / "scripts/agy-check-packages.txt").write_text("example_dep\nsingle_dep\nnative_dep\nnamespace\n")
    return root, env, repo, base, site, source


def run_setup(machine, *args):
    _, env, repo, *_ = machine
    return subprocess.run(["bash", str(repo / "scripts/verify-setup.sh"), *args],
                          env=env, text=True, capture_output=True)


@pytest.mark.parametrize("standalone", [False, True])
def test_managed_path_copies_checkout_instead_of_editable_workspace(setup_machine, standalone):
    root, env, repo, base, site, workspace = setup_machine
    checkout = root / "live checkout"
    (checkout / "hermes_cli").mkdir(parents=True)
    (checkout / "hermes_cli/__init__.py").write_text("ORIGIN = 'live checkout'\n")
    (checkout / "root_module.py").write_text("ORIGIN = 'live module'\n")
    (checkout / "hermes_dir_acl.py").write_text("ORIGIN = 'new root module'\n")
    native_suffix = machinery.EXTENSION_SUFFIXES[0]
    (checkout / f"native{native_suffix}").write_bytes(b"checkout native module")
    (checkout / "bytecode.pyc").write_bytes(b"checkout bytecode module")
    finder = site / "__editable___hermes_agent_1_finder.py"
    # The finder is parsed, never executed; put optional and native names in its literal.
    finder.write_text("MAPPING = " + repr({
        "hermes_cli": str(workspace / "hermes_cli"),
        "root_module": str(workspace / "root_module"),
        "native": str(workspace / "absent_native"),
        "bytecode": str(workspace / "absent_bytecode"),
        "ignored_optional": str(workspace / "absent"),
    }) + "\n")
    for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                  "commit", "--allow-empty", "-qm", "Checkout"]):
        subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    shim = root / "shim.py"
    # Managed dependencies must come from the file, not the interpreter's sites.
    shim.write_text(shim.read_text().replace(
        f"{str(site)!r} if name in", f"{str(root / 'empty/site-packages')!r} if name in"))
    if standalone:
        shim.write_text(shim.read_text().replace(
            f"sys.prefix = {str(root / 'venv')!r}",
            f"sys.prefix = {str(base)!r}\nsys.version = '3.11.9 (test runtime)'"))
        assert not (base / "pyvenv.cfg").exists()
    primary = Path(env["HERMES_HOME"]) / "control/verify"
    (primary / "site").mkdir(parents=True)
    (primary / "site/pytest.py").touch()
    path_file = primary / "hermes-path"
    path_file.write_text(f"{checkout}\n{root / 'not-a-dependency-site'}\n{site}\n")
    destination = copy_path(env)
    command = [env["HERMES_PYTHON"], str(repo / "scripts/agy-check-setup.py"),
               "--hermes-python", env["HERMES_PYTHON"], "--verify", str(primary),
               "--hermes-path", str(path_file), "--destination", str(destination)]
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (destination / "src/hermes_cli/__init__.py").read_text() == "ORIGIN = 'live checkout'\n"
    assert (destination / "src/root_module.py").read_text() == "ORIGIN = 'live module'\n"
    assert (destination / "src/hermes_dir_acl.py").is_file()
    assert (destination / f"src/native{native_suffix}").read_bytes() == b"checkout native module"
    assert (destination / "src/bytecode.pyc").read_bytes() == b"checkout bytecode module"
    assert not (destination / "src/ignored_optional").exists()
    assert (destination / "site/example_dep/__init__.py").is_file()
    assert (destination / "python/lib/stdlib.py").is_file()
    assert json.loads((destination / "stamp").read_text())["hermes_agent_head"] == head
    previous = (destination / "stamp").read_bytes()
    (checkout / "root_module.py").unlink()
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "Hermes editable package is missing: root_module" in result.stderr
    assert (destination / "stamp").read_bytes() == previous
    assert not list(destination.parent.glob(".check-*"))


def test_verify_setup_passes_recorded_path_to_agy_copy(setup_machine):
    _, env, _, _, site, source = setup_machine
    env["HERMES_PYTHON_PATH"] = f"{source}:{site}"
    # Missing on both sides is an optional module only in the managed path mode.
    finder = site / "__editable___hermes_agent_1_finder.py"
    finder.write_text("MAPPING = " + repr({"hermes_cli": str(source / "hermes_cli"),
                                          "optional": str(source / "absent")}) + "\n")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    assert (copy_path(env) / "src/root_module.py").is_file()


def plant_exclusions(directory):
    for name in (".env", ".env.local", "private.env", ".git/config", "tests/test_a.py",
                 "node_modules/a.js", "__pycache__/a.pyc"):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded\n")


def test_setup_ignores_callers_hermes_python_env(monkeypatch, request, tmp_path):
    monkeypatch.setenv("HERMES_PYTHON_PATH", str(tmp_path / "bogus-site-packages"))
    monkeypatch.setenv("HERMES_PYTHON", str(tmp_path / "bogus-python"))
    setup_machine = request.getfixturevalue("setup_machine")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("xdg", [False, True])
def test_setup_layout_readonly_rebuild_and_exclusions(setup_machine, xdg):
    _, env, _, base, site, source = setup_machine
    if not xdg:
        env.pop("XDG_CONFIG_HOME")
    for directory in (base, site / "example_dep", source / "hermes_cli"):
        plant_exclusions(directory)
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    dest = copy_path(env)
    assert (dest / "python/bin/python3.11").is_file()
    assert (dest / "site/pytest.py").is_file()
    assert (dest / "site/example_dep/__init__.py").is_file()
    assert (dest / "site/single_dep.py").is_file()
    assert (dest / "site/native_dep.so").is_file()
    assert (dest / "site/namespace/subpackage/__init__.py").is_file()
    assert (dest / "site/unrelated_distribution-1.0.dist-info/METADATA").is_file()
    assert (dest / "src/hermes_cli/__init__.py").is_file()
    assert (dest / "src/root_module.py").is_file()
    assert (dest / "python-path").read_text().strip() == str(dest / "python/bin/python3.11")
    stamp = json.loads((dest / "stamp").read_text())
    assert stamp["hermes_python"] == env["HERMES_PYTHON"]
    assert stamp["base_python_version"] == "3.11.9"
    assert stamp["hermes_agent_head"] is None
    assert stamp["built_at"].endswith("Z")
    for path in [dest, *dest.rglob("*")]:
        assert not path.is_symlink()
        assert not path.stat().st_mode & 0o222
        assert path.name not in {".git", "tests", "node_modules", "__pycache__"}
        assert not path.name.startswith(".env") and not path.name.endswith(".env")
    (site / "example_dep/__init__.py").write_text("NEW = True\n")
    assert run_setup(setup_machine).returncode == 0
    assert (dest / "site/example_dep/__init__.py").read_text() == "NEW = True\n"
    assert not list(dest.parent.glob(".check-*"))


@pytest.mark.parametrize(("rule", "secret"), [
    ("private-key", "-----BEGIN " + "PRIVATE KEY-----"),
    ("aws-key", "AKIA" + "A" * 16),
    ("aws-key", "ASIA" + "B" * 16),
    ("github-token", "ghp_" + "c" * 36),
    ("github-pat", "github_pat_" + "d" * 40),
    ("slack-token", "xoxb-" + "e" * 30),
    ("google-api-key", "AIza" + "f" * 35),
])
def test_setup_refuses_secrets_and_preserves_copy(setup_machine, rule, secret):
    _, env, _, _, site, _ = setup_machine
    assert run_setup(setup_machine).returncode == 0
    (site / "example_dep/leak.txt").write_text(secret)
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "site/example_dep/leak.txt" in result.stderr
    assert rule in result.stderr
    assert secret not in result.stdout + result.stderr
    assert (copy_path(env) / "site/example_dep/__init__.py").read_text() == "VALUE = 42\n"
    assert not list(copy_path(env).parent.glob(".check-*"))


def test_setup_missing_listed_package_is_actionable(setup_machine):
    _, env, repo, *_ = setup_machine
    with (repo / "scripts/agy-check-packages.txt").open("a") as file:
        file.write("missing_dependency\n")
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "missing_dependency" in result.stderr and "missing" in result.stderr
    assert not copy_path(env).exists()


def test_setup_skip_flag_still_installs_primary(setup_machine):
    _, env, repo, *_ = setup_machine
    (repo / "scripts/agy-check-packages.txt").unlink()
    result = run_setup(setup_machine, "--no-agy-copy")
    assert result.returncode == 0, result.stderr
    primary = Path(env["HERMES_HOME"]) / "control/verify"
    assert (primary / "site/pytest.py").is_file()
    assert (primary / "python-path").read_text().strip() == env["HERMES_PYTHON"]
    assert not copy_path(env).exists()


def test_setup_stamp_records_readable_hermes_head(setup_machine):
    _, env, _, _, _, source = setup_machine
    for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                 "commit", "--allow-empty", "-qm", "Hermes source"]):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    assert json.loads((copy_path(env) / "stamp").read_text())["hermes_agent_head"] == head


@pytest.mark.parametrize("marker", [None, "", "0", "true", "1"])
@pytest.mark.parametrize("sandbox", [None, "", "seatbelt"])
@pytest.mark.parametrize("missing", ["python-path", "site", "different-home"])
def test_copy_requires_explicit_worker_opt_in(machine, marker, sandbox, missing):
    root, env = machine
    install_check_stub(copy_path(env), env, "copy")
    primary = Path(env["HERMES_HOME"]) / "control/verify"
    install_check_stub(primary, env, "primary")
    if missing == "different-home":
        env["HERMES_HOME"] = str(root / "other-hermes")
    elif missing == "site":
        (primary / "site").rmdir()
    else:
        (primary / "python-path").unlink()
    if marker is not None:
        env["HERMES_CONTROL_CHECK_COPY"] = marker
    if sandbox is not None:
        env["CODEX_SANDBOX"] = sandbox
    result = run_check(env)
    if marker == "1" and sandbox is None:
        assert result.returncode == 17
        assert "primary verify dir is unreadable" in result.stderr
    else:
        assert result.returncode == 2
        assert result.stderr == "sandbox-check: not set up; run scripts/verify-setup.sh once, outside the sandbox\n"
        assert not (root / "calls").exists()


def test_primary_wins_even_when_copy_is_opted_in(machine):
    root, env = machine
    env["HERMES_CONTROL_CHECK_COPY"] = "1"
    install_check_stub(Path(env["HERMES_HOME"]) / "control/verify", env, "primary")
    install_check_stub(copy_path(env), env, "copy")
    assert run_check(env).returncode == 17
    assert (root / "calls").read_text().splitlines()[0] == "primary"


@pytest.mark.parametrize("selection", [DESELECTED[0], "./" + DESELECTED[0],
                                        str(REPO / DESELECTED[0]), DESELECTED[0] + "[case]"])
def test_explicitly_selected_tests_are_not_deselected(machine, selection):
    root, env = machine
    env["HERMES_CONTROL_CHECK_COPY"] = "1"
    install_check_stub(copy_path(env), env, "copy")
    result = run_check(env, selection)
    assert result.returncode == 17
    args = (root / "calls").read_text().splitlines()
    assert args[-1] == selection
    assert DESELECTED[0] not in args[7:-1]
    remaining = DESELECTED[1:]
    assert args[7:-1] == [item for name in remaining for item in ("--deselect", name)]
    assert f"{len(remaining)} tests" in result.stderr
    assert "primary verify dir is unreadable" in result.stderr


def test_selecting_a_whole_file_still_deselects_its_agy_only_failures(machine):
    # Inside agy these tests cannot pass (no git in a nested worktree, no real
    # hermes CLI); running a file that holds them must not show the worker
    # failures it cannot fix.
    root, env = machine
    env["HERMES_CONTROL_CHECK_COPY"] = "1"
    install_check_stub(copy_path(env), env, "copy")
    result = run_check(env, "tests/test_community_files.py")
    assert result.returncode == 17
    args = (root / "calls").read_text().splitlines()
    assert args[-1] == "tests/test_community_files.py"
    assert args[7:-1] == [item for name in DESELECTED for item in ("--deselect", name)]
    assert f"{len(DESELECTED)} tests" in result.stderr


def test_verify_environment_never_inherits_copy_opt_in(tmp_path, monkeypatch):
    from hermes_control.engines.gemini import GeminiAdapter
    from hermes_control.verify import _verify_env, write_check_script

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HERMES_CONTROL_CHECK_COPY", raising=False)
    assert GeminiAdapter().engine_env()["HERMES_CONTROL_CHECK_COPY"] == "1"
    assert "HERMES_CONTROL_CHECK_COPY" not in os.environ
    assert "HERMES_CONTROL_CHECK_COPY" not in _verify_env()
    monkeypatch.setenv("HERMES_CONTROL_CHECK_COPY", "1")
    fake_codex = executable(tmp_path / "codex", "#!/bin/sh\nexec /usr/bin/env\n")
    script = write_check_script(str(fake_codex), tmp_path, "exit 0", path=tmp_path / "check.sh")
    result = subprocess.run([str(script)], capture_output=True, text=True, check=True)
    assert "HERMES_CONTROL_CHECK_COPY=" not in result.stdout


@pytest.mark.parametrize("ancestor", ["tests", ".git", "node_modules", ".env.local", "private.env"])
@pytest.mark.parametrize("symlink", [False, True])
def test_copy_exclusions_are_relative_to_copy_root(tmp_path, ancestor, symlink):
    helper = runpy.run_path(str(REPO / "scripts/agy-check-setup.py"))
    source = tmp_path / ancestor / "python"
    source.mkdir(parents=True)
    (source / "stdlib.py").write_text("VALUE = 1\n")
    plant_exclusions(source)
    if symlink:
        link = tmp_path / "python-link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    target = tmp_path / "copy"
    helper["copy"](source, target)
    assert (target / "stdlib.py").read_text() == "VALUE = 1\n"
    assert [path.name for path in target.iterdir()] == ["stdlib.py"]


def test_hermes_copy_has_only_allowlisted_files_and_all_root_modules(setup_machine):
    _, env, _, _, _, source = setup_machine
    package = source / "hermes_cli"
    allowed = [".py", ".pyi", ".so", ".dylib", ".json", ".yaml", ".yml", ".toml", ".txt", ".md", ".typed"]
    for suffix in allowed + [".pem", ".key", ".p12", ".sqlite", ".bin", ".sh", ".html"]:
        (package / ("data" + suffix)).write_text("data\n")
    (package / "py.typed").touch()
    (package / "credentials").write_text("unmapped data\n")
    (source / "unmapped_private.py").write_text("PRIVATE = True\n")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    dest = copy_path(env)
    assert {p.name for p in (dest / "src/hermes_cli").iterdir()} == {
        "__init__.py", "py.typed", *("data" + ext for ext in allowed)}
    assert (dest / "src/root_module.py").is_file()
    assert (dest / "src/unmapped_private.py").is_file()


def test_hermes_copy_includes_modules_missing_from_stale_mapping(setup_machine):
    _, env, _, _, site, source = setup_machine
    # An install-time finder can predate both new modules and the root module list.
    (site / "__editable___hermes_agent_1_finder.py").write_text(
        "MAPPING = " + repr({"hermes_cli": str(source / "hermes_cli")}) + "\n")
    (source / "hermes_state_pidns.py").write_text("VALUE = 45\n")
    (source / "unmapped_package").mkdir()
    (source / "unmapped_package/__init__.py").touch()
    (source / "private.key").write_text("not a module\n")
    plant_exclusions(source)
    (source / ".env.hidden.py").write_text("excluded\n")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    dest = copy_path(env) / "src"
    assert {path.name for path in dest.iterdir()} == {
        "hermes_cli", "root_module.py", "hermes_state_pidns.py"}
    assert (dest / "hermes_state_pidns.py").read_text() == "VALUE = 45\n"


def test_hermes_copy_skips_root_tests_and_scripts_modules(setup_machine):
    _, env, _, _, _, source = setup_machine
    for name in ("tests", "scripts", "other_root"):
        (source / f"{name}.py").write_text("VALUE = 45\n")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    dest = copy_path(env) / "src"
    assert (dest / "other_root.py").is_file()
    assert not (dest / "tests.py").exists()
    assert not (dest / "scripts.py").exists()


def test_hermes_copy_keeps_mapped_root_tests_module(setup_machine):
    _, env, _, _, site, source = setup_machine
    (source / "tests.py").write_text("VALUE = 45\n")
    (site / "__editable___hermes_agent_1_finder.py").write_text(
        "MAPPING = " + repr({"hermes_cli": str(source / "hermes_cli"),
                              "tests": str(source / "tests.py")}) + "\n")
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    assert (copy_path(env) / "src/tests.py").is_file()


def test_hermes_copy_scans_modules_missing_from_mapping(setup_machine):
    _, env, _, _, _, source = setup_machine
    secret = "sk-proj-" + "a" * 30
    (source / "new_module.py").write_text(f"KEY = {secret!r}\n")
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "src/new_module.py: openai-or-anthropic-key" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert not copy_path(env).exists()


@pytest.mark.parametrize(("rule", "secret"), [
    ("openai-or-anthropic-key", "sk-proj-" + "a" * 30),
    ("openai-or-anthropic-key", "sk-" + "ant-" + "b" * 30),
    ("stripe-key", "sk_live_" + "c" * 30),
])
def test_copy_uses_plugin_secret_rules(setup_machine, rule, secret):
    _, env, _, _, site, _ = setup_machine
    (site / "example_dep/data.txt").write_text(secret)
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "site/example_dep/data.txt" in result.stderr
    assert rule in result.stderr
    assert secret not in result.stdout + result.stderr
    assert not copy_path(env).exists()


def test_failed_package_rebuild_preserves_previous_copy(setup_machine):
    _, env, _, _, site, _ = setup_machine
    assert run_setup(setup_machine).returncode == 0
    previous = (copy_path(env) / "stamp").read_bytes()
    (site / "single_dep.py").unlink()
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert (copy_path(env) / "stamp").read_bytes() == previous
    assert subprocess.run([str(copy_path(env) / "python/bin/python3.11")]).returncode == 0
    assert not list(copy_path(env).parent.glob(".check-*"))


def test_failed_promotion_restores_previous_copy(setup_machine):
    root, env, _, _, _, _ = setup_machine
    assert run_setup(setup_machine).returncode == 0
    previous = (copy_path(env) / "stamp").read_bytes()
    shim = root / "shim.py"
    shim.write_text(shim.read_text().replace("sys.argv = sys.argv[1:]", """
from pathlib import Path
original_rename = Path.rename
def fail_promotion(self, target):
    if (self.name.startswith('.check-') and not self.name.startswith('.check-old-')
            and Path(target).name == 'check'):
        raise OSError('simulated promotion failure')
    return original_rename(self, target)
Path.rename = fail_promotion
sys.argv = sys.argv[1:]
"""))
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "simulated promotion failure" in result.stderr
    assert (copy_path(env) / "stamp").read_bytes() == previous
    assert subprocess.run([str(copy_path(env) / "python/bin/python3.11")]).returncode == 0
    assert not list(copy_path(env).parent.glob(".check-*"))


def test_staging_smoke_test_uses_staged_interpreter_and_import_path(setup_machine):
    root, env, _, base, _, _ = setup_machine
    executable(base / "bin/python3.11", "#!/bin/sh\n" +
               'printf "%s\\n" "$0" "$PYTHONPATH" "$@" > "$CALLS"\n')
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    args = (root / "calls").read_text().splitlines()
    staged_python = Path(args[0])
    staging = staged_python.parents[2]
    assert staging.name.startswith(".check-")
    assert staging.parent == copy_path(env).parent
    assert args[1] == f"{staging}/site:{staging}/src"
    assert args[2] == "-c"
    assert args[3] == "import pytest, yaml, fastapi, hermes_cli.config, hermes_state, hermes_state_common"


def test_failed_smoke_test_keeps_previous_copy_and_hides_output(setup_machine):
    _, env, _, base, _, _ = setup_machine
    assert run_setup(setup_machine).returncode == 0
    previous = (copy_path(env) / "stamp").read_bytes()
    executable(base / "bin/python3.11", "#!/bin/sh\necho unsafe-import-output >&2\nexit 9\n")
    result = run_setup(setup_machine)
    assert result.returncode != 0
    assert "smoke test failed" in result.stderr
    assert "unsafe-import-output" not in result.stdout + result.stderr
    assert (copy_path(env) / "stamp").read_bytes() == previous
    assert subprocess.run([str(copy_path(env) / "python/bin/python3.11")]).returncode == 0
    assert not list(copy_path(env).parent.glob(".check-*"))


def test_no_agy_copy_reports_retained_older_copy(setup_machine):
    _, env, _, _, _, _ = setup_machine
    assert run_setup(setup_machine).returncode == 0
    previous = (copy_path(env) / "stamp").read_bytes()
    result = run_setup(setup_machine, "--no-agy-copy")
    assert result.returncode == 0, result.stderr
    assert "older agy copy left in place" in result.stdout + result.stderr
    assert str(copy_path(env)) in result.stdout + result.stderr
    assert (copy_path(env) / "stamp").read_bytes() == previous


def test_copy_scan_reads_current_plugin_rules_for_every_text_file(tmp_path, monkeypatch, capsys):
    import re
    from hermes_control import secret_scan

    helper = runpy.run_path(str(REPO / "scripts/agy-check-setup.py"))
    sentinel = "new-provider-" + "sentinel"
    monkeypatch.setattr(secret_scan, "_RULES", (("new-provider-rule", re.compile(sentinel)),))
    (tmp_path / "extensionless").write_text(sentinel)
    with pytest.raises(ValueError, match="secret scan"):
        helper["scan"](tmp_path)
    output = capsys.readouterr()
    assert output.err == "extensionless: new-provider-rule\n"
    assert sentinel not in output.out + output.err


def test_agy_copy_succeeds_without_yaml_in_hermes_site_and_runs_smoke_test(setup_machine):
    _, env, repo, base, site, source = setup_machine
    shutil.copy2(REPO / "scripts/agy-check-packages.txt", repo / "scripts/agy-check-packages.txt")
    hermes_packages = (
        "annotated_doc", "annotated_types", "anyio", "attr", "certifi",
        "charset_normalizer", "click", "fastapi", "httpx", "httpx2", "idna",
        "jiter", "psutil", "ptyprocess", "pydantic", "pydantic_core",
        "python_multipart", "requests", "rich", "ruamel", "sniffio",
        "starlette", "typing_extensions", "typing_inspection", "urllib3",
    )
    for name in hermes_packages:
        (site / f"{name}.py").write_text("VALUE = 1\n")
    assert not (site / "yaml").exists() and not (site / "yaml.py").exists()
    (source / "hermes_cli/config.py").write_text("CONFIG = True\n")
    (source / "hermes_state.py").write_text("STATE = True\n")
    (source / "hermes_state_common.py").write_text("COMMON = True\n")
    executable(
        Path(env["PATH"].split(":")[0]) / "uv",
        "#!/bin/sh\n"
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
    executable(
        base / "bin/python3.11",
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -S "$@"\n',
    )
    result = run_setup(setup_machine)
    assert result.returncode == 0, result.stderr
    assert "pytest 9.1.1 and PyYAML 6.0.3" in result.stdout
    dest = copy_path(env)
    assert (dest / "site/yaml/__init__.py").is_file()
    (Path(env["HERMES_HOME"]) / "control/verify/python-path").unlink()
    copy_check = run_check({**env, "HERMES_CONTROL_CHECK_COPY": "1"})
    assert copy_check.returncode == 0, copy_check.stderr
    (site / "fastapi.py").unlink()
    missing = run_setup(setup_machine)
    assert missing.returncode != 0
    assert "listed third-party package(s) missing from Hermes site-packages: fastapi" in missing.stderr
