#!/usr/bin/env bash
# Link this repo into a Hermes home (default ~/.hermes; HERMES_HOME overrides) and
# enable the plugin:
#   <home>/plugins/hermes_control                      -> <repo>/hermes_control
#   <home>/skills/software-development/hermes-control  -> <repo>/skills/hermes-control
#   ~/.local/bin/hc-update                             -> <repo>/scripts/hc-update
# Safe to re-run, including after moving the repo. Anything already at one of those
# paths that is not a symlink is moved to <home>/.hermes-control-replaced/<time>/,
# never deleted.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
home="$HERMES_HOME"
stamp="$(date +%Y%m%d-%H%M%S)"

link() {
    local target="$1" path="$2"
    mkdir -p "$(dirname "$path")"
    if [ -e "$path" ] && [ ! -L "$path" ]; then
        local keep="$home/.hermes-control-replaced/$stamp"
        mkdir -p "$keep"
        mv "$path" "$keep/"
        echo "moved the existing $path to $keep/"
    fi
    ln -sfn "$target" "$path"
    echo "linked $path -> $target"
}

link "$repo/hermes_control" "$home/plugins/hermes_control"
link "$repo/skills/hermes-control" "$home/skills/software-development/hermes-control"
link "$repo/scripts/hc-update" "$HOME/.local/bin/hc-update"

# Also what adds the `control` toolset (see hermes_control/plugin.yaml).
hermes plugins enable hermes_control
