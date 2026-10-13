#!/bin/sh
# The verify command for a Hermes-Control task that changes this repo: the test suite,
# run inside the sandbox with what scripts/verify-setup.sh installed outside it.
# Usage: /bin/sh scripts/sandbox-check.sh [pytest arguments, e.g. tests/test_limits.py]
verify_home="$HOME/.hermes"
if [ -n "${HERMES_HOME:-}" ] && [ -f "$HERMES_HOME/control/verify/python-path" ]; then
    verify_home="$HERMES_HOME"
fi
dest="$verify_home/control/verify"
if [ ! -r "$dest/python-path" ] || [ ! -d "$dest/site" ]; then
    echo "sandbox-check: not set up; run scripts/verify-setup.sh once, outside the sandbox" >&2
    exit 2
fi
python="$(cat "$dest/python-path")"
cd "$(dirname "$0")/.." || exit 2
exec env PYTHONPATH="$dest/site" "$python" -m pytest -q -p no:cacheprovider "$@"
