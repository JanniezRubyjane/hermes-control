#!/usr/bin/env bash
# Remove only this checkout's installed links; retain control data unless --purge.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
home="${HERMES_HOME:-$HOME/.hermes}"
purge=false
case "${1:-}" in
    '') ;;
    --purge) purge=true ;;
    *) echo 'usage: uninstall.sh [--purge]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo 'usage: uninstall.sh [--purge]' >&2; exit 2; }

if "$purge" && [ ! -f "$home/config.yaml" ] && [ ! -f "$home/state.db" ]; then
    echo "refusing purge: $home has neither config.yaml nor state.db" >&2
    exit 2
fi
if command -v hermes >/dev/null 2>&1; then
    echo 'running hermes plugins disable hermes_control'
    hermes plugins disable hermes_control || echo 'hermes plugins disable hermes_control failed; continuing' >&2
fi

# Follow installed links without depending on the Python discovery helpers.
link_target() {
    local path="$1" target hops=0
    while [ -L "$path" ]; do
        hops=$((hops + 1))
        [ "$hops" -le 40 ] || return 1
        target="$(readlink "$path")" || return 1
        case "$target" in
            /*) path="$target" ;;
            *) path="$(dirname "$path")/$target" ;;
        esac
    done
    printf '%s/%s\n' "$(cd -P "$(dirname "$path")" && pwd)" "$(basename "$path")"
}

unlink_ours() {
    local path="$1" target
    if [ -L "$path" ]; then
        target="$(link_target "$path")" || target=''
        case "$target" in
            "$repo"/*) rm "$path"; echo "removed $path"; return ;;
        esac
    fi
    if [ -e "$path" ] || [ -L "$path" ]; then
        echo "kept $path (does not point into this repo)"
    fi
}
unlink_ours "$home/plugins/hermes_control"
unlink_ours "$home/skills/software-development/hermes-control"
unlink_ours "$HOME/.local/bin/hc-update"
if "$purge"; then
    control="$home/control"
    # verify-setup.sh, runlog.py, dispatch.py, limits.py, pauses.py,
    # verify.py and slots.py create these paths. Retain all other control data.
    rm -rf "$control/verify"
    for path in "$control"/run-*.jsonl "$control"/run-*.live.lock \
        "$control"/run-*.summary.lock "$control"/run-*.resume-*.lock \
        "$control"/usage-claude.json "$control"/usage-claude.json.* \
        "$control"/paused/*.json "$control"/paused/*.tmp \
        "$control"/checks/*.sh "$control"/slots/claude/slot-*.lock \
        "$control"/slots/codex/slot-*.lock; do
        if [ -f "$path" ] || [ -L "$path" ]; then rm -f "$path"; fi
    done
    rmdir "$control/paused" "$control/checks" "$control/slots/claude" \
        "$control/slots/codex" "$control/slots" "$control" 2>/dev/null || true
    echo "purged plugin files under $control (including run logs)"
else
    echo "kept $home/control and run logs; use --purge to remove them"
fi
