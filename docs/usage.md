# Usage

[Back to the README](../README.md). Commands run from your repository unless stated otherwise.

The planner uses the default writer in your model settings; **claude reviews**.
Your model allow-list and engine pins choose the route. Usage limits can switch
engines in the same worktree, and paused tasks continue with **Resume**.

## Use details

```bash
cd /path/to/some/git/repo
hermes
```

Then describe the work. Pin an engine with `@claude`, `@codex`, or `@gemini`. The planner splits
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

## Repository diagrams

Ask the planner for a repository's design picture or work flow, or run:

```bash
hermes control diagram --repo PATH --ask TEXT [--kind KIND]... [--codex-model M] [--claude-model M] [--json]
```

`--kind` is repeatable, one value per flag: `--kind module --kind flow --kind sequence`.
Without `--kind`, module and flow views are drawn.

Codex writes the spec, claude reviews it, codex gets one fix round and claude
reviews again, then claude fixes any remaining problems. The command prints
the title, summary, review counts and a link opening **Diagrams** in the
dashboard's Engines tab. `--json` prints the record. Details, review marks and
cost estimates: [Diagrams view](diagrams-view.md).

Set the dashboard link base in `config.yaml`; the default is
`http://127.0.0.1:9119`:

```yaml
plugins:
  entries:
    hermes_control:
      settings:
        dashboard_url: http://127.0.0.1:9119
```

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
  task to the next usable engine in the user's `routing.fallback` list for its
  engine, or, when that engine has no list, to the other enabled engines in
  default order.
- A worker that hits a limit part-way is followed by the next usable engine in
  that order **in the same worktree**, so nothing it wrote is lost.
- When no engine in its fallback order is usable (including an explicit empty
  list), the task stays on its engine and is **paused**. It appears under
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
If the Hermes process ends mid-run, the run's unsettled tasks become paused.
Resume is refused while the earlier engine still runs; stop it or wait. A gateway
started with `--idle-exit` can exit during a long run, so keep the CLI open or
resume afterwards. See [limits-and-resume.md](limits-and-resume.md).
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

## Offline checks

Run `scripts/verify-setup.sh` outside the sandbox before using
`/bin/sh scripts/sandbox-check.sh`. Setup installs pytest and PyYAML into the Hermes
home's `control/verify`. A setup made before PyYAML was added must rerun
`scripts/verify-setup.sh`; `hc-update` does this by itself when the overlay has no
`yaml`. Unless `HERMES_PYTHON` is set, setup follows
`hermes --print-runtime-command` to find Hermes's bundled Python and loads
`hermes_bootstrap` to discover the repo and managed dependency import path.
It records the interpreter in `control/verify/python-path` and the import path,
one entry per line, in `control/verify/hermes-path`. Setup builds a symlink farm
at `control/verify/hermes-src` from the editable finder's `MAPPING` names and
root Python modules, excluding unmapped `tests` and `scripts` modules. Checks
replace the repo root with this farm so Hermes's own `tests/` and `scripts/`
packages cannot shadow this repo's packages. If the farm cannot be built, setup
and `scripts/check.sh` stop with exit 2: the recorded repo root cannot stand in for
the farm, because Hermes's own `tests` package would shadow this repo's tests. Setup
then removes `python-path`, `hermes-path` and the farm. It removes the previous agy
copy when `HERMES_HOME` is the default home `~/.hermes` (including symlinked or
trailing-slash spellings) or the copy's stamp records the same Hermes Python that
setup is using, so agy workers report "not set up" until setup succeeds. Otherwise,
setup prints that the copy was left in place, naming the other Hermes Python or
`unknown` when the stamp cannot be read. The Python check compares the stamp
with the Python setup uses as exact path strings: another spelling of the same
Python (such as a symlink) keeps the copy, so set `HERMES_PYTHON` to an
absolute path (the launcher's Python is always absolute). Because one copy is
shared by every home, when two homes use the same Hermes Python, a failed farm
under one home also removes the copy the other home still uses; run setup again
under a home whose farm builds to restore it.
With `--no-agy-copy`, setup leaves the older copy in place and warns
that it was built for the previous Hermes. Older launchers fall back
to the launcher or the install directory reported by `hermes --version`; an
empty import path removes `hermes-path` and the farm.

For an explicit managed runtime, set `HERMES_PYTHON` to the bundled interpreter
and `HERMES_PYTHON_PATH` to the Hermes repo root first, then the managed env
site-packages, colon-separated. That Python imports `hermes_cli` only after
bootstrap supplies the import path. Older Hermes needs only `HERMES_PYTHON`
and leaves `HERMES_PYTHON_PATH` empty.

Sandbox checks use the recorded Python and import path with user site-packages
disabled. `scripts/sandbox-check.sh` uses `$HERMES_HOME` when it holds a
`control/verify` directory, including after a failed setup; otherwise it uses
`~/.hermes`. Setup also builds a read-only copy under
`~/.config/hermes-control/check` for Gemini workers (or
`$XDG_CONFIG_HOME/hermes-control/check`). This is one copy per user, shared by every
Hermes home; a successful setup under any `HERMES_HOME` rebuilds it. Re-run setup
after every Hermes update: the copied interpreter, dependencies and Hermes source
are a snapshot.
`scripts/verify-setup.sh --no-agy-copy` installs only primary verify and reports
any older Gemini copy left in place.

The check prefers primary verify whenever it is readable. The Gemini adapter
sets `HERMES_CONTROL_CHECK_COPY=1` only in its worker environment. The copy requires
that exact opt-in, `CODEX_SANDBOX` unset and primary verify unavailable. It leaves
three public-file Git checks and the real Hermes CLI smoke test to post-commit
verify, except tests explicitly selected by file or node ID. Post-commit verify
does not receive the worker's marker and runs in the Codex sandbox, where the
copy is forbidden even if the marker is present. Without a usable primary or
permitted copy the original not-set-up error and exit 2 remain. Both paths accept
pytest arguments, for example `/bin/sh scripts/sandbox-check.sh tests/test_limits.py`.

The copy contains `python/` (the relocatable base CPython), `site/` (pytest,
PyYAML and selected third-party packages with distribution metadata), `src/` (Hermes editable
packages listed in the editable finder's `MAPPING` and every root `*.py` module
in their project directory, identified by `pyproject.toml`, even when the finder
predates those modules). In managed mode, sources come from the recorded Hermes
repo root instead of stale paths in the editable finder. A name missing both
under that root and at its recorded MAPPING location is skipped; a name missing
under the root but still present at the recorded location fails the build.
Single-file modules in managed mode skip the extension filter, preserving
interpreter-specific import suffixes. The copy also contains
`python-path` and a JSON `stamp` recording the
source interpreter, base Python version, Hermes Git HEAD when readable and UTC
build time. Setup builds a staging directory, excludes `.env*`, `*.env`, `.git`,
`tests`, `node_modules` and `__pycache__` below each copy root, and restricts Hermes
package files to `.py`, `.pyi`, `.so`, `.dylib`, `.json`, `.yaml`, `.yml`, `.toml`,
`.txt`, `.md` and `.typed`. It scans with both the copy's original secret rules
and the plugin's rules, smoke-tests imports (including `hermes_state` and
`hermes_state_common`) with the staged interpreter and
removes write permissions before replacing the previous copy. A failed copy rebuild
preserves the previous copy; a failed promotion restores it. A failed Hermes source
farm removes the previous copy when `HERMES_HOME` is the default home `~/.hermes`
(including symlinked or trailing-slash spellings) or the copy was built by the same
Hermes Python, so agy workers also get "not set up". Otherwise, setup prints that
the copy was left in place. `--no-agy-copy` always leaves it in place with a warning.
Secret findings
report only file paths and rule names. No downloads occur during a worker's check.

After a dependency change, refresh `scripts/agy-check-packages.txt` against the
imports of the full suite on primary verify:

```sh
/bin/sh scripts/sandbox-check.sh -p scripts.agy_check_imports
```

The plugin prints third-party import roots from `sys.modules` at session end.
Review its output against the list: keep the suite's third-party roots, omitting
pytest, PyYAML (`yaml`) and their dependencies already supplied by the verify site. Include imports
observed on other supported platforms too. Setup resolves each listed import from
Hermes's site-packages, including namespace packages and extension modules, and
fails with the names of missing packages. Update Hermes's environment or correct
the list, then re-run setup; listing a dependency never installs it into Hermes.

## Evaluate routing

`HERMES_PYTHON` is the interpreter Hermes runs on; `scripts/eval/analyze.py`
imports `yaml`, which `scripts/verify-setup.sh` installs into
`$HERMES_HOME/control/verify/site` (add it to `PYTHONPATH` when `HERMES_PYTHON`
lacks PyYAML).
Run `scripts/verify-setup.sh` once so the write probes can fill their `{verify}`
placeholder with the sandbox-ready pytest command. Preview the six quota-consuming
probes with `"$HERMES_PYTHON" scripts/eval/run.py --dry-run`.
Run them with `"$HERMES_PYTHON" scripts/eval/run.py --live --out /tmp/routing-before`, then
`"$HERMES_PYTHON" scripts/eval/analyze.py /tmp/routing-before` for a table and
`analysis.json`. Repeat with another output directory after changing planner-facing
text and compare the JSON files. The runner uses a temporary Hermes profile under
the source home's `profiles/` directory. It copies `config.yaml` and `.env` into
the throwaway profile, which is removed at the end unless `--keep-home` is set,
and symlinks the control plugin and skill. It copies `config.yaml`, run logs and `state.db`
(the planner's conversation history) into the `--out` directory, and never copies
`auth.json`. The analyser reads those copies read-only;
each prompt uses a fresh disposable git repo. Each JSON
row lists every matched run and task, with attempts, actual routes, controller
outcomes, verification exits, and an explicit per-prompt aggregate. A run that
fits two prompt windows is listed under `ambiguous_runs` in both rows and excluded
from both aggregates. An expected route with no unique affirmative `use_for`
match is shown as `unresolved`.
