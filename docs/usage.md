# Usage

[Back to the README](../README.md). Commands run from your repository unless stated otherwise.

The planner defaults to **codex writes / claude reviews**; your model allow-list
and engine pins choose the route. Usage limits can switch engines in the same
worktree, and paused tasks continue with **Resume**.

## Use details

```bash
cd /path/to/some/git/repo
hermes
```

Then describe the work. Pin an engine with `@claude` or `@codex`. The planner splits
the work into tasks and runs each one:

- in a worktree at `.worktrees/subagent-control-<run>-<task>`, on branch
  `hermes-subagent/subagent-control-<run>-<task>`;
- committed there when the engine succeeds, then checked by the task's `verify`
  command in a sandbox (no network; writes only in the worktree);
- a `claude` worker can run that same check itself while it works, through a
  script Hermes-Control pre-approves — codex must be installed for that, just
  as it must be for the check itself.

`unproven_reason` is `null` only when that check passed. A run still going after its
`wait` budget (5 minutes by default) comes back early with `state: "running"` and a
`run_id`, instead of blocking the planner's tool call past Hermes's own deadline; the
workers keep going regardless, and the planner checks back with `control_status`.

## Stall detection

Workers have a default 30-minute task timeout and are checked at least every 30 seconds
for prolonged silence or machine sleep. Under
`plugins.entries.hermes_control.settings`, `stall_seconds` defaults to 1200
(minimum 660, above the 600-second verify limit) and `wake_grace_seconds` defaults
to 300 (minimum 60). Silence counts awake monotonic time since any non-empty stdout
line, including lines the engine adapter drops. Detection requires both the full
silence threshold and, after a possible wake, the full wake grace; sleep time is
excluded. Wall-clock jumps can log an informational `wake` notice but cannot
shorten the silence threshold. The plan event records the stall settings;
invalid settings log a warning and use the defaults. A stalled worker is stopped,
its worktree is preserved, and `failure_mode: stall` appears in its summary and
review-pack.

## Task proof and review

Each finished task also carries a `verdict`: `red-green-proven`, `tests-passed`,
`red-passed`, `red-inconclusive`, `red-skipped`, `no-verify`, `verify-failed`,
`verify-blocked`, `unproven` or `not-committed`. A write task may add an opt-in
`red` command that runs only its new or changed tests. After `verify` passes, the
controller runs `red` in the sandbox on the base commit with the task's test files
laid over it, expecting a failure (exit 1, or exit 2 when the new tests cannot
import what the change adds), then on the task commit, expecting exit 0. Both legs
give `red-green-proven`. When the new tests also pass without the change, the
verdict is `red-passed`, an advisory warning only. Settings:
`plugins.entries.hermes_control.settings.proof.test_globs` and `.red_exit_codes`.
Every committed task is also checked for `warnings`: `check_gaming` when the check
command names a non-test file the task changed (e.g. `test -f done.txt`), and
`weakened_tests` when it deletes a test file or removes asserts. None of this
changes `unproven_reason`; review-pack and the Runs timeline show the proof step.

Review a task's branch and merge it yourself:

```bash
git diff main...hermes-subagent/subagent-control-<run>-<task>
git merge --no-ff hermes-subagent/subagent-control-<run>-<task>
```

Put the repo's check in a `.hermes.md` so you don't repeat it every time. Hermes
loads that file from the working directory:

```markdown
Hermes-Control: verify Python tasks with `/abs/path/to/repo/.venv/bin/python -m pytest -q`.
```

- **Engine status and codex sign-in:** `hermes dashboard`, then **Engines**. It is in the
  sidebar's *Plugins* section at the very bottom (scroll the sidebar down; it is not the
  *Plugins* page higher up), or go straight to http://127.0.0.1:9119/engines.
- **Run logs:** `~/.hermes/control/run-<id>.jsonl`.
- **Task worktrees** are kept for review. Use `hermes control prune` to inspect
  them, then `hermes control prune --apply` to remove merged, clean, idle task
  worktrees and branches.

`hermes control lessons` reports recurring mistakes from run logs over the last
30 days, listing classes seen at least twice with counts, run/task evidence, and
fix-level hints (whether a fix belongs in tests, docs or architecture).
Use `--since DAYS`, `--min N`, and `--limit N` to narrow
the report, or `--json` for its fields. It only reads logs; any rule made from a
class must be proven against one listed past case. Codex non-zero exits are
counts only, since they often reflect normal probes.
Each class includes first/last seen dates; `--recent DAYS` (default 14) separates
older classes under “Not seen in the last N days” (`stale: true` in JSON).

`hermes control health` provides a report-only L1 self-assessment from existing
run logs, local `main` merge history, and the Hermes home's read-only `state.db`:
outcomes, seven-day rework, review findings, planner cost compared with workers,
task timing, red-green use, recurring mistakes, and up to three measured
candidates for investigation. It never calls an LLM or edits code, config, or
skills. Use `--since DAYS` (default 30), `--repo PATH` (default: the current git
top level), `--json`, or `--out FILE` to write markdown with parent directories
created. Every section names its source and explains unavailable measurements;
git metrics are skipped with a note outside a repository.

## Models and effort

The model names below are examples; your allow-list decides.
Choose which models each engine may use, what each one is for, and its effort,
on the dashboard's **Engines** tab (each engine card → **Models** → Edit). The
settings are saved in `config.yaml`:

```yaml
plugins:
  entries:
    hermes_control:
      settings:
        models:
          claude:
            default: claude-sonnet-5-5
            allowed:
              - {name: claude-haiku-5-5, use_for: read-only quick checks, effort: medium}
              - {name: claude-sonnet-5-5, use_for: reviewing diffs codex wrote, effort: medium}
              - {name: claude-opus-5-5, use_for: reviewing risky diffs, effort: medium}
          codex:
            default: gpt-6.1-sol
            allowed:
              - {name: gpt-6-luna, use_for: quick checks with a clear answer, effort: low}
              - {name: gpt-6.1-sol, use_for: every write task, effort: medium}
              - {name: gpt-6-astra, use_for: after gpt-6.1-sol failed at effort high, effort: high}
```

Write each `use_for` as the work the model is for and when to step up to the next
one: the planner reads these words and nothing else when it picks a model.

The codex card also saves a reasoning-summary setting (`none`, `concise`,
`detailed`, or `auto`); it takes effect on the next codex task.

- The planner picks a model per task using those uses. A task that names no
  model gets the default at its effort.
- A model outside the list stops the call, and the error names the allowed ones.
- An engine with an empty list allows any model, with the CLI's own default.
- Effort levels are claude's `low … max` and codex's `low … ultra`.
- In the editor, the model field suggests available names:
  codex's own list (`codex debug models`, with the effort levels each model
  takes). Claude suggests CLI aliases plus Hermes's built-in list; no account
  is contacted for the Claude model list.
  **Refresh list** reloads the catalogs; otherwise they are kept for ten minutes. A name that is not listed (say `opus[1m]`)
  gets a warning but still saves.
- Tasks are checked against the saved list right away. The planner sees the new
  list in its next Hermes session.
- A new model needs a CLI that knows it. `codex debug models` lists only the models
  the installed codex knows; upgrade codex the way it was installed (for an npm
  install, `npm install -g @openai/codex@latest`).

## Usage limits and Resume

Each engine card on the **Engines** tab shows how much of that account's usage
windows is spent (claude: 5-hour and weekly; codex: its own windows) and when
each resets. Claude usage comes only from the official CLI's last
`rate_limit_event` during a task, shown with its age; account email and subscription
come from `claude auth status`. **Refresh** re-reads that status and the saved report
without a Claude usage network call. With no valid report, usage is unknown and
never holds work back. Codex reads usage through its official `codex app-server`.
Set a cap per engine (default 80%). Then:

- Before each task, an engine over its cap, or stopped by its limit, hands the
  task to the other engine.
- A worker that hits a limit part-way is followed by the other engine **in the
  same worktree**, so nothing it wrote is lost.
- When neither engine can take a task, it is **paused**. It appears under
  **Paused tasks** with the reason and when an engine frees up. Press **Resume**
  then: claude picks its own session back up, codex starts a new session in the
  worktree, or the other engine carries on there. Nothing resumes by itself.
  **Discard** forgets a paused task and keeps its worktree.

Paused tasks are kept in `~/.hermes/control/paused/`. Details:
[limits-and-resume.md](limits-and-resume.md).

Independent tasks can overlap by setting
`plugins.entries.hermes_control.settings.parallel` in `config.yaml`, for example
`{max_tasks: 2, per_engine: {claude: 1, codex: 2}}`. `max_tasks` defaults to 1
(sequential execution); a missing `per_engine` count defaults to `max_tasks`.
`max_tasks` accepts 1–8 and engine counts must be positive integers. Dependencies
still settle before their dependents start, and results retain planned order.
Invalid settings warn and use the defaults. `per_engine` limits workers of that
engine across every run on this machine: a second dispatch waits for a free slot
and logs `slot_wait`. With the default of 1, two Hermes sessions dispatching to the
same engine run one after another, and the second waits without a time limit.
Resume is refused while the run is in progress (an OS lock released when the run
ends or its process exits, even after a crash). When that lock cannot be checked,
a run counts as over once it has logged its summary, or after 35 minutes with no
worker event.
The plan event records the settings in use. A finished run
reports `overlaps` when tasks changed the same file from one base. Engines run in
their own session, so if Hermes itself is killed the OS frees the slot while the
engine may keep running.

## Runs

The **Runs** view (the **Runs** switch at the top of the Engines tab) draws each run: the planner, then a lane per
task from the planner's reason, through each engine attempt (click one for every
step it took), to the commit, the check and the merge command, or to where it
paused. Moves between engines, usage limits and resumes label the arrows. A
running run updates live. Details: [runs-view.md](runs-view.md).

In a terminal, run this in a second pane beside a Hermes session:

```bash
hermes control watch            # always the newest run, refreshed every second
hermes control watch RUN_ID     # one run
hermes control watch --once     # print it once and exit
```

It shows the same thing: each task, the engine and model working on it with its
latest steps, where each task landed, and each engine's usage against its cap.
Ctrl-C stops it.

Each write worker is asked to end with a short report: `RESULT` (PASS, ISSUES or
BLOCKED), `RAN` (each command with its exit code and last output line),
`DEVIATIONS` and `FOLLOW-UPS`. The report is a claim, not proof: only the sandboxed
verify proves a task. review-pack shows the report and lists as `unbacked_claims`
any `RAN` command with no matching command in the worker's own run log.

`hermes control stats` lists attempt costs and routes, including a `usd` column
(`quota` for Codex when no dollar cost was recorded, `-` for other engines). Use
`--run RUN_ID` repeatedly to select
runs, `--by-model` for grouped counts and medians, and `--json` for records. JSON
attempt rows include `model_recorded`; older rows keep `model: null` and display
`(not recorded)` in the text table. To check task branches together in a temporary
worktree, run `sh scripts/merge_check.sh '<verify command>' <branch> [<branch>...]`.
The check runs in a Codex sandbox. If Codex is unavailable, merges still run,
but the check does not; the command exits 3. After reviewing the diffs, pass
`--unsandboxed` as the first argument to run the check outside the sandbox.
`hermes control review-pack RUN_ID` prints one digest with each task's full reroute
and limit history, commit, diff against its recorded task base, verification,
worker's final result text, costs, and run-log path. Older logs use the branch's
merge-base with HEAD and the last text event. Add `--diff` for full diffs or
`--json` for the same evidence as structured data.

## Prune task worktrees

Run `hermes control prune` from a repository to see its Hermes task worktrees and
task branches that have no worktree. Use `--repo PATH` to inspect another repo,
or `--json` for machine-readable output. The report includes size, tip age,
whether the branch is merged into local `main`, worktree state, and any reason
it must be kept.

`hermes control prune --apply` removes only entries with no hold reason. Paused
tasks, live runs, tracked changes, untracked scratch files, and branches not
merged into local `main` stay in place. The worktree containing the directory
you run prune from is also kept. A missing task worktree is reported as `missing`;
`--apply` prunes its Git registration before deleting its merged branch. It never
removes the main worktree, unrelated branches, or detached verify checkouts. A
Git refusal is reported in the result so you can resolve it and run the command
again.

## Evaluate routing

`HERMES_PYTHON` is the interpreter Hermes runs on; it needs PyYAML.
Run `scripts/verify-setup.sh` once so the write probes can fill their `{verify}`
placeholder with the sandbox-ready pytest command. Preview the six quota-consuming
probes with `"$HERMES_PYTHON" scripts/eval/run.py --dry-run`.
Run them with `"$HERMES_PYTHON" scripts/eval/run.py --live --out /tmp/routing-before`, then
`"$HERMES_PYTHON" scripts/eval/analyze.py /tmp/routing-before` for a table and
`analysis.json`. Repeat with another output directory after changing planner-facing
text and compare the JSON files. The runner uses a temporary Hermes profile under
the source home's `profiles/` directory. It copies config and `.env`, symlinks the
control plugin and skill, and never copies credentials or `auth.json`. Before
removing the profile, it copies run logs and `state.db` into the output directory;
use `--keep-home` to retain the profile. The analyser reads those copies read-only;
each prompt uses a fresh disposable git repo. Each JSON
row lists every matched run and task, with attempts, actual routes, controller
outcomes, verification exits, and an explicit per-prompt aggregate. A run that
fits two prompt windows is listed under `ambiguous_runs` in both rows and excluded
from both aggregates. An expected route with no unique affirmative `use_for`
match is shown as `unresolved`.
