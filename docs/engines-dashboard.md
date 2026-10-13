# Hermes-Control — Engines dashboard tab

The Engines page builds on [the dispatch design](design.md).

## 1. Goal

One page where the user sees whether each Hermes-Control engine can take work
and gets it signed in without hunting for commands. The planner card shows its
configured provider and model without checking sign-in.

## 2. Decisions (agreed with the user)

| # | Decision | Why |
|---|---|---|
| E1 | A tab in `hermes dashboard`, contributed by the `hermes_control` plugin | Uses the existing plugin-tab mechanism (`kanban`, `hermes-achievements`); no core files touched, which the plugin rules require |
| E2 | Rows: `claude` CLI, `codex` CLI, and a read-only **planner** row | The planner's own login already has a dashboard flow (`/models`); duplicating it is what "extend, don't duplicate" forbids |
| E3 | codex signs in **in the page** (device code); claude signs in **in a terminal** (copyable command) | See §3.3 — claude is kept `external` for the same policy reason the core dashboard gives |

Rejected: adding rows to the core Accounts catalog (a plugin editing
`hermes_cli/web_server_oauth.py`), and a CLI-only `hermes control status`
(not the UI that was asked for).

## 3. Verified facts this design depends on

### 3.1 Dashboard plugin mechanism

- Discovery scans `<plugins root>/*/dashboard/manifest.json`
  (`hermes_cli/web_server_dashboard.py::_discover_dashboard_plugins`). The
  manifest `name` is the plugin's identity for enable/disable and the API prefix.
- A manifest `api` file must expose `router` (FastAPI `APIRouter`); it is imported
  by path as `hermes_dashboard_plugin_<name>` and mounted at
  `/api/plugins/<name>/` (`_mount_plugin_api_routes`). Bundled plugins are
  trusted but respect an explicit disable (`_plugin_api_mount_skip_reason`).
- `/api/plugins/*` is behind the dashboard's session auth; the bundle calls it
  through `SDK.fetchJSON`, which handles both the loopback token and gated mode.
- The bundle is a hand-written IIFE (`dist/index.js`) using
  `window.__HERMES_PLUGIN_SDK__` (`React`, `hooks`, `components`: `Card`,
  `CardHeader`, `CardTitle`, `CardContent`, `Badge`, `Button`, `Separator`,
  `Toast`, …) and registers with
  `window.__HERMES_PLUGINS__.register(name, Component)`.

### 3.2 Engine status

`hermes_control/engines/registry.py::engine_problem(adapter)` already
returns None or a reason for "not installed" and "not signed in" (the latter via
`claude auth status` / `codex login status`, exit 0 = signed in). The page
reuses it; it adds no second definition of "usable".

### 3.3 Sign-in flows

- **codex** — `codex login --device-auth` (codex-cli 0.155.1) prints a fixed link
  `https://auth.openai.com/codex/device` and a one-time code (`XXXX-XXXXX`,
  "expires in 15 minutes"), then waits; on approval it writes codex's own
  `auth.json` and exits 0. The official client does the OAuth; the page only
  relays a public link and a code the user approves on OpenAI's site. The core
  dashboard already runs a device-code flow for the `openai-codex` provider.
- **claude** — `hermes_cli/web_server_oauth.py` keeps every Anthropic entry at
  `flow: "external"`: *"an in-dashboard Connect button would let a scriptable
  HTTP endpoint mint Claude Pro/Max subscription tokens outside Anthropic's own
  client, against its OAuth usage policies."* The page follows that stance:
  status plus the command `claude auth login`, run by the user in a terminal.
- `codex sandbox` (the verify step) needs the codex **binary** only, not a login
  (`hermes_control/verify.py::find_sandbox`).

### 3.4 Planner configuration (read only)

The Engines page shows the planner's provider and model from config. It does not
check planner sign-in or resolve runtime credentials. Use Hermes's Models page
for the planner's sign-in flow.

### 3.5 Model lists

Codex uses its official `codex debug models` catalog. Claude combines CLI aliases
with Hermes's built-in model list; no account is contacted for that list.
**Refresh list** reloads the catalogs; otherwise they are cached for ten minutes.

## 4. Architecture

```
hermes_control/
├── device_login.py          # codex device-code session: spawn, parse, poll, cancel (no FastAPI)
└── dashboard/
    ├── manifest.json        # name "hermes_control", tab "/engines", api "plugin_api.py"
    ├── plugin_api.py        # thin FastAPI router over registry + device_login
    └── dist/index.js        # the Engines page (IIFE on the plugin SDK)
```

`device_login.py` holds all process handling so it is testable without a web
server; `plugin_api.py` only translates it to HTTP. Process groups and tree
kills reuse `hermes_control/proctree.py`; the child environment is
`engines.base.engine_env()`, the same one the engines run with.

### 4.1 Manifest

```json
{
  "name": "hermes_control",
  "label": "Engines",
  "description": "Sign-in status of the Hermes-Control engines (claude, codex) and the planner",
  "icon": "Cpu",
  "version": "0.1.0",
  "tab": { "path": "/engines", "position": "after:models" },
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

`icon` must be a key of the host's `ICON_MAP` (`web/src/App.tsx`); an unknown name
silently falls back to `Puzzle`. `position` is `after:<path segment>`
(`web/src/plugins/types.ts`), so the tab sits right after Models.

### 4.2 Backend API (prefix `/api/plugins/hermes_control`)

Sync `def` routes (FastAPI runs them in its threadpool; every call shells out).

| Method + path | Returns |
|---|---|
| `GET /engines` | `{engines: [EngineRow, ...], sandbox: {available, path}}` |
| `POST /engines/codex/login` | `LoginSession` once the link and code are read; `409` if codex is already signed in or not installed |
| `GET /engines/codex/login/{session_id}` | `LoginSession` (current state) ; `404` for an unknown id |
| `DELETE /engines/codex/login/{session_id}` | `LoginSession` with `state: "cancelled"` |
| `GET /planner` | `{signed_in: null, provider, model, source, detail}` from config; no sign-in check or credential resolution |

```
EngineRow    = {name, installed, version, path, signed_in, problem, sign_in_command,
                sign_in_in_page}          # sign_in_in_page: true for codex, false for claude
LoginSession = {session_id, state, url, code, expires_at, detail}
state        ∈ pending | signed_in | failed | expired | cancelled
```

`problem` is exactly `engine_problem()`'s text, so the page and the dispatch
summary never disagree. `sign_in_command` is `claude auth login` / `codex login`.

The codex card's reasoning-summary switch is backed by `GET /reasoning-summary`,
`PUT /reasoning-summary`, and the `reasoning_summary` field in its `GET /engines` row.
`none` produces no summary, `concise` produces a short one, `detailed` produces
a fuller one, and `auto` lets codex choose. The saved setting applies when the
next codex task starts; a running task keeps the mode it started with.

### 4.3 codex login session (`device_login.py`)

1. **Start** — refuse if codex is missing or already signed in. Cancel any
   session still pending (one at a time: two device codes for one CLI only
   confuse). Spawn `[codex_path, "login", "--device-auth"]` with
   `stdin=DEVNULL`, stdout+stderr merged, `engine_env()`, own process group.
2. A reader thread strips ANSI, keeps a bounded output tail, and extracts the
   first `https://` URL and the first `[A-Z0-9]{4}-[A-Z0-9]{4,6}` code. Start
   waits up to 30 s for both; if the process exits or the wait lapses first, the
   session is `failed` with the output tail as `detail`.
3. **Poll** — process alive and younger than 15 min → `pending`. Exit 0 →
   re-check `auth_problem`: None → `signed_in`, otherwise `failed` ("codex exited
   but still reports signed out"). Non-zero exit → `failed` with the tail.
   Older than 15 min → kill the tree, `expired`.
4. **Cancel** — kill the tree, `cancelled`.

Sessions live in a module-level dict under a lock, keyed by a random id
(`secrets.token_urlsafe`); at most 8 are kept, oldest settled first, and at most
one is ever `pending`. Nothing is persisted: a dashboard restart drops pending
logins, and the orphaned device code simply expires.

### 4.4 Page (`dist/index.js`)

- On mount: `GET /engines`, `GET /planner`.
- **Engine card** per row: name, version, a green "Signed in" / amber "Not signed
  in" / grey "Not installed" badge, and `problem` in plain text when present.
  - claude, signed out → the `claude auth login` command with a Copy button, and
    **Check again** (re-fetches `/engines`).
  - codex, signed out → **Sign in** → shows the link (opens a new tab) and the
    code with Copy, polls every 3 s until the state leaves `pending`, then
    re-fetches `/engines` and shows a toast. **Cancel** while pending.
  - codex row also shows "Verify sandbox: available / unavailable".
- **Usage and Refresh**: claude percentages come only from the official CLI's
  last `rate_limit_event` during a task, shown with the report's age. Account
  email and subscription come from `claude auth status`. Refresh re-reads that
  status and the saved report without a Claude usage network call or stored
  credential read; it cannot obtain newer percentages until a task reports them.
  No valid report means unknown usage, which never holds work back. Codex keeps
  reading through its official `codex app-server`. Caps and mid-task limit
  switching follow `docs/limits-and-resume.md`.
- **Planner card**: configured provider + model, without checking its sign-in;
  a link to `/models` for the planner's sign-in flow.
- **Model-list editor**: codex's own catalog; Claude aliases plus Hermes's built-in
  list. No account is contacted for the Claude model list.
- Every fetch failure renders as an inline message on its card; the rest of the
  page keeps working.

## 5. Security

- No credential passes through Hermes: codex writes its own `auth.json`; the API
  returns only the public device link and the one-time code, both of which the
  user sees in their own browser anyway.
- Claude stays terminal-only (§3.3).
- All routes sit behind the dashboard session auth (§3.1); nothing new is exposed
  without it. A gated remote dashboard signs in the codex on the **server**
  machine — which is the machine the engines run on, so that is the intent.
- The login child gets `engine_env()` (Tier-1 secrets stripped), like the engines.
- Rate of process spawns is bounded: one pending codex login at a time.

## 6. Error handling

| Condition | Behaviour |
|---|---|
| codex not installed | Row "Not installed"; no Sign in button; `POST` → 409 |
| codex already signed in | No Sign in button; `POST` → 409 |
| `--device-auth` prints no code within 30 s / exits early | `failed`, output tail in `detail` |
| User never approves | `expired` after 15 min, process tree killed |
| Dashboard restarted mid-login | Session gone → poll `404` → page shows "login was interrupted, start again" |
| Status check hangs (old CLI) | `engine_problem` already times out (20 s) and reports it |

## 7. Testing

- **`tests/test_device_login.py`** — against fake `codex`
  scripts (not the real CLI): link + code parsed; a fake that exits 0 after a
  trigger file appears → `signed_in`; non-zero exit → `failed` with tail; no code
  printed → `failed`; expiry → `expired` and the process gone; cancel kills the
  tree; starting a second login cancels the first.
- **`tests/test_dashboard_api.py`** — the router on a bare
  `FastAPI()` with `TestClient` (the `test_kanban_dashboard_plugin.py` pattern):
  `/engines` rows mirror `engine_problem()` for signed-in / signed-out / missing
  fakes; `claude` is never `sign_in_in_page`; `POST` 409 cases; poll/cancel
  round-trip; unknown session 404.
- **Manifest contract** — the real discovery function finds the tab and the real
  mount function mounts the router under `/api/hermes_control/`.
- **Integration (real CLIs, marked)** — `--device-auth` against the real codex
  with an empty `CODEX_HOME` yields a link and a code; cancel leaves no process.
- **E2E in a browser** — `hermes dashboard`, open the Engines tab in Chrome, check
  the three cards against `claude auth status` / `codex login status`; run a real
  codex sign-in into an empty `CODEX_HOME` (the user approves the code), watch the
  row turn green. Tests never read `.js` source (repo rule).

## 8. Out of scope

- One-click claude sign-in (§3.3), sign-out buttons, account switching.
- A VSCode panel (design.md D4) — it can call the same `/engines` API later.
- i18n of the page strings.
