# Hermes-Control desktop plugin

Hermes-Control includes an optional plugin for the Hermes desktop app. It adds a
status-bar chip and separate **Engines** and **Runs** sidebar pages, using the same
backend as the browser dashboard. It also adds **Hermes Control: Open Engines**
and **Hermes Control: Open Runs** commands to the command palette, registers a
**Hermes Control** theme, and changes the app wordmark to **HERMES CONTROL** while
enabled. The theme is available to select; enabling the plugin does not select it.

Hermes-Control is independent and is not affiliated with or endorsed by Nous
Research, Anthropic, OpenAI or Google.

## Requirements

The desktop plugin needs a Hermes desktop build with desktop plugin support.
It was tested on a Hermes desktop development build from upstream main, commit
4dc56fe (2026-10-23). The agent plugin works on Hermes Agent v0.22.5 (2026.9.24).

## Install and enable

1. Follow the [requirements and installation instructions](../README.md#install-and-quick-start).
   From the checkout, run `scripts/install.sh` and `scripts/verify-setup.sh`.
   The installer links the agent package, including `desktop/plugin.js`, into
   the Hermes plugins directory and enables the agent plugin.
2. Open Hermes desktop and go to **Capabilities → Plugins**. The app discovers
   the installed package and copies its desktop component into its app-level
   desktop plugins directory. Use **Rescan** if it has not appeared.
3. Enable **Hermes Control** on that page. The desktop component declares
   `defaultEnabled: false`, so installing the agent plugin alone does not enable
   the desktop UI. The agent plugin must also be enabled for the active profile;
   check with `hermes plugins list` and enable it with
   `hermes plugins enable hermes_control` if needed.
4. Open **Engines** or **Runs** in the sidebar, use the command palette, or click
   the status-bar chip for links to both pages.

The desktop component is app-level, while backend requests follow the active
profile. That profile needs the agent plugin and its dashboard API available.
After updating, use **Capabilities → Plugins → Rescan** or restart the app to
refresh the desktop copy. **Reload desktop plugins** in the command palette
reloads the desktop plugins. A checkout linked into the plugins directory also
supports automatic reloads when its source changes.

## Status-bar chip

The chip on the right reports running work, blocked work and reset time when
known, unproven results, usage near a cap, or idle state. A failed status request
shows **Hermes Control · offline**. Clicking the chip opens a popover with up to
two usage windows per engine, cap markers, the latest run's proof summary, and
**Open Engines** and **Open Runs** buttons.

Status is requested on activation and every 15 seconds; the chip's running timer
updates locally every second. The plugin requests an OS notification when it
observes the same latest run change from running to another state. Notification
delivery depends on the OS; a run that finishes between observations may not
produce one.

## Engines page

**Engines** shows CLI installation and sign-in state, versions, the planner's
provider and model, and whether the verification sandbox is available. Engine
rows show available usage windows and reset times, caps, usage sources and
whether an engine is taking work, paused, blocked or holding work back. Report-only
readings are marked as such and do not hold work back.

**Refresh** reloads engine and planner information and requests a usage refresh.
Readings still depend on what each CLI can report: refreshing Claude usage reads
the saved report and cannot obtain a newer percentage until the CLI reports one
during a task. See [Usage limits and Resume](limits-and-resume.md).

The desktop page displays this information without editing it. **Edit in
dashboard** opens the browser dashboard for its controls; see the
[Engines dashboard guide](engines-dashboard.md).

## Runs page and live activity

**Runs** lists the 30 newest runs, with state, a shortened run ID, relative update
time and repository name. Task rows show the task ID, result, engine and model,
and a diff statistic when recorded for a write task. Finished tasks keep their
recorded worker duration. Running task rows hide that duration because worker
time alone does not include the ongoing check.

Result labels distinguish tests passed from red-green proof, missing checks,
unproven work and failures. Available failure reasons and failed or blocked check
output are shown, limited to the last eight output lines. Successful read-only
tasks are labelled **review only**.

While a run and one of its tasks are running, a live block appears above the run
list for that task. It shows a status dot beside the task ID, engine and model,
and elapsed time since the latest attempt started, including time spent checking.
This is the current attempt's timer, not a total across retries or the whole run.
The phase strip shows **write → check → done**, or **review → done** for a
read-only task. It highlights the phase inferred from the recorded worker result.
The block shows up to three recent tool, text or error steps, shortened to fit;
error steps are highlighted. It disappears when the task or run stops running.

The page polls every 10 seconds, with an additional 3-second poll while its list
contains a running run and no load is in flight. **Refresh** reloads immediately.
Running run details are fetched again; settled details are cached until the
run's state or update time changes. A detail error is shown on the run and hides
its live block until a successful request.

For the browser dashboard's fuller timeline and inspection controls, see
[Runs view](runs-view.md). The desktop page uses polling rather than that view's
event stream and does not provide its trace, diff, review or Resume controls.

## Dashboard API

All data requests use the desktop SDK's `ctx.rest`, which resolves paths under
`/api/plugins/hermes_control` for the active profile and handles authentication.
The plugin does not ask you to paste a dashboard token.

| Request under that prefix | Used for |
| --- | --- |
| `GET /status` | Status chip, compact engine readings and latest-run proof summary |
| `GET /limits` | Popover and Engines usage windows and caps |
| `GET /limits?refresh=true` | Engines page's usage refresh |
| `GET /engines` | CLI installation, sign-in and verification sandbox state |
| `GET /planner` | Planner provider and model |
| `GET /runs?limit=30` | Recent run list |
| `GET /runs/RUN_ID` | Tasks, latest attempts, live steps and check results |

## Limits and troubleshooting

- The desktop SDK and dashboard API must be supported by your Hermes build.
  Hermes-Control uses Hermes internal APIs, which may change after an update.
  The project's tested platform and versions are listed in the
  [README](../README.md#requirements); Linux is untested.
- If pages are missing, check the desktop toggle and rescan the package. If the
  chip is offline or a page shows request errors, check the active profile's
  agent plugin and dashboard connection. UI visibility does not enable the backend.
- **Edit in dashboard** and **Open in dashboard** both open
  `http://127.0.0.1:9119/engines`. This address is fixed in the desktop plugin;
  the buttons do not follow a remote backend or a custom dashboard URL. Start
  the local browser dashboard with `hermes dashboard` when using that address.
- This UI issues GET requests and does not dispatch, merge, edit settings,
  sign in engines or resume tasks. Reading run details can reconcile interrupted
  runs in the backend, marking unsettled work paused; it does not resume it.
- Timers and steps reflect recorded attempts and polling, so they may lag actual
  work. Usage readings may be unavailable or stale. The live block is a compact
  activity view, not the complete log or proof that a task succeeded.
- The chip's status response omits account details. Engine and usage responses
  contain more data; the desktop views select the fields they display. Step text
  shortens common home-directory prefixes, which is not a general secret filter.
  Review logs and output before sharing them.
