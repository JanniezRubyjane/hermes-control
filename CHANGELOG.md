# Changelog

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
