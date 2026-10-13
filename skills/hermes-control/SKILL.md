---
name: hermes-control
description: "Delegate coding tasks to claude and codex in worktrees."
version: 0.1.0
author: JanniezRubyjane
license: MIT
platforms: [macos, linux]
prerequisites:
  commands: [claude, codex]
metadata:
  hermes:
    tags: [Orchestration, Delegation, Coding, Worktree]
---

# Hermes-Control

Use `control_dispatch`; you plan and review.

## Decide the path first

When the user asks you to use Hermes-Control, to delegate, or names an engine
(`@claude`, `@codex`), dispatch — even when the task only reads. Otherwise,
answer tiny questions yourself (one or two files or a short diff); delegate
larger work and wide reading via a read-only brief. Workers use their engine's
account, not necessarily yours.

## Choose an engine per task, and say why

Every task needs logged `why`. **The model settings come first.** The `model`
field lists allowed models and uses. When a use fits, choose that engine and
that model; record "listed use: ...". Use engine notes for ties or no match.
A pin still decides the engine; choose its model by listed use.

`claude` is prompt-gated; use it for read-only diff review. `codex` runs inside
an OS sandbox; use its default write model, including front-end work. It can
read across modules. For investigation, dispatch `read-only` on the engine and
model the settings list for that reading; put findings in a later write goal.

## Codex writes, claude reviews

After a proven codex write, review its diff read-only on claude with the
settings' review model; use the settings' risky-diff model for the safety guard
(`policy.py`, `verify.py`, worktrees), cross-module or task-contract changes,
or diffs over ~300 lines. If claude cannot run, review on codex with the
settings' hard-problems model, never the model that wrote the diff.
Supply goal and diff; request severity and file:line findings ("no findings"
is valid). Dispatch valid findings verbatim. Allow at most two review rounds.
After a review, report each task's cost (calls, seconds), including review.
For detail, load
`skill_view(name='hermes-control', file_path='references/review.md')`.

## Respect an explicit pin

`@claude` or `@codex` anywhere pins the described work. Record
`why: "user pinned"`; a pin still decides the engine over a listed use.

## When an engine cannot run or reaches a limit

Checks move tasks from engines not signed in, including pinned ones. If
neither runs, relay both reasons; do not retry unchanged. Moved or resumed
tasks get destination policy model and effort; `rerouted` carries original
engine, reason, sign-in command and resolved `model`/`effort`. Report actual
engine, reason and sign-in command.

Caps default to 80% of any window. Before each task, cap or provider limit
moves work; mid-task limits continue in the same worktree
(`rerouted.continued` is true). If both are blocked, `paused` gives id, reason
and `resets_at`; nothing commits and dependents pause. Report paused tasks and
resets. Resume is on the Engines tab. Never dispatch a paused task again.
For detail, load
`skill_view(name='hermes-control', file_path='references/limits.md')`.

## Pick a model and effort

Match listed uses: stronger for hard work; cheaper for routine edits. Leave
`model` empty for default. Raise `effort` for hard work, lower it for mechanical
work, or leave it empty for configured effort. An unlisted model stops the call;
the error names the allowed ones. Explain non-default models in `why`; report
actual `model` and `effort`.

## Write good tasks

Each `goal` is self-contained. Ids must match
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; a goal must start with a word, not '-'.
Default `policy` to `read-only`; use `write` only to edit. `files_hint`
paths are appended to the goal. Give every write task a `verify` command.

## Give every write task a check

Claude can check through a pre-approved sandboxed script. Proof is
still the controller's post-commit check: `/bin/sh` in a clean checkout of the
task's commit, sandboxed without network. `unproven_reason`
clears only when it exits 0; omitted checks say "no verify command was given".
Choose affected tests, an interpreter by
absolute path (no worktree virtualenv), no network, ten-minute maximum and
clean worktree. For detail, load
`skill_view(name='hermes-control', file_path='references/verify.md')`.

## Tasks cannot hand work to each other

`depends_on` orders tasks. It passes nothing between them. A fresh HEAD
worktree never sees an earlier task's files or results. For sequential work,
use one task or dispatch the next with findings in its goal. Use dependencies
only where interleaving would hurt. If a
dependency failed (`ok` false, verify failed, or verify never ran), its
dependent is skipped and reported failed, not run. For detail, load
`skill_view(name='hermes-control', file_path='references/dependencies.md')`.

## Read the results honestly

Report in the language the user writes in, even when the workers' results are
in English; preserve code and quotes. Report `ok`, `diff_stat`, `branch`,
`commit`, `verify`, `unproven_reason`, actual engine/model/effort. Never call
unproven work done: explain missing or failed check, timeout, missing sandbox,
empty diff or engine failure. Quote `verify.exit_code` and end of `output_tail`.
"Empty diff" means nothing changed; "diff capture failed" means inspect the
worktree.

Claude refuses to continue in a worktree whose `.claude/` changed. A committed
change there carries a warning in `unproven_reason`; relay it even if verified.

## A long run comes back as running

On `state: "running"`, call `control_status(run_id)` with optional `wait`
(at most 300). If wait runs out, call again. Never dispatch the same
tasks again for a running run.

## Hand the branches back

A successful write has a `commit` on its `branch`; failed work stays
uncommitted in `worktree`. Nothing merges automatically. Name each branch and
explain `git merge <branch>`. Warn when same-HEAD tasks touch the same files.
