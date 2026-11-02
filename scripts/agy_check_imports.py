"""pytest -p scripts.agy_check_imports: report loaded third-party import roots.

Run on the primary Hermes toolchain after an update, then review the roots against
agy-check-packages.txt. Paths and module contents are deliberately not reported.
"""

import sys
import sysconfig
from pathlib import Path


def pytest_sessionfinish(session, exitstatus):
    sites = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
    sites.update(Path(entry).resolve() for entry in sys.path
                 if entry and Path(entry).name == "site-packages")
    roots = set()
    # Use all loaded submodules too: namespace roots may have no __file__.
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename and any(Path(filename).resolve().is_relative_to(site) for site in sites):
            root = name.split(".")[0]
            # The venv bootstrap is not needed by the relocated base interpreter.
            if root != "_virtualenv" and not root.startswith("__editable__"):
                roots.add(root)
    print("\nagy third-party import roots (review before updating the list):")
    print("\n".join(sorted(roots)))
