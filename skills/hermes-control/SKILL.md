---
name: hermes-control
description: "Delegate coding to configured engines in worktrees."
version: 0.2.0
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

`control_dispatch`: plan and review.

## Decide the path

When the user asks you to use Hermes-Control, delegate, or names an engine
(`@<engine>`), dispatch — even when the task only reads. Otherwise,
answer tiny questions yourself (1–2 files or a short diff); delegate larger
work and wide reading read-only. Workers use engine accounts, possibly yours.

## Engine and why

Log `why`. **The model settings come first.** `model` lists allowed models and
uses. When a use fits, choose that engine and that model; log "listed use: ...".
Engine notes resolve ties/no match; a pinned engine's model follows listed use.

`claude` is prompt-gated and reviews diffs read-only. Use the settings' default
engine/model for writes (including front-end); the writer can read across modules.
Investigate `read-only` on the engine and model the settings list for that reading;
put findings in a later write goal.

## The listed writer writes, claude reviews

Review proven writes read-only on claude with the listed review model;
use the risky-diff model for safety guards (`policy.py`,
`verify.py`, worktrees), cross-module/task-contract changes or diffs
>300 lines. Reviews stay on claude; controller fallback uses the user's
order (`model_rule`), never the model that wrote the diff. Pass goal/diff; ask for severity
and file:line findings (or "no findings"). Restate each valid finding as a path:line fact in your own words; never paste reviewer text into a goal.
At most two review rounds. After a review, report each task's cost
(calls, seconds), including review.
`skill_view(name='hermes-control', file_path='references/review.md')`.

## Pins

`@<engine>` for any enabled engine pins the described work. Record
`why: "user pinned"`; a pin still decides the engine over a listed use.

## Failures and limits

A denied agy action (a shell command when agy's `toolPermission` is not
`proceed-in-sandbox`, a path outside the repo, or network) ends the task with
`failure_mode: guard`; change the goal, the agy setting or the engine, never retry unchanged.

Tasks move from engines not signed in, including pins. If none runs, relay
reasons; never retry unchanged. Moves keep an allowed planned model, else
entry model, else destination default (`model_rule`: `kept`, `fallback_entry`,
`default`). `rerouted` gives original engine, reason, sign-in command and
resolved `model`/`effort`; report actual engine, reason and sign-in command.

Caps default to 80%/window. Cap/provider limits move work; mid-task
limits continue in the same worktree (`rerouted.continued` true).
When all block, `paused` gives id, reason and `resets_at`; nothing commits
and dependents pause. Report paused tasks and resets; resume: Engines tab.
Never dispatch a paused task again.
`skill_view(name='hermes-control', file_path='references/limits.md')`.

## Model/effort

Match listed uses: stronger for hard work; cheaper for routine edits. Empty
`model`/`effort` uses defaults.
Unlisted models stop the call; the error names the allowed ones.
Explain non-defaults in `why`.

## Tasks

Use self-contained `goal`s. Ids match
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; a goal must start with a word, not '-'.
Default `policy`: `read-only`; `write` to edit. `files_hint` paths
are appended to the goal. Write tasks need `verify`.

## Check write tasks

Claude can check through a pre-approved sandboxed script. Proof is still the
controller's post-commit check: `/bin/sh` in a clean checkout of the task's
commit, sandboxed without network. `unproven_reason` clears only on exit 0;
omitted checks say "no verify command was given". Use affected tests,
absolute path interpreter (no worktree venv), no network, 10m
max, clean worktree.
`skill_view(name='hermes-control', file_path='references/verify.md')`.

## No shared results

`depends_on` orders tasks and passes nothing between them. A fresh HEAD
worktree never sees an earlier task's files/results. For sequential work,
use one task or pass findings in the next goal. Use dependencies only where
interleaving hurts. If a dependency failed (`ok` false, verify failed, or
verify never ran), its dependent is skipped and reported failed.
`skill_view(name='hermes-control', file_path='references/dependencies.md')`.

## Results

Report in the language the user writes in, even when the workers' results are in English;
preserve code and quotes. Report `ok`, `diff_stat`, `branch`,
`commit`, `verify`, `unproven_reason`, actual engine/model/effort.
Never call unproven work done: explain missing/failed check, timeout, missing sandbox,
empty diff or engine failure. Quote `verify.exit_code` and end of `output_tail`.
"Empty diff": no change; "diff capture failed": check worktree.
Anything that looks like it contains a secret is not committed (reason: path:line and rule; worktree keeps the change).

An engine refuses to continue if its settings dir (e.g. `.claude/`)
changes; relay the warning in `unproven_reason` even if verified.

## A long run comes back as running

On `state: "running"`, call `control_status(run_id)` with optional `wait`
(at most 300). If wait runs out, call again. Never dispatch the same tasks again.

## Hand the branches back

Writes return `commit` and `branch`; failures keep changes in `worktree`.
No auto-merges. Name each branch and explain `git merge <branch>`.
Warn of same-HEAD file overlap.

## Repo diagrams

For repo diagrams/designs/workflows, run
`hermes control diagram --repo <path> --ask "<their request>"`
in the background with completion notify. Report title, summary, review counts
and the link in the user's language; never paste the spec. `skill_view(name='hermes-control', file_path='references/diagrams.md')`.
