# Changelog

## 0.2.0 — More engines, clearer review

For an existing clone, follow [Upgrade from 0.1.0](README.md#upgrade-from-010).

### Highlights

- **A third engine.** Gemini runs through the Antigravity `agy` CLI next to Claude
  Code and Codex, with the same worktrees, sandboxed checks and commit records.
- **Review a branch before you merge it.** The Runs view shows each task's committed
  diff with secrets masked, takes line comments and copies them as review notes for
  the planner.
- **See the code as diagrams.** The Diagrams view draws reviewed module, flow and
  sequence diagrams of a repository, with evidence marks on what was checked.
- **Hermes Desktop pages.** A desktop plugin adds Runs and Engines pages, live task
  progress and notifications to the desktop app.
- **More control over limits.** Fallback routes, model allow-lists and a pause
  switch per engine decide where work goes when an engine is busy or near its limit.
- **More safety checks.** Task changes are scanned for secrets before commit, the
  sandbox proves it blocks outside writes before each check, and changes to test or
  engine settings raise warnings.

### What changed since 0.1.0

| Area | 0.1.0 | 0.2.0 |
|---|---|---|
| Engines | Claude Code and Codex | Adds Gemini (`agy`); Codex can be turned off as a worker |
| Usage limits | Limit routing and manual Resume | Configurable fallback routes, model allow-lists, pause per engine |
| Reviewing results | Run inspection | Branch review with masked diffs, line comments and review notes |
| Understanding a repository | None | Diagrams view with reviewed diagrams |
| Hermes Desktop | None | Desktop plugin with Runs and Engines pages |
| Safety | Sandboxed verify, red-green proof, secret redaction | Adds secret scan before commit, sandbox write probe, warnings on check and engine setting changes, cross-origin refusal |
| Interrupted work | Manual Resume | Interrupted runs become resumable tasks; Resume will not start a second worker |
| Measuring | Health and stats for two engines | Gemini in health and stats; experimental benchmark harness |
| Install | Install script | New-install and upgrade guides with rollback |

### Details

Gemini joins Claude Code and Codex through the Antigravity `agy` CLI. Configurable
fallback routes preserve the worktree on limits, governed by model allow-lists.
Codex can be disabled as a worker while remaining available for sandbox verification.
Codex and Gemini can be [paused from the Engines tab](docs/limits-and-resume.md#pausing-an-engine)
to hold new work while running tasks continue. Antigravity quota readings show
their snapshot age in the dashboard and watch output; they never pause or reroute work.

The [Diagrams view](docs/diagrams-view.md) adds reviewed module, flow and sequence
diagrams with evidence marks, zoom, pan and overview drill-down. Set the author with
[`diagram.author: codex` or `gemini`](docs/diagrams-view.md), and diagrams accept
[`--lang`](skills/hermes-control/references/diagrams.md). The [Runs view](docs/runs-view.md) adds branch review:
masked committed diffs, line comments and copyable review notes let users inspect
changes and hand findings back to the planner. Its dashboard API refuses
cross-origin browser requests with HTTP 403. A [desktop plugin](docs/desktop-plugin.md) brings Runs and
Engines pages, live task progress, a proof-state chip and notifications into Hermes Desktop.

A secret scan now checks task changes before commit, including resumed work,
and blocks suspected secrets or scanner errors. Interrupted runs are reconciled
into resumable tasks, preserving recorded outcomes; Resume refuses to start
another worker while the earlier engine still runs or cannot be checked.
Sandbox verification first probes that writes outside the checkout are blocked.
[Changes to test-runner configuration and committed engine settings](skills/hermes-control/references/verify.md),
including `.codex/`, `.agents/` and `.gemini/`, raise warnings so passing checks
do not hide changes to how those checks run. Controller Git also pins task
worktrees to their controller-owned Git metadata.

[`control_dispatch`](README.md#use) selects the repository from the calling
session's working directory instead of the gateway's, and ignores invalid `timeout`
and `wait` values and uses the defaults. `control_status` reports a run held by another process as running
and honours `wait` for it.

The [benchmark harness](scripts/bench/README.md) compares direct CLI, controller and
full-planner outcomes with hidden sandbox checks, quota guards and replayable tasks.
The `health` and `stats` commands now account
for Gemini usage; health recognises review verdict lines and `lessons` recognises
agy commands, making operational reports more useful across engines.
Check setup also follows Hermes's managed runtime and provides a read-only check
toolchain for agy workers.

## 0.1.0 — First public release

Hermes-Control delegates coding tasks to Claude Code and Codex in individual Git
worktrees, records commits and sandboxed verification with optional red-green
proof, supports usage-limit routing and manual Resume, and provides an Engines
dashboard, run inspection and worktree pruning. The public repository begins
with one squashed export of the private development tree; development diaries
and local planner notes stay private. This release adds the MIT license,
support and security documentation, and a checked public-export workflow.

Workers use restricted Claude mode when available and refuse unsafe committed
engine config; continuation guards check changes to engine config. Controller
Git uses Hermes's repository-hardened environment, and commit/result boundaries
redact known secret formats. Claude model discovery uses aliases and Hermes's
built-in list without contacting an account. Usage comes from the official CLIs.
