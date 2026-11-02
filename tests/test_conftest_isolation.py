"""The suite never reads the operator's real ~/.hermes, even for import-time settings."""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_import_time_engine_names_ignore_the_real_config():
    from hermes_control.engines import registry

    # An empty test home enables no optional engine, whatever the real config says.
    assert registry.ENGINE_NAMES == ("claude", "codex")


def test_registry_is_computed_under_import_time_test_home(tmp_path):
    from conftest import IMPORT_HERMES_HOME

    assert Path(IMPORT_HERMES_HOME).resolve() != (Path.home() / ".hermes").resolve()
    # A fresh interpreter observes the home at the registry's actual config read,
    # before pytest's per-test fixture can replace it.
    script = f"from pathlib import Path\nROOT = Path({str(ROOT)!r})\n" + '''
import os
import runpy
from unittest.mock import patch

namespace = runpy.run_path(str(ROOT / "tests" / "conftest.py"))
home = namespace["IMPORT_HERMES_HOME"]
assert Path(home).resolve() != (Path.home() / ".hermes").resolve()
seen = []
def config():
    seen.append(os.environ["HERMES_HOME"])
    return {}
with patch("hermes_cli.config.load_config_readonly", side_effect=config):
    from hermes_control.engines import registry
assert seen and all(path == home for path in seen), seen
assert registry.ENGINE_NAMES == ("claude", "codex")
print(home)
'''
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(tmp_path)}, cwd=ROOT, timeout=20)
    assert proc.returncode == 0, proc.stderr
    # The import-time directory is also removed at interpreter exit.
    assert not Path(proc.stdout.strip()).exists()
