#!/usr/bin/env bash
# Run once, and again after a Hermes update changes its Python: put in place what the
# sandboxed check cannot fetch itself. A Hermes-Control task's verify command runs in
# codex's sandbox (no network, writes only in the task worktree), so pytest cannot come
# from uv there as it does in scripts/check.sh. This installs it into
# ~/.hermes/control/verify/ and records which Python runs Hermes.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/hermes-python.sh"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
resolve_hermes_python

dest="$HERMES_HOME/control/verify"
mkdir -p "$dest"
rm -rf "$dest/site"
uv pip install --quiet --target "$dest/site" --python "$HERMES_PYTHON" pytest==9.1.1
printf '%s\n' "$HERMES_PYTHON" > "$dest/python-path"
echo "verify-setup: pytest 9.1.1 in $dest/site, for $HERMES_PYTHON"
echo "A task that changes this repo can now verify with: /bin/sh scripts/sandbox-check.sh"
