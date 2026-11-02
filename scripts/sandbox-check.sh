#!/bin/sh
# The verify command for a Hermes-Control task that changes this repo: the test suite,
# run inside the sandbox with what scripts/verify-setup.sh installed outside it.
# Usage: /bin/sh scripts/sandbox-check.sh [pytest arguments, e.g. tests/test_limits.py]
verify_home="$HOME/.hermes"
# Setup creates control/verify before it can fail; keep that home's failure visible.
# An inherited profile home that never ran setup still uses the default home.
if [ -n "${HERMES_HOME:-}" ] && [ -d "$HERMES_HOME/control/verify" ]; then
    verify_home="$HERMES_HOME"
fi
dest="$verify_home/control/verify"
if [ ! -r "$dest/python-path" ] || [ ! -d "$dest/site" ]; then
    copy="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-control/check"
    if [ "${HERMES_CONTROL_CHECK_COPY:-}" = 1 ] && [ "${CODEX_SANDBOX+x}" != x ] && [ -r "$copy/python-path" ]; then
        python="$(cat "$copy/python-path")"
        cd "$(dirname "$0")/.." || exit 2
        # Respect explicit node IDs (including absolute paths and parameterized
        # IDs). Selecting a whole file still deselects these tests: they cannot
        # pass inside agy. Prepend the remaining deselections in order.
        deselected=0
        for test in \
            tests/test_watch.py::test_the_real_hermes_cli_runs_it \
            tests/test_community_files.py::test_tracked_public_pngs_have_only_approved_chunks \
            tests/test_community_files.py::test_tracked_public_files_have_no_thai_script \
            tests/test_community_files.py::test_tracked_public_files_have_no_private_markers
        do
            selected=false
            for arg do
                selection="${arg#./}"
                selection="${selection#"$PWD"/}"
                case "$selection" in "$test"|"$test"\[*) selected=true ;; esac
            done
            if [ "$selected" = false ]; then
                set -- --deselect "$test" "$@"
                deselected=$((deselected + 1))
            fi
        done
        echo "sandbox-check: using agy copy because the primary verify dir is unreadable or missing; $deselected tests are left to the post-commit verify" >&2
        exec env PYTHONNOUSERSITE=1 PYTHONPATH="$copy/site:$copy/src" "$python" -m pytest -q -p no:cacheprovider \
            "$@"
    fi
    echo "sandbox-check: not set up; run scripts/verify-setup.sh once, outside the sandbox" >&2
    exit 2
fi
python="$(cat "$dest/python-path")"
cd "$(dirname "$0")/.." || exit 2
pythonpath="$dest/site"
if [ -r "$dest/hermes-path" ]; then
    skip_root=false
    if [ -d "$dest/hermes-src" ]; then
        pythonpath="$pythonpath:$dest/hermes-src"
        skip_root=true
    fi
    while IFS= read -r entry || [ -n "$entry" ]; do
        if [ "$skip_root" = true ]; then
            skip_root=false
            continue
        fi
        pythonpath="$pythonpath:$entry"
    done < "$dest/hermes-path"
fi
exec env PYTHONNOUSERSITE=1 PYTHONPATH="$pythonpath" "$python" -m pytest -q -p no:cacheprovider "$@"
