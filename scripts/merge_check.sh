#!/bin/sh
# Merge Hermes-Control task branches onto HEAD in a throwaway detached worktree,
# run a check there, and remove the worktree. The user's checkout is not touched.
#
# Usage (from inside the repo):
#   sh merge_check.sh [--unsandboxed] '<verify command>' <branch> [<branch>...]
# Exit 0 means every merge was clean and the check passed; 1 means a merge or
# check failed; 3 means merges were clean but no sandbox was available.
#
# The check runs inside `codex sandbox -P :workspace` (no network, writes only in
# the scratch worktree and $TMPDIR) - the same containment Hermes-Control uses
# for a task's verify step. Without codex, merges still run but the check does
# not. Pass --unsandboxed first to run the check bare after reading the diffs.
set -e
unsandboxed=false
if [ "${1:-}" = --unsandboxed ]; then
    unsandboxed=true
    shift
fi
[ $# -ge 2 ] || { echo "usage: merge_check.sh [--unsandboxed] '<verify command>' <branch>..." >&2; exit 2; }
verify="$1"; shift
result=
repo="$(git rev-parse --show-toplevel)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/hc-merge-check.XXXXXX")"

cleanup() {
    git -C "$repo" worktree remove --force "$scratch" >/dev/null 2>&1 || true
    git -C "$repo" worktree prune || true
}
trap cleanup EXIT

git -C "$repo" worktree prune
git -C "$repo" worktree add -q --detach "$scratch" HEAD
for branch in "$@"; do
    if git -C "$scratch" -c user.email=hermes-control@local -c user.name=Hermes-Control \
            merge -q --no-edit "$branch"; then
        echo "merge_check: merged $branch"
    else
        echo "merge_check: $branch does not merge cleanly" >&2
        exit 1
    fi
done

if [ "$unsandboxed" = true ]; then
    if command -v codex >/dev/null 2>&1; then
        echo "merge_check: --unsandboxed; running the check outside a sandbox" >&2
    else
        echo "merge_check: codex not found; running the check outside a sandbox" >&2
    fi
    ( cd "$scratch" && /bin/sh -c "$verify" ) || result=$?
elif command -v codex >/dev/null 2>&1; then
    codex sandbox -P :workspace -C "$scratch" -- /bin/sh -c "$verify" || result=$?
else
    echo "merge_check: codex not found; the check was not run (no sandbox available)" >&2
    exit 3
fi
if [ -n "${result:-}" ]; then
    echo "merge_check: check failed (exit $result)" >&2
    exit 1
fi
echo "merge_check: clean merge and check passed"
