# Security

Report vulnerabilities privately through this repository's GitHub **Security →
Report a vulnerability** (private vulnerability reporting):
[report privately](https://github.com/JanniezRubyjane/hermes-control/security/advisories/new).
Do not post secrets or exploit details in a public issue. Include the affected
version, a minimal reproduction, the expected boundary and what crossed it.

In scope: escapes from the task worktree or OS sandbox, bypasses of the worker
or verification safety guards, and secret leaks in commits, results, logs or the
dashboard. Read the [security model](README.md#security-model) before running the
plugin. The verify sandbox blocks writes outside the checkout and the network,
but permits reads; use only repositories you would run in your own shell.
Nothing is merged without your action.

The 0.1.x release line is the initial supported line. Please report issues on
its latest revision.

Claude workers use `--restricted` when their installed CLI lists it. With Claude
Code 2.1.295, committed `.claude` hooks did not run, `CLAUDE.md` loaded, and reads
outside the worktree were refused. Without the flag, committed `.claude` settings
or a `.claude` symlink cause refusal. Codex cannot skip project config: anything
tracked under `.codex/` or a `.codex` symlink in the base commit causes refusal.
Continuation is refused after changes to the engine's `.claude/` or `.codex/`.

Controller Git uses Hermes's repository-hardened environment to neutralise hooks,
fsmonitor and repository filter drivers. Commit messages, verify/red output,
`unproven_reason` and result text are redacted for known secret formats; this
cannot recognize every secret.
