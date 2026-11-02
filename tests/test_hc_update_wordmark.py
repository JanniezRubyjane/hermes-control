"""Report the built macOS desktop wordmark after an update, without changing it."""
import shlex
import sys

import pytest

from test_scripts_portable import executable, machine, run, update_repo


FOUND = (
    "Desktop wordmark: the app still writes HERMES AGENT; "
    "the Hermes Control plugin can rename it."
)
WARNING = (
    'hc-update: warning: the desktop app no longer writes "HERMES AGENT"; '
    "the Hermes Control plugin may show the old wordmark. "
    "Open a new chat in the app to check."
)


@pytest.fixture(params=[True, False], ids=["explicit-root", "import-root"])
def desktop(machine, request):
    repo = update_repo(machine)
    root, _, _, env = machine
    checkout = root / "Hermes checkout"
    (checkout / "hermes_cli").mkdir(parents=True)
    (checkout / "hermes_cli/__init__.py").touch()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HERMES_PYTHON"] = str(executable(
        root / "python",
        "#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(checkout))
        + "\nexec " + shlex.quote(sys.executable) + ' "$@"\n',
    ))
    env["HERMES_PYTHON_PATH"] = str(checkout) + ":" + str(root / "site") if request.param else ""
    assets = checkout / (
        "apps/desktop/release/mac arm64/Hermes.app/Contents/Resources/"
        "app.asar.unpacked/dist/assets"
    )
    return repo, env, checkout, assets


@pytest.mark.parametrize("contents", ["found", "missing", "no-release"])
def test_desktop_wordmark_report(desktop, contents):
    repo, env, checkout, assets = desktop
    if contents != "no-release":
        assets.mkdir(parents=True)
        (assets / "first.js").write_text("var other = `OTHER`;\n")
        (assets / "second.js").write_text(
            "var X=`HERMES AGENT`;\n" if contents == "found" else "var X=`NEW NAME`;\n"
        )
    before = {p: p.read_bytes() for p in checkout.rglob("*") if p.is_file()}
    result = run("hc-update", env, repo=repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines().count(FOUND) == (contents == "found")
    assert result.stderr.splitlines().count(WARNING) == (contents == "missing")
    assert "Hermes-Control OK." in result.stdout
    if contents == "found":
        assert result.stdout.index("Hermes-Control OK.") < result.stdout.index(FOUND)
    assert str(checkout) not in result.stdout + result.stderr
    assert before == {p: p.read_bytes() for p in checkout.rglob("*") if p.is_file()}


@pytest.mark.parametrize("found", [True, False])
def test_failing_control_check_still_reports_wordmark(desktop, found):
    repo, env, _, assets = desktop
    assets.mkdir(parents=True)
    (assets / "main.js").write_text("HERMES AGENT" if found else "NEW NAME")
    executable(repo / "scripts/check.sh", "#!/bin/sh\nexit 7\n")
    result = run("hc-update", env, repo=repo)
    # hc-update already normalizes a failed Control check to exit 1.
    assert result.returncode == 1
    assert "Hermes-Control's tests fail" in result.stderr
    assert (FOUND in result.stdout) is found
    assert (WARNING in result.stderr) is not found
    assert "Hermes-Control OK." not in result.stdout


def test_check_mode_does_not_report_wordmark(desktop):
    repo, env, _, assets = desktop
    assets.mkdir(parents=True)
    (assets / "main.js").write_text("HERMES AGENT")
    result = run("hc-update", env, "--check", repo=repo)
    assert result.returncode == 0, result.stderr
    assert FOUND not in result.stdout + result.stderr
    assert WARNING not in result.stdout + result.stderr
