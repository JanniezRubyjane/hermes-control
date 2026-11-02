#!/usr/bin/env bash
# Run once, and again after every Hermes update: put in place what the
# sandboxed check cannot fetch itself. A Hermes-Control task's verify command runs in
# codex's sandbox (no network, writes only in the task worktree), so pytest cannot come
# from uv there as it does in scripts/check.sh. This installs it into
# ~/.hermes/control/verify/ and records Hermes's Python and import path. Unless skipped,
# also stage a read-only toolchain in the config home for Gemini's sandbox.
set -euo pipefail

agy_copy=true
case "${1:-}" in
    --no-agy-copy) agy_copy=false; shift ;;
esac
if [ "$#" -ne 0 ]; then
    echo 'Usage: scripts/verify-setup.sh [--no-agy-copy]' >&2
    exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/lib/hermes-python.sh"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
resolve_hermes_python

dest="$HERMES_HOME/control/verify"
mkdir -p "$dest"
rm -rf "$dest/site"
uv pip install --quiet --target "$dest/site" --python "$HERMES_PYTHON" pytest==9.1.1 PyYAML==6.0.3
printf '%s\n' "$HERMES_PYTHON" > "$dest/python-path"
path_count=0
farm_count=0
if [ -n "$HERMES_PYTHON_PATH" ]; then
    printf '%s\n' "$HERMES_PYTHON_PATH" | tr ':' '\n' > "$dest/hermes-path"
    path_count="$(wc -l < "$dest/hermes-path" | tr -d '[:space:]')"
    # Keep diagnostics separate from the stdout count. A file also works in
    # sandboxes that deny /dev/fd process substitutions.
    farm_stderr="$(mktemp "${TMPDIR:-/tmp}/hermes-farm-stderr.XXXXXX")"
    trap 'rm -f "$farm_stderr"' EXIT
    if ! farm_count="$("$HERMES_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/hermes-src-farm.py" \
        --hermes-path "$dest/hermes-path" --out "$dest/hermes-src" 2> "$farm_stderr")"; then
        sed 's/^/verify-setup: /' "$farm_stderr" >&2
        echo "verify-setup: Hermes source farm failed; the full Hermes import path cannot be used because the Hermes repo root's tests package would shadow this repo's tests; fix the Hermes install and re-run scripts/verify-setup.sh" >&2
        rm -rf "$dest/hermes-src"
        rm -f "$dest/hermes-path" "$dest/python-path"
        copy="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check"
        if [ -e "$copy" ] || [ -L "$copy" ]; then
            if ! "$agy_copy"; then
                echo "verify-setup: --no-agy-copy: the agy copy at $copy was left in place and was built for the previous Hermes" >&2
            else
                if ! copy_python="$("$HERMES_PYTHON" -I -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)["hermes_python"]
if not isinstance(value, str) or not value:
    raise ValueError("invalid Hermes Python in stamp")
print(value)
' "$copy/stamp" 2>/dev/null)"; then
                    copy_python=unknown
                fi
                if { [ -d "$HOME/.hermes" ] && [ "$(CDPATH= cd -P -- "$HERMES_HOME" && pwd -P)" = "$(CDPATH= cd -P -- "$HOME/.hermes" && pwd -P)" ]; } || [ "$copy_python" = "$HERMES_PYTHON" ]; then
                    copy_removed=false
                    if [ -L "$copy" ]; then
                        rm -f "$copy" && copy_removed=true
                    else
                        chmod -R u+w "$copy" && rm -rf "$copy" && copy_removed=true
                    fi
                    if "$copy_removed"; then
                        echo "verify-setup: removed the agy copy at $copy because it was built for the previous Hermes; agy workers get \"not set up\" until setup succeeds" >&2
                    else
                        echo "verify-setup: could not remove the agy copy at $copy; remove it by hand" >&2
                    fi
                else
                    echo "verify-setup: the agy copy at $copy was left in place because HERMES_HOME ($HERMES_HOME) is not the default home $HOME/.hermes and the copy was built for another Hermes Python ($copy_python)" >&2
                fi
            fi
        fi
        exit 2
    else
        cat "$farm_stderr" >&2
    fi
    rm -f "$farm_stderr"
    trap - EXIT
else
    rm -f "$dest/hermes-path"
    rm -rf "$dest/hermes-src"
fi
echo "verify-setup: pytest 9.1.1 and PyYAML 6.0.3 in $dest/site, for $HERMES_PYTHON; $path_count path entries recorded; $farm_count farm entries"
if "$agy_copy"; then
    set --
    if [ -f "$dest/hermes-path" ]; then
        set -- --hermes-path "$dest/hermes-path"
    fi
    if [ -n "$HERMES_PYTHON_PATH" ]; then
        PYTHONPATH="$HERMES_PYTHON_PATH" PYTHONNOUSERSITE=1 \
            "$HERMES_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/agy-check-setup.py" \
            --hermes-python "$HERMES_PYTHON" --verify "$dest" \
            --destination "${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check" "$@"
    else
        "$HERMES_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/agy-check-setup.py" \
            --hermes-python "$HERMES_PYTHON" --verify "$dest" \
            --destination "${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check" "$@"
    fi
elif [ -e "${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check" ]; then
    echo "verify-setup: --no-agy-copy: older agy copy left in place at ${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check"
fi
echo "A task that changes this repo can now verify with: /bin/sh scripts/sandbox-check.sh"
