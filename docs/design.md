# Hermes-Control — Design

Historical design record. The provider path (D1) and the VS Code extension (D4) were not built; README describes what ships.

**Date:** 2026-09-23
**Status:** Approved for implementation planning
**Repo:** downstream of `NousResearch/hermes-agent`

## 1. Goal

Turn Hermes Agent into an **orchestrator** that plans work and delegates the
coding to `claude` and `codex`, launched against a chosen repo/path from
VSCode.

Hermes is the planner and reviewer. It is not the coder. It reads the repo,
decomposes the prompt into tasks, picks an engine per task, runs each worker in
an isolated git worktree, then aggregates diffs and results for human review.

Hermes-Control is the name for this subsystem: one Hermes plugin, one skill,
and one thin VSCode extension.

## 2. Decisions

These decisions guided the original implementation plan.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Hybrid**: engines serve as both delegated workers and model providers | Worker path for code changes; provider path for cheap one-shot work (summaries, titles, small reviews) reusing existing subscriptions |
| D2 | **Planner decides routing** (LLM), not config rules | Adapts to tasks that rules would not anticipate. Mitigated by D2a and D2b |
| D2a | Planner must emit a `why` for every engine choice, and it is logged | Makes a non-deterministic router auditable and tunable |
| D2b | `@claude` / `@codex` in the prompt pins the engine | Cheap escape hatch; avoids arguing with the planner |
| D3 | **Native adapters**, not community ACP adapters | The native CLIs expose orchestration primitives ACP does not: codex sandbox policy, claude `--bg` fleet, `--output-schema`, `--permission-prompt-tool`. No added npm dependency |
| D4 | **Thin VSCode extension**: launcher + task panel + diff/merge | Real visibility into parallel workers without building a full ACP client |
| D5 | Run events transported as **newline-delimited JSON file**, not JSON-RPC | No new protocol to design; survives Hermes running in a container with a shared volume, which is the recommended security posture |

## 3. Verified environment facts

Everything below was verified by running the tools on 2026-09-23. Flags not
listed here were **not** confirmed and must be re-checked before use.

### 3.1 `claude` (Claude Code)

Headless invocation and the flags this design depends on:

- `-p` / `--print` — non-interactive
- `--output-format <format>` — `stream-json` for streaming events
- `--input-format <format>` — stream-json input
- `--include-partial-messages` — partial chunks as they arrive
- `--session-id <uuid>`, `-r` / `--resume [value]`, `--fork-session`
- `--add-dir <directories...>` — additional tool-accessible directories
- `--append-system-prompt <prompt>`
- `--permission-mode <mode>` — choices: `acceptEdits`, `auto`, `bypassPermissions`,
  `manual`, `dontAsk`, `plan`. Hermes-Control emits only `plan` (read-only) and
  `acceptEdits` (write); `bypassPermissions` and `dontAsk` skip the prompt gate
  that *is* claude's containment, and `auto` hands the approval decision to a
  classifier (`claude auto-mode`: "Inspect or reset auto mode classifier"); all
  three are **never emitted**. `auto` is guarded only as a `--permission-mode`
  value, because it is also an ordinary value of `--autocompact` and of
  `codex exec --color`
- `--permission-prompt-tool` — **routes permission prompts to an MCP tool**
- `--allowedTools` / `--allowed-tools <tools...>`
- `--disallowedTools` / `--disallowed-tools <tools...>`
- `--model <model>`, `--mcp-config <configs...>`, `--strict-mcp-config`
- `--settings <file-or-json>`, `--agents <json>`, `--bare`
- `--bg` / `--background` — starts a background session and prints an id;
  managed with `claude attach|logs|stop|rm` and listed by `claude agents`
- `--allow-dangerously-skip-permissions` — **never emitted by Hermes-Control**
- `--dangerously-skip-permissions` — a **separate** flag from the one above;
  claude ships both. Also **never emitted**. (Corrected after the Phase 1 final
  review: this design listed only the longer name, and `policy.py`'s guard was
  written from this list, so the shorter one went unguarded.)

### 3.2 `codex` (`codex exec`)

- `codex exec [PROMPT]` — prompt as argument, or from stdin (`-`)
- subcommands: `resume`, `fork`, `review`
- `--json` — streaming JSON events
- `-o` / `--output-last-message <FILE>`
- `--output-schema <FILE>` — **structured final result**
- `-C` / `--cd <DIR>` — working directory
- `-s` / `--sandbox <read-only|workspace-write|danger-full-access>`
- `--approve-for-me` — automatic review using the workspace-write sandbox — **never emitted**
- `-m` / `--model`, `-c` / `--config <key=value>`, `-p` / `--profile`
- `--enable` / `--disable <FEATURE>`, `--strict-config`
- `--skip-git-repo-check`
- `--dangerously-bypass-approvals-and-sandbox` — **never emitted**
- `--dangerously-bypass-hook-trust` — **never emitted**

### 3.3 Different engine capabilities

The two engines have **different security models**. The design surfaces this
rather than pretending they are interchangeable:

- **claude workers are prompt-gated.** Containment comes from
  `--allowedTools` / `--disallowedTools` plus permission prompts, which
  `--permission-prompt-tool` can route into Hermes's own approval gate.
- **codex workers are sandbox-contained.** `-s workspace-write` is an
  OS-level sandbox — the only kind of boundary `SECURITY.md` §2.2 treats as
  load-bearing. There is no prompt bridge.

Neither is strictly stronger. A task needing a real boundary should prefer
codex; a task needing fine-grained human approval should prefer claude. The
skill in §4.7 teaches the planner exactly this.

### 3.4 Hermes internals this design reuses

- `tools/subagent_worktree.py` — `create_subagent_worktree(parent_cwd, subagent_id)`
  creates `<repo>/.worktrees/subagent-<id>` on branch `hermes-subagent/subagent-<id>`,
  keeps `.worktrees/` out of `git status`, and silently downgrades outside git.
  Also `local_backend_active()`, `resolve_repo_root()`, and
  `mark_worktree_payload_unproven()`.
- `tools/delegate_tool*.py` — the subagent contract to mirror: the parent sees
  only the delegation call and the summary result, never the child's
  intermediate tool calls or reasoning. Config keys worth matching:
  `delegation.worktree_isolation`, `delegation.max_concurrent_children`.
- `hermes_cli/plugins.py:456` — the plugin tool registration API:
  ```python
  ctx.register_tool(
      name: str, toolset: str, schema: dict, handler: Callable,
      check_fn: Callable | None = None, requires_env: list | None = None,
      is_async: bool = False, description: str = "", emoji: str = "",
      override: bool = False,
  )
  ```
  A plugin module exposes **`register(ctx)`** — that is the name
  `hermes_cli/plugins_loader.py` looks up (`getattr(module, "register", None)`),
  and a plugin without it is skipped with "no register() function". (Corrected
  after the Phase 1 final review: this section previously said `register_tools(ctx)`,
  generalising from `plugins/platforms/a2a/__init__.py`, where `register_tools` is
  a plugin-internal helper that its own `register()` calls. The plugin was built
  to this sentence and therefore never loaded.) `override=True` is **not
  needed** — `control_dispatch` is a new name — and would require operator
  opt-in via `plugins.entries.<id>.allow_tool_override`.
- `plugins/model-providers/copilot-acp/` — the provider-plugin pattern:
  a `ProviderProfile` subclass overriding `create_client()` and
  `fetch_models()`, registered via `register_provider`. Its own docstring
  notes an out-of-tree provider needs the same three lines "without touching
  core".
- `agent/copilot_acp_client.py` — precedent for an OpenAI-shaped shim that
  drives an external agent subprocess and bridges tool calls over text.
- `tools/environments/local.hermes_subprocess_env(inherit_credentials=...)` —
  the credential-scoped subprocess environment. Per `SECURITY.md` §2.3,
  provider keys are stripped by default.
- `tools/approval_floors.py` — `approvals.deny` globs block unconditionally,
  **before** the yolo / `approvals.mode: off` bypass.
- `agent/redact.py:redact_sensitive_text` — used before any event text is
  written to the run log.

### 3.5 Known gaps

- `claude` and `codex` do **not** speak ACP (unlike `copilot --acp`), so
  `delegate_tool`'s `override_acp_command` path does not apply. This is why D3
  chose native adapters.
- The repo has **no VSCode extension**. `apps/desktop/electron/vscode-marketplace.ts`
  and `src/themes/vscode.ts` are unrelated. `acp_adapter/` targets Zed.
- Neither CLI's JSON event schema is pinned by this design. Adapters must be
  written against captured fixtures and tolerate unknown event kinds.

## 4. Architecture

### 4.1 `ControlEvent` — the central contract

The load-bearing abstraction. Both CLIs emit different JSON; everything
downstream consumes one normalized shape.

```
ControlEvent:
  run_id:    str
  task_id:   str
  engine:    "claude" | "codex"
  seq:       int                  # monotonic per task
  ts:        float
  kind:      "text" | "thinking" | "tool_start" | "tool_end"
           | "permission_request" | "plan" | "usage" | "result" | "error"
  payload:   dict                 # kind-specific, documented per kind
  raw_kind:  str | None           # the engine's original event name, for debugging
```

`raw_kind` is deliberate: when an engine adds an event type, it shows up in
the log as an unmapped passthrough instead of being silently dropped.

The VSCode panel, the Hermes TUI, and the result aggregator all read only
`ControlEvent`. Adding a third engine later touches one adapter and nothing
else.

### 4.2 `EngineAdapter`

```python
class EngineAdapter(ABC):
    name: str                      # "claude" | "codex"

    def probe(self) -> EngineInfo | None:
        """Binary present, version, auth usable, models available.
        None when the engine cannot be used right now."""

    def build_argv(self, task: ControlTask, *, cwd: Path,
                   policy: ExecPolicy) -> list[str]: ...

    def parse_events(self, line: str) -> Iterator[ControlEvent]:
        """Normalize one line of engine stdout. Unknown kinds pass through
        with raw_kind set and kind='text'."""

    def extract_result(self, events: Sequence[ControlEvent],
                       artifacts: Artifacts) -> ControlResult: ...
```

`ExecPolicy` is the engine-neutral permission request (`read_only` or `write`,
plus optional tool allow/deny lists). Each adapter maps it to that engine's
own mechanism — this is where the §3.3 asymmetry is contained.

### 4.3 `ClaudeAdapter`

```
claude -p "<goal>" \
  --output-format stream-json --include-partial-messages \
  --session-id <uuid> \
  --add-dir <worktree> \
  --append-system-prompt "<worker contract>" \
  --permission-mode <mapped from policy> \
  --allowedTools <from policy> --disallowedTools <from policy> \
  --model <model>
```

- cwd is the worktree; `--add-dir` grants it explicitly
- result comes from the terminal `result` event in the stream
- `--permission-prompt-tool` is wired in a later phase (§9, step 8), pointing at a
  Hermes MCP tool so worker permission requests reach Hermes's approval gate
  and surface in the TUI and the VSCode panel

### 4.4 `CodexAdapter`

```
codex exec "<goal>" --json \
  -C <worktree> \
  -s <read-only|workspace-write> \
  --output-schema <schema.json> \
  -o <last-message.txt> \
  -m <model>
```

- `--output-schema` is the structured result; it maps directly to
  `ControlResult` with no text parsing
- `--skip-git-repo-check` stays **off**: the worktree is a valid repo, and the
  check is a useful guard
- `--approve-for-me` stays **off** by default; it is an operator config choice

### 4.5 `control_dispatch` tool — the worker path

Registered by the plugin into toolset `control`:

```
control_dispatch(
  tasks: [
    { id, goal, engine: "claude"|"codex"|"auto",
      policy: "read-only"|"write",
      files_hint: [...],          # optional, narrows the worker's search
      depends_on: [task_id, ...], # optional
      why: str,                   # required when engine != "auto" (D2a)
      verify: str }               # optional; the only way a task becomes proven
  ]
)
```

Concurrency is configured in `config.yaml` under
`plugins.entries.hermes_control.settings.parallel`:
`{max_tasks: 2, per_engine: {claude: 1, codex: 2}}`. A missing `per_engine`
count defaults to `max_tasks`.
Counts must be integers >= 1 (booleans are rejected), `max_tasks` is at most 8,
and unknown keys are errors naming the key. Invalid settings log a warning and
fall back to all ones. The plan event records the parallel settings in use and
any validation error. There is no tool argument or environment override.

With `max_tasks: 1`, dispatch uses the original plain sequential loop and event
order. Otherwise a bounded thread pool starts ready tasks in planned order as
total and engine slots become available. Every dependency must have settled
(done, failed or paused) before admission; failure skips and pause propagates
with the same reasons as sequential execution. A task is counted on its engine
at admission, after any cap move; a moved task waits for a destination slot.
A later limit handover retains that admission count. `per_engine` limits how
many tasks start on an engine; a task that hits a usage limit mid-run continues
on the other engine at once, so that engine can briefly exceed its `per_engine`
value. Entries and the final summary remain in planned order even when tasks
finish out of order; one task's
exception does not abandon siblings. Independence is expressed by `depends_on`;
`files_hint` remains a search hint, not an inferred dependency.

Lock points:

- A run's scheduler lock protects dependency/failure/pause state and slot
  accounting. An admission handshake keeps initial usage checks and cap moves
  in planned order; usage checks, cap moves, engines and landing run outside the lock.
- `RunLog.append_next` holds the log lock across sequence allocation and append;
  every dispatch append (including engine, landing, resume and planner events)
  uses it. Each batch reaches disk with one write under the lock.
- A module-level lock per resolved git common directory (falling back to the
  resolved repository path if git fails) serializes task worktree
  creation, controller commits and verify checkout setup/cleanup. Engines and
  verify commands run outside this lock and can overlap.
- The lazy sandbox probe is locked. `limits.engine_hold` uses immutable usage
  readings; its usage/account caches already have a lock, and Claude reports
  use unique temporary files and atomic replacement.

Per task:

1. `create_subagent_worktree(parent_cwd, subagent_id=f"control-{run_id}-{task_id}")`
   — the `run_id` is load-bearing. Task ids are short and stable by design, so a
   run-independent id makes the *second* dispatch against the same repo collide
   with the first run's branch, which `git worktree add -b` refuses. (Corrected
   after the Phase 1 final review, which found the collision reported as
   "not a git repo".)
2. Spawn via the adapter; stream `ControlEvent`s to the run log
3. Hold the admitted concurrency slot through execution, commit and verification
4. On completion: capture `git diff` in the worktree as an artifact
5. **Land** the task (`landing.py`). If the engine succeeded with a non-empty
   diff, commit the worktree onto the task branch — hardened git, a fixed
   `Hermes-Control` identity, no repository hooks. A failed or timed-out run is
   never committed; its partial work stays in the worktree for inspection.
   Then, if the task has a `verify` command, make a detached, clean checkout of
   the commit, run the check there (`verify.py`, §6), and remove the checkout.
   Ignored files and bytecode left in the task worktree cannot prove the commit.
   If checkout creation fails, keep the commit but mark it unproven.
   `unproven_reason` is null **only** when the check passed;
   otherwise it names why not: `not verified: no verify command was given`,
   `not verified: no sandbox available (...)`, `verification failed (exit N)`,
   `verification timed out after Ns`, `not committed: ...`, `empty diff`, or
   the engine's own failure.
6. Return a compact per-task summary — never the child's intermediate tool
   calls, matching `delegate_tool`'s contract. Per task: `task_id`, `engine`,
   `ok`, `text`, `diff_stat`, `unproven_reason`, `branch`, `commit`,
   `worktree`, `why`, `verify` (`command`, `passed`, `exit_code`,
   `output_tail`, or null)

**Why the controller verifies, not the worker (found by the first real run).**
Both workers reported success and the summary said `unproven_reason: none`, but
nothing had checked either change: `claude -p` in `acceptEdits` refuses any Bash
command that needs approval (`python3 -c ...` came back "This command requires
approval"), and codex's sandbox had no `pytest`. Asking the worker to test is not
a mechanism. The planner states the check; the controller runs it.

**The worker may run the same check.** When a task has a `verify` command, runs
on `claude`, and codex is installed (codex supplies the sandbox; a bare
`claude` install gets nothing new here), the controller writes a script to
`<hermes home>/control/checks/<run_id>-<task_id>.sh` before the worker starts —
outside the worktree, mode `0500`, so the worker can run it but not edit it —
and removes it once the worker exits. The script `exec`s exactly the
invocation the controller uses for its own check:
`codex sandbox -P :workspace -C <worktree> -- /bin/sh -c <verify>`, through
`/usr/bin/env -i` so the worker's environment — which carries its own provider
credentials — never reaches the check; only `HOME`, `PATH`, `TMPDIR`, `LANG`,
`LC_ALL`, `TERM`, `USER`, `LOGNAME`, `SHELL` and `CODEX_HOME` pass through. The
worker's claude argv gets `--allowedTools Bash(<script path>)`: that exact
command, nothing before or after it, is the only Bash call pre-approved, and
its goal gets a note that it can run the command to check its work and should
fix what it reports until it passes. The proof is unchanged: after the worker
finishes, Hermes-Control still commits the change and runs `verify` itself in
a clean checkout of the commit inside the sandbox, and only that run clears
`unproven_reason` — the worker's own
runs are for self-correction before the commit, so a `verify` command should
leave the worktree clean, or whatever it writes rides along in the diff.
For `codex` write tasks with `verify`, the worker goal also says it may run the
check inside its sandbox, use focused tests while editing, run the full check
when done, and read only needed line ranges.
Without codex installed, nothing changes for the worker and the task is
reported "not verified: no sandbox available" as before.

`depends_on` gives a simple topological order; a cycle is a validation error,
not a deadlock.

**`depends_on` orders and gates tasks.** A dependent starts only if each
dependency's engine succeeded and any requested verification passed. A failed
dependency skips its dependents as failed; a paused dependency pauses them.
Success without a verify command does not block a dependent. Every task
branches a fresh worktree from `HEAD`, so task B never sees task A's edits —
not its files, not its diff, not its result text. A planner that reads "B needs
A's result" as data flow will emit pairs that cannot work, and B will return a
plausible diff built on the wrong base: failure that looks like success. Passing
a predecessor's output to a successor is a later phase; until then the only
safe use of `depends_on` is sequencing tasks whose *side effects outside the
worktree* must not interleave, and the skill says exactly that.

### 4.6 Provider path

Two provider plugins following the `copilot-acp` pattern exactly:

- `plugins/model-providers/claude-cli/`
- `plugins/model-providers/codex-cli/`

Each supplies a `ProviderProfile` whose `create_client()` returns an
OpenAI-shaped shim over `claude -p --output-format json` / `codex exec --json`,
modeled on `agent/copilot_acp_client.py`. This makes both engines selectable
with `hermes model` for single-turn, no-file-write work.

### 4.7 Router — the skill

Because routing is planner-decided (D2), the router is a **prompt artifact,
not code**: `skills/hermes-control/SKILL.md`, following
the bundled skill format (YAML frontmatter with `name`, `description`,
`version`, `prerequisites.commands: [claude, codex]`, `metadata.hermes.tags`).

It teaches the planner:

- each engine's strengths, cost, and **security model** (§3.3)
- when the provider path suffices (single turn, no file writes) versus the
  worker path
- how to decompose a prompt into independently-completable tasks and declare
  `depends_on`
- that every engine choice carries a `why` (D2a)
- that `@claude` / `@codex` in the user's prompt overrides its judgment (D2b)

### 4.8 Run log

`~/.hermes/control/run-<run_id>.jsonl` — one `ControlEvent` per line, append
only. Text passes through `redact_sensitive_text` before it is written.
After a task's engine events the controller appends its own `commit` and
`verify` events, continuing that task's `seq`.

This is the single source of truth for the VSCode panel and for post-hoc
inspection of what an engine did.

### 4.9 VSCode extension

`apps/vscode-control/`. Three responsibilities, nothing more:

| Piece | Behavior |
|---|---|
| Command `Hermes-Control: Run in this folder` | Opens an integrated terminal with `cwd` = workspace root and starts `hermes` |
| Tree view `HERMES-CONTROL` | Watches `~/.hermes/control/run-*.jsonl`; renders run → tasks → live status from `ControlEvent`s |
| Task actions | `Open diff` shows the task's worktree diff in VSCode's native diff editor; `Merge` runs `git merge hermes-subagent/subagent-control-<run_id>-<task_id>` |

The extension holds no orchestration logic. It is a viewer plus two git
commands, so a bug in it cannot corrupt a run.

**Shipped first: the Engines tab in `hermes dashboard`** (`engines-dashboard.md`).
Before any VSCode panel, the plugin contributes a dashboard tab that shows whether
each engine can take work (the same `engine_state()` dispatch routes on), signs
codex in through its device code, gives claude's terminal command, and shows the
planner's provider. Its `/api/hermes_control/engines` API is what a VSCode
panel would call too.

**Closed gap, found by the Phase 1 end-to-end smoke run:** `Merge` used to do
nothing. The worker contract tells the engine not to commit, so the task branch
stayed at the base commit and `git merge <branch>` merged an empty branch. The
controller now commits a successful task's worktree onto its branch (§4.5 step
5), so `Merge` is a plain `git merge` of a branch that carries the change.

## 5. Data flow

```
VSCode: "Hermes-Control: Run in this folder"     cwd = /path/to/repo
  └─► hermes (TUI in the integrated terminal)
        │  "fix auth to support OIDC + add tests"
        ▼
      planner  (Hermes LLM, guided by the hermes-control skill)
        │  reads repo, decomposes, picks engine per task + why
        ▼
      plan event ───────────► ~/.hermes/control/run-42.jsonl ──► VSCode panel
        │
        ├─ task-1  worker   claude   .worktrees/subagent-control-42-task-1
        ├─ task-2  worker   codex    -s workspace-write
        └─ task-3  provider claude   (no worktree, one-shot)
        │
        │  each worker streams ControlEvents → run log → panel
        │  claude permission_request → Hermes approval gate → prompt
        ▼
      aggregate: per-task diff + result + unproven flag
        ▼
      Hermes reviews the diffs and reports; the human clicks
      Open diff / Merge in VSCode
```

## 6. Security posture

This design is deliberately **safer than Hermes's default**, which runs
LLM-emitted shell directly on the host (Hermes Agent's `SECURITY.md` §2.2, confirmed: the
default terminal backend is `local`).

- Workers write into **worktrees**, never the main tree. Bad output is
  reviewable before it can reach the working branch.
- Task ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, keeping ids inside their
  intended paths and Claude tool rules. Goals must start with a word so an
  engine cannot parse one as a CLI flag.
- codex workers get an **OS-level sandbox** per task (`-s workspace-write`).
- claude workers get tool allow/deny lists, and later a permission bridge into
  Hermes's approval gate.
- Before Claude continues or resumes in a used worktree, the controller refuses
  to start it if `.claude/` differs from the task's base commit, including
  staged, untracked, ignored, and case variants such as `.CLAUDE/`. It also
  refuses symlinks at `.claude` or anywhere inside it. A committed `.claude/`
  change, matched without regard to case, remains reviewable on the task branch
  and carries a warning in the result before merge. After every engine run,
  Hermes terminates its process group so background children cannot edit settings
  before a handover. A child that creates its own session with `setsid` escapes
  that group cleanup.
- `approvals.deny` globs in `config.yaml` are the recommended hard floor: they
  fire **before** any yolo bypass.
- The plan **never** emits `--allow-dangerously-skip-permissions`,
  `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`,
  `--dangerously-bypass-hook-trust`, `--permission-mode bypassPermissions`,
  `--permission-mode dontAsk`, `--permission-mode auto`, codex's
  `--approve-for-me`, or `-s danger-full-access`. If an operator wants
  them, they are config-only and off by default.
- Engine subprocesses are spawned through
  `hermes_subprocess_env(inherit_credentials=True)`. The workers are
  user-blessed CLIs that legitimately need their own provider credentials
  (Tier 2), so what this buys is **Tier-1 stripping**: GitHub tokens,
  gateway/bot tokens and Hermes-internal secrets never reach a worker. The
  original wording ("provider credentials are stripped unless explicitly
  declared") described `inherit_credentials=False`, which would sign the
  workers out; it was also unimplemented until the Phase 1 final review.
- Run logs are redacted before write. They still contain verbatim task goals
  and diffs, so they inherit the same privacy caveat as `hermes debug share`.
- A task's `verify` command executes code an LLM just wrote, so it runs only
  inside `codex sandbox -P :workspace -C <clean commit checkout> -- /bin/sh -c <verify>`:
  writes confined to that disposable checkout and `$TMPDIR`, no network (checked on
  codex-cli 0.155.1: `$HOME` writes and outbound connections both fail), and
  `hermes_subprocess_env(inherit_credentials=False)` — no provider keys, since
  the check needs none. A probe (`printf` assembling a marker) first proves the
  sandbox really executes commands, so an old codex's usage error is not read as
  a failing check. **No sandbox, no run:** without codex the command is never
  executed bare, and the task stays unproven. `-P :danger-full-access`, a real
  built-in that lets the command write anywhere, is in the forbidden-flag guard.
- A `claude` worker's pre-approved script runs only that one sandboxed
  command — `--allowedTools Bash(<script path>)` names nothing else — and
  `/usr/bin/env -i` scrubs its environment before `exec`, because the
  worker's own process carries provider credentials the check has no
  business seeing.

Merging is always a human action. Nothing in this design auto-merges.

## 7. Error handling

| Condition | Behavior |
|---|---|
| Engine binary missing or not signed in | Checked once per run before any worktree exists (`probe()` + the CLI's own status command: `claude auth status`, `codex login status`). Its tasks move to the engine that can run (`routing.py`), recorded as `rerouted` in the summary and the plan event; a per-engine `model` is dropped. With no usable engine nothing starts and the call names each engine's reason. The verify sandbox needs only the codex binary, not a login |
| Worker timeout | SIGTERM then SIGKILL. Worktree and branch are **preserved**; result marked unproven |
| Worker exits 0 with an empty diff | Marked unproven (mirrors Hermes's existing check) |
| Worker succeeds but no `verify` was given | Committed; unproven: `not verified: no verify command was given` |
| `verify` fails, times out, or no sandbox exists | Committed; unproven with the reason; the command's output tail is in the summary and run log |
| Unknown event kind from an engine | Passed through with `raw_kind` set; never fatal |
| Merge conflict | Surfaced in the panel. Never auto-resolved |
| Partial failure in a parallel batch | Other tasks continue; the run reports mixed status |
| `depends_on` cycle | Validation error before any worker starts |
| Not inside a git repo | Worktree isolation downgrades silently (existing behavior); the run is marked as having no isolation |

## 8. Testing

- **Adapter event parsing** — golden-file tests over captured real JSON
  streams, one fixture set per engine. This is the highest-value test surface
  because the engines' schemas are external and unpinned.
- **`probe()`** — integration test against the real binaries, skipped when
  absent.
- **`control_dispatch` orchestration** — a fake adapter drives assertions on
  concurrency capping, worktree lifecycle, `depends_on` ordering, cycle
  rejection, and unproven flagging. No real CLI involved.
- **Policy mapping** — table test asserting each `ExecPolicy` maps to the
  right flags per engine, and that no dangerous-bypass flag is ever emitted.
- **VSCode extension** — parse a fixture `.jsonl` and assert the resulting
  tree shape.

## 9. Build order

MVP first; everything in §2 stays in scope, this is sequencing only.

1. `ControlEvent`, `EngineAdapter`, `ClaudeAdapter`, `CodexAdapter`, golden tests
2. `control_dispatch`, **sequential mode only**, one worktree per task
3. JSONL run log with redaction
4. The `hermes-control` skill (makes the planner actually use it)
5. Opt-in parallel scheduling with total/per-engine caps (implemented, §4.5)
6. The two provider plugins
7. VSCode extension
8. `--permission-prompt-tool` bridge into Hermes's approval gate

Step 8 is last because it is the most complex piece and a sequential MVP does
not need it. Steps 1–4 are the smallest thing that demonstrates the concept
end to end.

## 10. Open questions

- **Tool name.** `control_dispatch` is explicit and collision-free, but
  Hermes's house style is short verbs (`terminal`, `patch`, `delegate`). Worth
  revisiting before it becomes load-bearing.
- **Worktree namespace.** Reusing `create_subagent_worktree()` yields
  `.worktrees/subagent-control-<run_id>-<task_id>` on
  `hermes-subagent/subagent-control-<run_id>-<task_id>` —
  free correctness, slightly awkward naming. A dedicated `hermes-control/<id>`
  namespace would read better but duplicates working code.
- **Provider-path tool bridging.** `copilot_acp_client.py` bridges tool calls
  over text, which is lossy. The provider path may be better restricted to
  genuinely tool-free calls.
- **claude `--bg` fleet.** Native background sessions with
  `claude attach|logs|stop|rm` could replace part of the orchestration loop.
  Not used in the MVP; evaluate after step 5.

## 11. Out of scope

- A full ACP client extension for VSCode (D4 chose the thin panel)
- Auto-merge or auto-conflict-resolution
- Engines beyond `claude` and `codex` (the adapter seam makes them additive)
- Upstreaming to `NousResearch/hermes-agent`
