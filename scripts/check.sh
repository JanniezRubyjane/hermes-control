#!/usr/bin/env bash
# Run Hermes-Control's tests against the Hermes that `hermes` runs.
#
# pytest comes from a throwaway uv overlay, so the Hermes venv is never modified
# (`hermes update` re-syncs that venv from its lockfile and would drop anything
# installed into it by hand). HERMES_PYTHON picks another Hermes interpreter.
# With HERMES_PYTHON_PATH set, build a temporary hermes-path and hermes-src farm,
# use the farm on PYTHONPATH with PYTHONNOUSERSITE=1, and remove both on exit.
# Extra arguments go to pytest: `scripts/check.sh -m integration` runs the
# real-CLI tests. Exits 2 when the source farm cannot be built or the pytest
# that would run is not 9.1.1 or comes from a PYTHONPATH entry (lib/check-pytest.py).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pytest_version=9.1.1
pyyaml_version=6.0.3

source "$(dirname "${BASH_SOURCE[0]}")/lib/hermes-python.sh"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
resolve_hermes_python

cd "$repo"
if [ -n "$HERMES_PYTHON_PATH" ]; then
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/hermes-check.XXXXXX")"
    trap 'rm -rf "$temporary"' EXIT
    printf '%s\n' "$HERMES_PYTHON_PATH" | tr ':' '\n' > "$temporary/hermes-path"
    if ! "$HERMES_PYTHON" "$repo/scripts/hermes-src-farm.py" \
        --hermes-path "$temporary/hermes-path" --out "$temporary/hermes-src" >/dev/null; then
        echo "check: Hermes source farm failed; the full Hermes import path cannot be used because the Hermes repo root's tests package would shadow this repo's tests" >&2
        exit 2
    fi
    pythonpath="$temporary/hermes-src"
    if [[ "$HERMES_PYTHON_PATH" == *:* ]]; then
        pythonpath="$pythonpath:${HERMES_PYTHON_PATH#*:}"
    fi
    # Do not exec: the exit trap must remove the temporary farm after pytest.
    PYTHONNOUSERSITE=1 PYTHONPATH="$pythonpath" \
        uv run --quiet --no-project --python "$HERMES_PYTHON" --with "pytest==$pytest_version" --with "PyYAML==$pyyaml_version" \
        -- python "$repo/scripts/lib/check-pytest.py" "$pytest_version" || exit 2
    PYTHONNOUSERSITE=1 PYTHONPATH="$pythonpath" \
        uv run --quiet --no-project --python "$HERMES_PYTHON" --with "pytest==$pytest_version" --with "PyYAML==$pyyaml_version" \
        -- python -m pytest "$@"
    exit $?
fi
uv run --quiet --no-project --python "$HERMES_PYTHON" --with "pytest==$pytest_version" --with "PyYAML==$pyyaml_version" \
    -- python "$repo/scripts/lib/check-pytest.py" "$pytest_version" || exit 2
exec uv run --quiet --no-project --python "$HERMES_PYTHON" --with "pytest==$pytest_version" --with "PyYAML==$pyyaml_version" \
    -- python -m pytest "$@"
