# Usage limits, engine switching and Resume

Extends `docs/design.md` (dispatch) and `docs/engines-dashboard.md` (Engines tab).

## 1. Goal

A task must not be lost to a subscription limit. Before a task starts, an engine
over the user's cap hands the task to the other engine. When a worker hits a real
limit mid-task, the other engine finishes it in the same worktree. When neither
engine can take it, the task is paused and the user resumes it from the dashboard
once a limit resets. Nothing already written in a worktree is ever thrown away.

## 2. Decisions

- Resume is a button on the Engines tab. Nothing resumes on its own.
- One cap per engine: the most of any usage window Hermes-Control may use before
  sending that engine's tasks elsewhere. Default 80%. 100 means "only a real limit".
- Engines switch; models within an engine do not. Anthropic's windows are shared by
  every model, so another claude model would hit the same wall.
- Usage that cannot be read never holds work back. Mid-task detection still applies.
- A missing window percentage is unknown and does not count against the cap; the
  terminal and dashboard show it as unknown rather than 0%.

### Claude usage source

Hermes-Control uses only the last
`rate_limit_event` the official claude CLI emitted during a task, marked with
its age. Account email and subscription come from `claude auth status`.
With no valid report, usage is unknown and never holds work back. Refresh
re-reads that status and the saved report without a network call by
Hermes-Control. It neither reads Claude Code's stored credentials nor calls
a subscription usage endpoint.

### Antigravity (gemini) usage source

This usage source is unofficial and local-only. When the `/engines` dashboard or
watch line asks for Gemini usage, `hermes_control/agy_quota.py` finds the running
Antigravity language server through the process list (`ps`), reads its local CSRF
token from that process's arguments in memory, and discovers its listening port
with `lsof`. It calls only `https://127.0.0.1` at that port; proxies and redirects
are disabled. The token stays in memory for the request and is never stored on
disk or logged. Discovery and request errors omit process arguments and headers.

The reading is report-only and is never used to route or pause work. Dispatch
never reads this quota. There is no separate setting to turn off the reader; in
normal operation it runs only when the dashboard or watch line asks for Gemini
usage. The optional benchmark can also explicitly request readings for its own
quota guard, separately from task dispatch.

The groups have independent quotas. The source shows the estimated snapshot age
(or that it is unknown); the app may keep an old snapshot until refreshed. Buckets
past their reset read as unknown; unused five-hour buckets show no reset time.
This private app interface may break when agy updates, in which case usage reads
as unknown.

## 3. Verified facts (2026-09-24)

- **Report units.** The run report's `utilization` is a fraction (0-1), converted
  to a percentage for the dashboard and cap checks.
- **claude usage in a run.** `claude -p --output-format stream-json` emits
  `{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed" |
  "allowed_warning" | "rejected", "resetsAt": <epoch>, "unifiedWindows":
  {"five_hour": {"utilization": <fraction>, "resetsAt": ...}, "seven_day": {...}}}}`.
  The adapter already logs it as a `usage` event.
- **codex usage.** `codex app-server` (stdio JSON-RPC, codex-cli 0.155.1):
  `initialize`, then `account/rateLimits/read` returns `rateLimits: {primary,
  secondary: {usedPercent, windowDurationMins, resetsAt}, rateLimitReachedType,
  planType}` and `ordinaryUsageAllowed`, for codex's own login, in about a second.
  Hermes's `openai-codex` login can be a different account from codex's own,
  so Hermes's reading is not used.
  The app-server runs in its own process group, and Hermes stops the whole group after each reading.
- **Reset times.** A Codex provider block ends at the latest reset among full
  windows. If no window is full, the earliest known reset is used. Cap holds
  include only windows at or over the cap; reset text includes the server's
  time zone offset.
- **Claude report age and account.** A report made during a run records the
  claude CLI account. A report from a different account, or one past its latest
  reset, reads as unknown. Without a reset time it expires after five hours.
  Older report files without an account remain readable until they expire;
  reports without an account expire after at most five hours even when a
  weekly reset is later.
- **codex when blocked** prints "You've hit your usage limit. ... Try again at
  ..." (error codes `usage_limit_reached`, `usage_limit_exceeded`,
  `rate_limit_exceeded`, `quota_exceeded`).
- **Resuming a session.** `claude -p ... --resume <session-id>` keeps every other
  flag. `codex exec resume` takes neither `-s/--sandbox` nor `-C/--cd`, so a
  resumed codex session could run outside the sandbox; codex therefore never
  resumes a session. It starts a new one in the same worktree, where the earlier
  edits already are.

## 4. Behaviour

1. **Before each task.** An engine can take the task when it is signed in (checked
   once per run, as before) and its usage is under the cap and not blocked
   (checked before every task; readings are cached 60 s). The planner's engine
   runs it when it can; otherwise the other engine does, and `rerouted` says why
   (for example, that the engine is over its configured cap, with its reset time).
   When neither can, the task is paused without a worktree.
2. **A limit mid-task.** The engine's output shows a limit (claude:
   `rate_limit_event` with `status: rejected`, or an error result that says so;
   codex: an error or failed turn with a usage-limit message). The event is
   logged. If the other engine can take work, it continues **in the same
   worktree** with the goal plus a note that an earlier worker stopped and its
   edits are in place; `rerouted` carries `continued: true`. Otherwise the task
   is paused with its worktree, engine and session id.
3. **Dependents.** A task whose `depends_on` names a paused task is paused too,
   not started. A task whose dependency failed or did not pass its requested
   verification is skipped as failed. If a resumed task fails or does not pass
   verification, every still-paused dependent in that run, including transitive
   dependents, is skipped as failed and its pause record is removed.
4. **Pause record.** `<hermes home>/control/paused/<run>-<task>.json`: the task,
   the repo, the worktree (or none), engine, session id, reason, and when the
   blocking window resets. The saved task keeps its original goal and the model
   and effort from the engine that last ran it. Paused tasks are not committed;
   their summary entry says `paused` and how to resume.
5. **Resume** (dashboard button). Refused while a task it depends on is still
   paused, or while no engine can take work (the answer says when that changes).
   The paused engine is preferred: claude resumes its session with `--resume`;
   codex starts a new session in the worktree. Otherwise the other engine
   continues in the worktree. The resumed engine's model policy is applied before
   it starts, including its default when the saved task has no model or carries
   an invalid model from an older pause record. A task paused before it started
   gets its worktree now. The run appends to the original run log and lands like
   any task (commit, then the sandboxed check). Another limit updates the record;
   any other outcome removes it. **Discard** removes the record and leaves the
   worktree. Resume is also refused while the earlier engine still runs: the
   task's latest `spawned` event gives the engine's pid and start time, and
   while that process group runs the answer is "the earlier engine (pid 12345)
   is still running in its worktree; stop it or wait, then resume". When this
   cannot be checked: "could not check whether the earlier engine (pid 12345) is
   still running; resume once it has stopped". Hermes-Control never stops the
   engine itself (it only sends signal 0); you stop it or wait. A pid now used
   by another process (a different start time) counts as gone. On Windows the
   check always answers "could not check", so Resume is refused for every task
   that has a `spawned` event. Windows is untested.
6. **Interrupted run.** The process that runs a dispatch can end before the run
   finishes: an idle exit, a restart, a kill, a crash or a power loss. The OS
   frees the run's live lock however the process dies. When a run is read
   (`control_status`, the Runs view, the Runs list) and its log has no summary
   event, the live lock is free, no resume lock is held, and nothing but planner
   phases has been logged for 60 seconds, the run is reconciled from its log
   (`hermes_control/recover.py`), however long ago it died. Each unsettled task
   gets a pause record with the reason "the Hermes process running this run
   ended before the task finished" and shows as paused, resumable from the
   dashboard like a limit pause. A task that had not started is paused without
   a worktree (Resume creates it); dependents of a paused task are paused as
   usual. What the log already proves is kept: a task whose check passed stays
   passed; a finished read-only task or a failed write task is settled, not
   paused; a dependent of a task settled as failed is skipped as failed. A task
   whose branch already moved past its base (the process died while landing) is
   reported committed and unproven, never paused, with the reason "the Hermes
   process running this run ended before the check finished", or "not verified:
   no verify command was given" when the task had no verify command. Every
   engine start logs a `spawned` event with the engine's pid and start time; the
   pause record carries them too. A Hermes gateway started with `--idle-exit`
   can exit during a long run once the CLI is closed; the run then becomes
   paused. Keep the CLI open during a long run, or resume afterwards. On
   Windows the reconcile needs flock, so it does nothing there.

## 5. Settings

`plugins.entries.hermes_control.settings.limits.<engine>.max_percent`: an integer
1-100, default 80. Written through Hermes's plugin-settings writer, like `models`.

### Pausing an engine

Use **Pause** / **Resume** on the Engines tab's codex or gemini card to take an
engine out of rotation immediately. The setting is
`plugins.entries.hermes_control.settings.engines.<name>.paused`, a boolean that
defaults to `false`. Claude cannot be paused. The setting is read live before
dispatch, fallback handover and resume, without restarting Hermes. A running
task continues; new work follows `routing.fallback`, just as for a usage cap.
Paused engines are held before reading usage, so codex's app-server is not
started for them. A non-boolean pause value or a non-mapping engine entry holds
only that engine, before reading usage, with a `pause setting is invalid` reason.
Claude and other engines keep working, and the enabled engine list stays unchanged
by a malformed pause value. The dashboard shows `pause_problem`; use the toggle
to save a valid boolean and repair the pause value. Limits, paused-task listings
and resume checks remain available.

A pause keeps the login, enabled engine list and planner tool schema unchanged;
the planner still lists the engine until a new Hermes session. If every allowed
destination is paused, the task is saved with no reset time and becomes resumable
when one destination is unpaused. Resume the task from the dashboard.

## 6. Dashboard API (prefix `/api/plugins/hermes_control`)

| Route | Returns |
|---|---|
| `GET /limits?refresh=` | per engine `{cap, windows: [{label, used_percent, resets_at}], blocked, source, error, report_only, held_back, paused, pause_problem}` |
| `PUT /limits` | body `{limits: {<engine>: {max_percent}}}`; 400 on a bad value |
| `PUT /engines/{name}/paused` | body `{"paused": true\|false}`; returns `{engine, paused}`; 400 for claude, unknown/disabled engines or non-booleans; 403 for a managed setting |
| `GET /paused` | paused tasks plus the state of resumes started from this dashboard |
| `POST /paused/{id}/resume` | 202 and runs in the background; 409 with the reason when it cannot start |
| `DELETE /paused/{id}` | removes the record |

For claude, Refresh re-reads `claude auth status` and the last saved report;
it cannot fetch a newer usage percentage until the CLI reports one during a task.
Codex continues to use its official `codex app-server`.

## 7. Security

Claude usage reads only the official CLI's stream reports and `claude auth status`;
Hermes-Control does not read stored Claude Code credentials or make a direct
Claude usage network request. The codex reading comes from codex itself.
Resumed and continued runs use the same
adapters and flags as any task, through the forbidden-flag guard.

## 8. Testing

Fake engines that print a limit event exercise the switch, the pause, dependents
and resume end to end in a real git repo. The codex and claude usage readers are
tested against recorded responses, plus integration tests against the real CLIs.
