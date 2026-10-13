# Shared Bash helpers. Do not resolve the Python symlink: its venv path matters.
hermes_realpath() {
    local path="$1" target hops=0
    while :; do
        path="$(cd -P "$(dirname "$path")" && pwd)/$(basename "$path")" || return 1
        [ -L "$path" ] || break
        hops=$((hops + 1))
        [ "$hops" -le 40 ] || return 1
        target="$(readlink "$path")" || return 1
        case "$target" in
            /*) path="$target" ;;
            *) path="$(dirname "$path")/$target" ;;
        esac
    done
    printf '%s\n' "$path"
}

hermes_is_python() {
    case "${1##*/}" in python*) ;; *) return 1 ;; esac
    [ -x "$1" ] && "$1" -c 'import sys; assert sys.version_info.major == 3; import hermes_cli' >/dev/null 2>&1
}

# A watchdog keeps version discovery bounded on systems without GNU timeout.
hermes_version() {
    local launcher="$1" first rest output version_pid watchdog_pid
    # A shebang-only placeholder has no version command to run.
    if {
        IFS= read -r first || true
        IFS= read -r rest || true
        [[ "$first" == '#!'* ]] && [ -z "$rest" ] && [ -z "$(cat)" ]
    } < "$launcher"; then
        return 0
    fi
    # Capture in a file so children left behind by a stopped wrapper cannot
    # keep a command-substitution pipe open beyond the deadline.
    output="$(mktemp "${TMPDIR:-/tmp}/hermes-version.XXXXXX")" || return 0
    "$launcher" --version > "$output" 2>/dev/null &
    version_pid=$!
    (sleep 5; kill "$version_pid" 2>/dev/null || true) >/dev/null 2>&1 &
    watchdog_pid=$!
    wait "$version_pid" 2>/dev/null || true
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    cat "$output"
    rm -f "$output"
}

resolve_hermes_python() {
    local launcher line candidate='' directory version
    local -a words tried=()
    if [ -n "${HERMES_PYTHON:-}" ]; then
        tried+=("$HERMES_PYTHON")
        candidate="$(command -v "$HERMES_PYTHON")" || candidate=''
    else
        launcher="$(command -v hermes)" || launcher=''
        if [ -n "$launcher" ]; then
            launcher="$(hermes_realpath "$launcher")" || launcher=''
        fi
        if [ -n "$launcher" ]; then
            IFS= read -r line < "$launcher" || true
            if [[ "$line" == '#!'* ]]; then
                read -r -a words <<< "${line#\#!}"
                candidate="${words[0]:-}"
                if [ "${candidate##*/}" = env ]; then
                    candidate="${words[1]:-}"
                    if [ "$candidate" = '-S' ]; then
                        candidate="${words[2]:-}"
                    fi
                    case "${candidate##*/}" in
                        python*) candidate="$(command -v "$candidate")" || candidate='' ;;
                        *) candidate='' ;;
                    esac
                fi
                case "${candidate##*/}" in
                    python*)
                        tried+=("$candidate")
                        if ! hermes_is_python "$candidate"; then candidate=''; fi
                        ;;
                    *) candidate='' ;;
                esac
            fi
            if [ -z "$candidate" ]; then
                directory="$(dirname "$launcher")"
                for candidate in "$directory/python" "$directory/python3"; do
                    tried+=("$candidate")
                    if hermes_is_python "$candidate"; then break; fi
                    candidate=''
                done
            fi
            if [ -z "$candidate" ]; then
                tried+=("$launcher --version (Install directory)")
                version="$(hermes_version "$launcher")" || version=''
                directory=''
                while IFS= read -r line; do
                    case "$line" in
                        'Install directory: '*)
                            directory="${line#Install directory: }"
                            directory="${directory%$'\r'}"
                            break
                            ;;
                    esac
                done <<< "$version"
                if [ -n "$directory" ]; then
                    for candidate in "$directory/venv/bin/python" "$directory/.venv/bin/python" "$directory/venv/bin/python3"; do
                        tried+=("$candidate")
                        if hermes_is_python "$candidate"; then break; fi
                        candidate=''
                    done
                fi
            fi
        fi
    fi
    if [ -z "$candidate" ] || ! hermes_is_python "$candidate"; then
        echo 'set HERMES_PYTHON to the Python that runs Hermes (must import hermes_cli)' >&2
        if [ "${#tried[@]}" -gt 0 ]; then
            printf 'tried: %s\n' "${tried[@]}" >&2
        else
            echo 'tried: hermes on PATH (not found)' >&2
        fi
        return 1
    fi
    HERMES_PYTHON="$candidate"
    export HERMES_PYTHON
}
