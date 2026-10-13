# Verify commands

A claude worker can run the verify command through a script Hermes-Control
pre-approves before launch. That check runs in the task worktree, so the
worker can fix a failure before finishing. Its pass is not proof that the
committed change works. A codex worker usually cannot run project tools inside
its own sandbox. The controller commits the task's change and runs `verify`
itself. Only an exit code of 0 clears `unproven_reason`.

The controller uses `/bin/sh` in a detached clean checkout of the task's commit,
with no network and writes limited to that checkout and the temp directory.
It contains only committed files: no ignored or untracked files the worker
made, `node_modules`, build output, or `.pyc` files. The check must pass from
the commit alone. It must regenerate needed files offline, or the worker must
commit them. Choose a check of the new or affected tests rather than the whole
suite. Name the interpreter by absolute path: the checkout has no repository
`.venv` unless committed. Find the interpreter first, then use its full path,
for example `/absolute/repo/.venv/bin/python -m pytest -q tests/test_calc.py`.
Use plain `python3` only for standard-library-only checks.

The command must need no install or download, finish within ten minutes, and
leave the task worktree clean. A claude worker may run it before commit, so build
outputs and caches could otherwise enter the diff. For pytest, consider
`-p no:cacheprovider`. The worker receives the command in its task goal.

## Red-green proof

For a write task with `verify`, optionally set `red` to a shell command that
runs only the new or changed tests. This opts into proof after `verify` passes:
the controller runs `red` in a sandbox on base code with the committed tests
overlaid, then on a clean checkout of the task commit. Expected failure on base
(exit 1 by default) and exit 0 on the commit yield `red-green-proven`. A base
pass yields advisory `red-passed`; it does not change `unproven_reason`.
An exit 2 outside `red_exit_codes` also yields `red-green-proven` when the red
output contains `ImportError while importing test module`, `cannot import name`,
`ModuleNotFoundError: No module named`, or `has no attribute`, and the same
command exits 0 on the commit. Its proof reason is
`red: the new tests could not import what the change adds`. The green pass
guards against an environment problem. An inconclusive red leg skips green;
when `red` equals `verify` exactly, the controller reuses the verify outcome
for green.
Other failures yield `red-inconclusive`, and no changed test files yields
`red-skipped`. Without `red`, a verify pass yields `tests-passed`. Other task
verdicts are `no-verify`, `verify-failed`, `verify-blocked`, `unproven`, and
`not-committed`. A passing check with a non-null final `unproven_reason` yields
`unproven`, including a committed `.claude/` warning or a landing log failure.

Configure `proof.test_globs` (list of strings) and `proof.red_exit_codes` (list
of integers) under `plugins.entries.hermes_control.settings`. Defaults match
`tests/**`, `**/test_*.py`, `**/*_test.py`, `**/*.test.*`, and `**/*.spec.*`.
Committed tasks also carry advisory warnings when check commands name changed
non-test files, tests are deleted, or existing tests lose more assert lines
than they gain. Review the verdict, proof output, and warnings in review-pack.
