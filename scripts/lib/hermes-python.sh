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
    [ -x "$1" ] || return 1
    if [ -n "${HERMES_PYTHON_PATH:-}" ]; then
        PYTHONPATH="$HERMES_PYTHON_PATH" "$1" -c 'import sys; assert sys.version_info.major == 3; import hermes_cli' >/dev/null 2>&1
    else
        "$1" -c 'import sys; assert sys.version_info.major == 3; import hermes_cli' >/dev/null 2>&1
    fi
}

# A watchdog bounds probes on systems without GNU timeout.
hermes_bounded_output() {
    local seconds="$1" output version_pid watchdog_pid status=0
    shift
    # Capture in a file so children left behind by a stopped wrapper cannot
    # keep a command-substitution pipe open beyond the deadline.
    output="$(mktemp "${TMPDIR:-/tmp}/hermes-version.XXXXXX")" || return 1
    "$@" > "$output" 2>/dev/null &
    version_pid=$!
    (sleep "$seconds"; kill "$version_pid" 2>/dev/null || true) >/dev/null 2>&1 &
    watchdog_pid=$!
    wait "$version_pid" 2>/dev/null || status=$?
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    cat "$output"
    rm -f "$output"
    return "$status"
}

hermes_launcher_output() {
    local launcher="$1" flag="$2" seconds="$3" first rest
    # A shebang-only placeholder has no version command to run.
    if {
        IFS= read -r first || true
        IFS= read -r rest || true
        [[ "$first" == '#!'* ]] && [ -z "$rest" ] && [ -z "$(cat)" ]
    } < "$launcher"; then
        return 0
    fi
    hermes_bounded_output "$seconds" "$launcher" "$flag"
}

hermes_version() {
    # Older launchers may print their install directory even on a nonzero exit.
    hermes_launcher_output "$1" --version 5 || true
}

hermes_install_directory() {
    local line
    while IFS= read -r line; do
        case "$line" in
            'Install directory: '*)
                line="${line#Install directory: }"
                printf '%s\n' "${line%$'\r'}"
                return 0
                ;;
        esac
    done
    return 1
}

hermes_runtime_error() {
    echo "Hermes runtime $1 failed; set HERMES_PYTHON to the managed interpreter and HERMES_PYTHON_PATH to the repo root first, then the env site-packages, colon-separated" >&2
    return 2
}

hermes_runtime() {
    local launcher="$1" command parser python directory paths
    # Give the new flag two seconds, leaving the existing five-second version
    # fallback bounded even when an old launcher hangs for unknown flags.
    command="$(hermes_launcher_output "$launcher" --print-runtime-command 2)" || return 1
    [ -n "$command" ] || return 1
    parser="$(command -v python3)" || { hermes_runtime_error 'python3 parser discovery'; return 2; }
    "$parser" -I -c 'import sys; assert sys.version_info.major == 3' >/dev/null 2>&1 || {
        hermes_runtime_error 'python3 parser execution'; return 2;
    }
    python="$("$parser" -I -c '
import json, sys
argv = json.loads(sys.argv[1])
assert isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
print(argv[0])
' "$command" 2>/dev/null)" || return 1
    [ -f "$python" ] && [ -x "$python" ] || return 1
    directory="$(hermes_version "$launcher")"
    directory="$(hermes_install_directory <<< "$directory")" || {
        hermes_runtime_error 'Install directory discovery'; return 2;
    }
    [ -d "$directory" ] || { hermes_runtime_error 'Install directory validation'; return 2; }
    paths="$(hermes_bounded_output 20 env HERMES_DISABLE_LAZY_INSTALLS=1 "$python" -I -c '
import json, os, sys
plain = set(sys.path)
root = sys.argv[1]
sys.path.insert(0, root)
import hermes_bootstrap
import hermes_cli
paths = [root]
for entry in sys.path:
    if entry and entry not in plain and entry not in paths and os.path.isdir(entry):
        paths.append(entry)
print(json.dumps(paths))
' "$directory")" || { hermes_runtime_error 'bootstrap probe (20 second limit)'; return 2; }
    paths="$("$python" -I -c '
import json, sys
paths = json.loads(sys.argv[1])
assert isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths)
print(":".join(paths))
' "$paths" 2>/dev/null)" || { hermes_runtime_error 'bootstrap import path'; return 2; }
    HERMES_PYTHON="$python"
    HERMES_PYTHON_PATH="$paths"
    export HERMES_PYTHON HERMES_PYTHON_PATH
}

resolve_hermes_python() {
    local launcher line candidate='' directory version runtime_status
    local -a words tried=()
    if [ -n "${HERMES_PYTHON:-}" ]; then
        HERMES_PYTHON_PATH="${HERMES_PYTHON_PATH:-}"
        if [ -n "$HERMES_PYTHON_PATH" ]; then
            directory="${HERMES_PYTHON_PATH%%:*}"
            if [ ! -d "$directory" ] || { [ ! -d "$directory/hermes_cli" ] && [ ! -f "$directory/hermes_cli.py" ]; }; then
                echo 'HERMES_PYTHON_PATH first entry must be the Hermes repo root, a directory containing hermes_cli' >&2
                return 1
            fi
        fi
        tried+=("$HERMES_PYTHON")
        candidate="$(command -v "$HERMES_PYTHON")" || candidate=''
    else
        HERMES_PYTHON_PATH=''
        launcher="$(command -v hermes)" || launcher=''
        if [ -n "$launcher" ]; then
            runtime_status=0
            hermes_runtime "$launcher" || runtime_status=$?
            if [ "$runtime_status" -eq 0 ]; then return 0; fi
            if [ "$runtime_status" -ne 1 ]; then return 1; fi
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
        echo 'for managed Hermes, also set HERMES_PYTHON_PATH to the repo root first, then the env site-packages, colon-separated' >&2
        if [ "${#tried[@]}" -gt 0 ]; then
            printf 'tried: %s\n' "${tried[@]}" >&2
        else
            echo 'tried: hermes on PATH (not found)' >&2
        fi
        return 1
    fi
    HERMES_PYTHON="$candidate"
    export HERMES_PYTHON HERMES_PYTHON_PATH
}
