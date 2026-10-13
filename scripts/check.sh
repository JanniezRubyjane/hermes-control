#!/usr/bin/env bash
# Run Hermes-Control's tests against the Hermes that `hermes` runs.
#
# pytest comes from a throwaway uv overlay, so the Hermes venv is never modified
# (`hermes update` re-syncs that venv from its lockfile and would drop anything
# installed into it by hand). HERMES_PYTHON picks another Hermes interpreter.
# Extra arguments go to pytest: `scripts/check.sh -m integration` runs the
# real-CLI tests.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "$(dirname "${BASH_SOURCE[0]}")/lib/hermes-python.sh"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
resolve_hermes_python

cd "$repo"
exec uv run --quiet --no-project --python "$HERMES_PYTHON" --with pytest==9.1.1 \
    -- python -m pytest "$@"
