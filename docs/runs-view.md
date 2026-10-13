# Runs view

The Engines tab has two views (a plugin gets one tab): **Engines** and **Runs**. The
last one chosen is remembered per browser; the dashboard rewrites the URL, so a
hash would not stick.

## What it draws

Runs lists the newest run logs first, with a dot per task (green done, amber
unproven or paused, red failed, blue running, grey waiting or stopped). Selecting
one draws:

- **A header**: run id and a state badge (finished green, running blue with a
  live dot, paused or stopped outlined), the repo (its full path on hover), when it started and
  how long it has run, a summary line (`3 tasks · 2 proven · 1 unproven`), totals
  across every attempt (tool calls, refused or failed calls, tokens out, cached
  tokens in, and cost when any attempt reports one), and the planner's model,
  provider, and `hermes chat --resume <id>` (its whole reasoning is that
  conversation) on one line.
- **A phase strip**, above the table: `planned → waiting (N s) → returned
  running / returned → checked ×k → finished`, the current phase highlighted.
  Built from `planner` trace entries (run-level, not tied to a task); hidden
  when none have arrived yet, which is always true for logs written before
  the trace route existed.
- **One row per task**, in plan order, collapsed by default: status dot, task id,
  policy, the last attempt's engine and model (`planned → actual` when routing
  moved it), how long it ran, its tool calls (refused or failed calls in red),
  and the outcome first — the verified result for a done task, the reason for an
  unproven, failed, or paused one, the live step for a running one, or what it is
  waiting on. A task with a branch gets a Copy button for the merge command.
  Clicking a row (not Copy) expands it: the planner's `why`, any reroute, limit,
  or pause notes, one summary line per attempt (engine, model, how it started,
  calls, errors, time, tokens, cost, its last message), a **live timeline**, the
  verify block (command, exit code, the check's last 8 lines), and the commit,
  branch, and worktree. A task auto-expands the first time it is seen running,
  so its timeline is visible without a click; collapsing it sticks until it
  starts running again.

  The timeline is a chat-like transcript of what the worker thought, said, and
  did, in order, growing as trace entries land: the codex reasoning summary
  (dim, 💭, collapsible past 6 lines), the text it writes between tool calls
  (💬), each tool call (⚙ name and input on one line, a spinner while it's
  still running, a "▸ output" toggle once it completes, red with ✗ when
  refused), commits, verify results, and limit/reroute/pause/error notes in
  rose. A filter bar (all · thinking+said · tools · errors) sits above it, with
  a "reasoning: concise" tag when the task's engine is codex and the setting is
  known. It auto-scrolls to the newest entry while the task runs, unless the
  user has scrolled up, in which case a "↓ newest" pill jumps back down. It is
  fed by `GET .../tasks/{task_id}/trace` (fetched once per task, on first
  expand) and by `trace` frames on the same stream the table already follows;
  `task_id: ""` frames (the planner's own entries) are shown in every task's
  timeline and drive the phase strip. When the trace route is missing (404, an
  older backend) or fails, the timeline falls back to the old per-attempt
  "show steps" list, with a muted note explaining why.

A running run is followed live: the page opens `GET /runs/{id}/stream`
(Server-Sent Events) and updates the row table as `view` frames arrive, falling
back to 3 s polling if the stream is unavailable (an older backend, or the
connection failed) or ends unexpectedly. The backend closes the stream once the
run settles, at which point the page reads the run once more and stops. While a
selected run is paused, the page polls it every 10 s; after Resume makes it
running, live streaming starts again. The run list also polls every 10 s, so new
runs appear on their own. On narrow screens a
row reads top to bottom.

## Where the data comes from

`hermes_control/runs.py` reads the run log (`~/.hermes/control/run-<id>.jsonl`).
Session events split a task into attempts. `rerouted`, `limit`, `paused` and
`resumed` events mark decisions as they happen; `summary` events, written when a
dispatch or a resume ends, settle tasks. A run whose tasks are all settled and at
least one is paused has state `paused`; a resumed event makes it `running` again
until it settles. A run with unsettled tasks and no event
for 35 minutes (longer than a task's timeout) is shown as stopped. Logs from
before these events existed are read as well: checks settle their tasks, and the
repo is found in the workers' worktree paths.

`GET /runs/{id}/tasks/{task_id}/trace?since=<seq>` returns the ordered events for
one task, including the planner's run-level phases, worker thinking and text,
and each tool call with its recorded output. A completed call retains its start
sequence; a `since` request includes it when the matching tool-end sequence is
newer. The response includes `entries` and `last_seq`. After each updated `view`
frame, the run stream also sends one `trace` frame per new entry, with `task_id`
and `entry`; a tool-end frame carries the completed call so a live timeline can
replace its pending entry. Planner frames use an empty `task_id`.

Routes: `GET /runs?limit=`, `GET /runs/{id}` (404 for an unknown or malformed id),
`GET /runs/{id}/stream` (Server-Sent Events; the page falls back to polling when
this route is missing or the connection fails), `GET /runs/{id}/tasks/{task_id}/trace`
(the timeline's entries; the page falls back to the old steps list when this route
404s or fails).

## codex's reasoning summary

The codex engine card on the Engines view shows a reasoning-summary switch
(none / concise / detailed / auto) when its row carries a `reasoning_summary`
field: a hint line explains it shapes what the worker's thinking looks like in
Runs, and applies to its next task. Changing it `PUT`s `GET/PUT
/reasoning-summary`. Both are absent on a backend that has not shipped the
field yet, in which case the card draws nothing.

`GET /runs/{id}/stream` sends Server-Sent Events. Each `view` frame contains the
full run view when the stream starts or the log grows; idle connections receive
`: keep-alive` comments, and a `gone` frame reports a removed log. The stream
ends after the view shows a settled run.

## In a terminal: `hermes control watch`

The plugin registers `hermes control` (`register_cli_command`); `watch` draws the
same run view as text (`hermes_control/watch.py`, with rich): the header (run,
state, repo, time), the planner and the command that opens its session, then
each task — its reason, a move with why, each attempt (a spinner and the last
three steps while it works; its last message after), and where it landed — and a
usage line per engine against its cap (`--no-usage` leaves that out).

It follows the newest run unless given a run id, redraws every second
(`--interval`) on the terminal's alternate screen, and exits on Ctrl-C with the
terminal restored. When stdout is not a terminal, or with `--once`, it prints one
frame and exits.

## `hermes control stats`

`hermes control stats [--limit N] [--json]` shows one row per task attempt across
the newest runs (30 by default). The table includes run and task, actual engine
with an arrow from the planned engine when routing moved, model, effort, tool
calls and errors, elapsed seconds, input and cached input tokens, output and
reasoning tokens, task status, and whether verification passed. Token counts
are shown in thousands; `--json` prints the underlying attempt records,
including planned engine and model, policy, continuation type, and cost.

This makes the cost and outcome of each delegation visible so planner-facing
text can be tuned from measurements.
