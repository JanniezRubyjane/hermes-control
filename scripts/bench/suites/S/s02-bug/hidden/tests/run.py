# hc-bench-canary: 789ac5d9f52795df
"""Run the repository's plain assertion tests, including newly added test files."""
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
count = 0
for path in sorted(Path(__file__).parent.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in sorted(vars(module)):
        if name.startswith("test_") and callable(getattr(module, name)):
            getattr(module, name)()
            count += 1
print(f"{count} assertions groups passed")
