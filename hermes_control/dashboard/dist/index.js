(function () {
  "use strict";
  // Hermes-Control "Engines" tab: can claude and codex take work, and how to sign
  // them in. Design: docs/hermes-control/engines-dashboard.md. claude signs in from
  // a terminal on purpose (spec section 3.3); codex signs in here, through codex's
  // own device-code flow.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef, useToast } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Toast } = SDK.components;

  const API = "/api/plugins/hermes_control";
  const POLL_MS = 3000;
  const USAGE_POLL_MS = 5 * 60 * 1000;

  function api(path, init) {
    return SDK.fetchJSON(API + path, init);
  }

  // fetchJSON throws Error("<status>: <body>"); FastAPI bodies are {"detail": "..."}.
  function reason(err) {
    const text = String((err && err.message) || err);
    const start = text.indexOf("{");
    if (start >= 0) {
      try {
        const body = JSON.parse(text.slice(start));
        if (body && body.detail) return String(body.detail);
      } catch (_) { /* not JSON: show the text as it is */ }
    }
    return text;
  }

  function copy(text, showToast) {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      showToast("Copy is unavailable here; select the text instead", "error");
      return;
    }
    navigator.clipboard.writeText(text).then(
      () => showToast("Copied: " + text, "success"),
      () => showToast("Copy failed; select the text instead", "error"));
  }

  function StatusBadge({ row }) {
    if (!row.installed) return h(Badge, { tone: "outline" }, "Not installed");
    if (row.signed_in) return h(Badge, { tone: "success" }, "Signed in");
    return h(Badge, { tone: "warning" }, "Not signed in");
  }

  function CommandLine({ command, showToast }) {
    return h("div", { className: "hc-command" },
      h("code", null, command),
      h(Button, { size: "sm", outlined: true, onClick: () => copy(command, showToast) }, "Copy"));
  }

  function CodexSignIn({ onSettled, showToast }) {
    const [session, setSession] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const timer = useRef(null);

    const stopPolling = () => {
      if (timer.current) { clearInterval(timer.current); timer.current = null; }
    };
    useEffect(() => stopPolling, []);

    const poll = useCallback((id) => {
      stopPolling();
      timer.current = setInterval(() => {
        api("/engines/codex/login/" + encodeURIComponent(id)).then((next) => {
          setSession(next);
          if (next.state === "pending") return;
          stopPolling();
          if (next.state === "signed_in") showToast("codex is signed in", "success");
          onSettled();
        }, (err) => { stopPolling(); setSession(null); setError(reason(err)); });
      }, POLL_MS);
    }, [onSettled, showToast]);

    const start = () => {
      setBusy(true);
      setError("");
      api("/engines/codex/login", { method: "POST" }).then((next) => {
        setSession(next);
        if (next.state === "pending") poll(next.session_id);
        else setError(next.detail || "codex did not start a sign-in");
      }, (err) => setError(reason(err))).finally(() => setBusy(false));
    };

    const cancel = () => {
      stopPolling();
      api("/engines/codex/login/" + encodeURIComponent(session.session_id), { method: "DELETE" })
        .then(setSession, (err) => setError(reason(err)))
        .finally(onSettled);
    };

    if (session && session.state === "pending") {
      return h("div", { className: "hc-device" },
        h("p", null, "1. Open ",
          h("a", { href: session.url, target: "_blank", rel: "noopener noreferrer" }, session.url)),
        h("p", null, "2. Enter this one-time code:"),
        h("div", { className: "hc-command" },
          h("code", { className: "hc-code" }, session.code),
          h(Button, { size: "sm", outlined: true, onClick: () => copy(session.code, showToast) }, "Copy")),
        h("p", { className: "hc-muted" },
          "Waiting for approval. This card updates by itself; the code expires in 15 minutes."),
        h(Button, { size: "sm", ghost: true, onClick: cancel }, "Cancel"));
    }
    const lastAttempt = session && session.state !== "signed_in"
      ? "Last attempt: " + session.state + (session.detail ? " (" + session.detail + ")" : "")
      : "";
    return h("div", { className: "hc-actions" },
      h(Button, { size: "sm", onClick: start, disabled: busy }, busy ? "Starting..." : "Sign in"),
      lastAttempt ? h("p", { className: "hc-muted" }, lastAttempt) : null,
      error ? h("p", { className: "hc-error" }, error) : null);
  }

  // Where the model suggestions came from, or why there are none, and a way to fetch
  // them again. A missing list never blocks editing: the name field takes any text.
  function CatalogLine({ engine, catalog, onLoadCatalog }) {
    const entry = catalog && catalog.data && catalog.data[engine];
    let text = "";
    let warn = false;
    if (!catalog || catalog.loading) {
      text = "Fetching the models your account can use...";
    } else if (catalog.error || !entry || entry.error) {
      text = "Could not fetch the model list (" + (catalog.error || (entry && entry.error) || "no list") +
        "). Type model names instead.";
      warn = true;
    } else {
      text = entry.models.length + " models from " + entry.source + ".";
    }
    return h("div", { className: "hc-catalog" },
      h("span", { className: warn ? "hc-warn" : "hc-muted" }, text),
      h(Button, { size: "sm", ghost: true, onClick: () => onLoadCatalog(true),
                  disabled: !catalog || Boolean(catalog.loading) }, "Refresh list"));
  }

  // One engine's allowed models: which a task may use, and at what effort. An empty
  // list allows any model (the CLI's own default when a task names none).
  function ModelSettings({ engine, block, efforts, onSave, catalog, onLoadCatalog }) {
    const [draft, setDraft] = useState(null); // null: showing, not editing
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const allowed = (block && block.allowed) || [];
    const defaultName = (block && block.default) || "";
    const entry = catalog && catalog.data && catalog.data[engine];
    const listed = entry && !entry.error ? entry.models : [];
    const known = new Map(listed.map((m) => [m.name, m]));

    // The levels this model takes, when its catalog says; never one the engine lacks,
    // since saving checks against the engine's list.
    const effortsFor = (name) => {
      const match = known.get(name.trim());
      return match && match.efforts ? efforts.filter((level) => match.efforts.includes(level)) : efforts;
    };

    const startEdit = () => {
      setError("");
      if (!catalog || catalog.error) onLoadCatalog(false);
      setDraft({ default: defaultName,
                 allowed: allowed.map((m) => ({ name: m.name, use_for: m.use_for || "", effort: m.effort || "",
                                                autoUse: false })) });
    };
    const setRow = (index, patch) => setDraft((d) => ({
      ...d, allowed: d.allowed.map((m, i) => (i === index ? { ...m, ...patch } : m)) }));
    const rename = (index, name) => setDraft((d) => ({
      ...d,
      default: d.default === d.allowed[index].name ? name : d.default,
      allowed: d.allowed.map((m, i) => {
        if (i !== index) return m;
        const next = { ...m, name, typing: true };
        // An empty "use for" takes the catalog's description, and follows the name
        // until the user writes their own.
        if (!m.use_for || m.autoUse) {
          const match = known.get(name.trim());
          next.use_for = (match && match.description) || "";
          next.autoUse = Boolean(next.use_for);
        }
        return next;
      }) }));
    const removeRow = (index) => setDraft((d) => ({ ...d, allowed: d.allowed.filter((_, i) => i !== index) }));
    const addRow = () => setDraft((d) => ({
      ...d, allowed: d.allowed.concat([{ name: "", use_for: "", effort: "", autoUse: false }]) }));

    const save = () => {
      const rows = draft.allowed
        .map((m) => ({ name: m.name.trim(), use_for: m.use_for.trim(), effort: m.effort || null }))
        .filter((m) => m.name);
      const next = { default: rows.some((m) => m.name === draft.default) ? draft.default : (rows[0] ? rows[0].name : null),
                     allowed: rows };
      setSaving(true);
      setError("");
      onSave(engine, next).then(() => setDraft(null), (err) => setError(reason(err))).finally(() => setSaving(false));
    };

    if (!draft) {
      return h("div", { className: "hc-models" },
        h("div", { className: "hc-models-head" },
          h("span", { className: "hc-label" }, "Models"),
          h(Button, { size: "sm", ghost: true, onClick: startEdit }, "Edit")),
        allowed.length
          ? h("ul", { className: "hc-model-list" }, allowed.map((m) => h("li", { key: m.name },
              h("code", null, m.name),
              m.name === defaultName ? h(Badge, { tone: "secondary" }, "default") : null,
              m.effort ? h("span", { className: "hc-muted" }, " effort " + m.effort) : null,
              m.use_for ? h("div", { className: "hc-muted" }, m.use_for) : null)))
          : h("p", { className: "hc-muted" }, "Any model; a task that names none uses the CLI's own default."));
    }
    const listId = "hc-models-" + engine;
    return h("div", { className: "hc-models" },
      h("div", { className: "hc-models-head" }, h("span", { className: "hc-label" }, "Models")),
      h(CatalogLine, { engine, catalog, onLoadCatalog }),
      h("datalist", { id: listId }, listed.map((m) => h("option", {
        key: m.name, value: m.name, label: [m.label, m.description].filter(Boolean).join(" - ") }))),
      draft.allowed.map((m, i) => {
        const levels = effortsFor(m.name);
        // Judged once the name is finished, not while it is half typed.
        const unlisted = !m.typing && listed.length > 0 && m.name.trim() !== "" && !known.has(m.name.trim());
        return h("div", { key: i, className: "hc-model-item" },
          h("div", { className: "hc-model-row" },
            h("label", { className: "hc-radio", title: "Used when a task names no model" },
              h("input", { type: "radio", name: engine + "-default", checked: m.name !== "" && draft.default === m.name,
                           onChange: () => setDraft((d) => ({ ...d, default: m.name })) }),
              "default"),
            h("input", { className: "hc-input", list: listId, placeholder: "model, e.g. sonnet", value: m.name,
                         onChange: (e) => rename(i, e.target.value), onBlur: () => setRow(i, { typing: false }) }),
            h("input", { className: "hc-input hc-wide", placeholder: "use for, e.g. routine edits", value: m.use_for,
                         onChange: (e) => setRow(i, { use_for: e.target.value, autoUse: false }) }),
            h("select", { className: "hc-input", value: m.effort, onChange: (e) => setRow(i, { effort: e.target.value }) },
              h("option", { value: "" }, "effort: CLI default"),
              levels.map((level) => h("option", { key: level, value: level }, level)),
              m.effort && !levels.includes(m.effort)
                ? h("option", { value: m.effort }, m.effort + " (not listed for this model)")
                : null),
            h(Button, { size: "sm", ghost: true, onClick: () => removeRow(i) }, "Remove")),
          unlisted
            ? h("p", { className: "hc-warn" },
                "Not in " + engine + "'s list for your account. It can still be saved, if you know the CLI takes it.")
            : null);
      }),
      h("div", { className: "hc-actions-row" },
        h(Button, { size: "sm", ghost: true, onClick: addRow }, "Add model"),
        h(Button, { size: "sm", onClick: save, disabled: saving }, saving ? "Saving..." : "Save"),
        h(Button, { size: "sm", ghost: true, onClick: () => setDraft(null), disabled: saving }, "Cancel")),
      h("p", { className: "hc-muted" },
        "An empty list allows any model. Tasks are checked against this at once; the planner sees the new list in its next Hermes session."),
      error ? h("p", { className: "hc-error" }, error) : null);
  }

  function when(epoch) {
    if (!epoch) return "";
    return new Date(epoch * 1000).toLocaleString([], { month: "short", day: "numeric",
                                                        hour: "2-digit", minute: "2-digit" });
  }

  // Pure usage helpers
  function usageBarView(usage, window) {
    const known = window.used_percent !== null && window.used_percent !== undefined;
    const used = known ? Math.max(0, Math.min(100, window.used_percent)) : null;
    const cap = usage.cap;
    return {
      used,
      tone: usage.report_only ? "" : known && used >= cap ? "hc-bar-over" : known && used >= cap * 0.8 ? "hc-bar-near" : "",
      showCap: !usage.report_only,
      title: usage.report_only ? "Report only" : "cap " + cap + "%",
    };
  }
  // End pure usage helpers.

  // One engine's subscription usage, its cap, and whether it is holding work back
  // (docs/limits-and-resume.md). A reading that failed says so and holds nothing back.
  function UsageBlock({ engine, usage, onSaveCap, onRefresh, refreshing }) {
    const [draft, setDraft] = useState(null); // the cap being edited, as typed
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    if (!usage) return null;
    const cap = usage.cap;
    const save = () => {
      const value = Number(draft);
      if (!Number.isInteger(value) || value < 1 || value > 100) {
        setError("The cap is a whole number from 1 to 100.");
        return;
      }
      setSaving(true);
      setError("");
      onSaveCap(engine, value).then(() => setDraft(null), (err) => setError(reason(err)))
        .finally(() => setSaving(false));
    };
    return h("div", { className: "hc-models" },
      h("div", { className: "hc-models-head" },
        h("span", { className: "hc-label" }, "Usage"),
        h("span", { className: "hc-usage-tools" },
          usage.plan ? h(Badge, { tone: "outline" }, usage.plan + " plan") : null,
          usage.paused ? null : h(Button, { size: "sm", ghost: true, onClick: onRefresh, disabled: refreshing,
                      title: "Read the usage again. For codex this also renews its sign-in, so a plan you just bought shows." },
            refreshing ? "Refreshing..." : "Refresh"))),
      usage.paused ? h("p", { className: "hc-muted" }, "Usage is not read while paused.") : null,
      usage.account ? h("p", { className: "hc-muted" }, "Signed in as " + usage.account) : null,
      usage.error
        ? h("p", { className: "hc-muted" }, "Unknown right now (" + usage.error + "). Work is not held back for it.")
        : null,
      (usage.windows || []).map((w) => {
        const { used, tone, showCap, title } = usageBarView(usage, w);
        const known = used !== null;
        return h("div", { key: w.label, className: "hc-usage" },
          h("div", { className: "hc-usage-head" },
            h("span", null, w.label),
            h("span", { className: "hc-muted" },
              (known ? Math.round(used) + "% used" : "unknown") +
              (w.resets_at ? " · resets " + when(w.resets_at) : ""))),
          h("div", { className: "hc-bar", title },
            known ? h("div", { className: "hc-bar-fill " + tone, style: { width: used + "%" } }) : null,
            showCap ? h("div", { className: "hc-bar-cap", style: { left: cap + "%" } }) : null));
      }),
      usage.held_back
        ? h("p", { className: "hc-warn" }, "Holding work back: " + usage.held_back + ". New tasks follow routing.fallback.")
        : null,
      usage.report_only
        ? h("p", { className: "hc-muted" }, "Report only: this reading never holds work back.")
        : h("div", { className: "hc-cap-row" },
        h("span", { className: "hc-muted" }, "Send tasks elsewhere at"),
        h("input", { className: "hc-input hc-cap", type: "number", min: 1, max: 100,
                     value: draft === null ? cap : draft, onChange: (e) => setDraft(e.target.value) }),
        h("span", { className: "hc-muted" }, "% of any window"),
        draft !== null && String(draft) !== String(cap)
          ? h(Button, { size: "sm", onClick: save, disabled: saving }, saving ? "Saving..." : "Save")
          : null),
      error ? h("p", { className: "hc-error" }, error) : null,
      usage.source ? h("p", { className: "hc-muted" }, "From " + usage.source + ".") : null);
  }

  // What a finished resume did, in one line.
  function resumeOutcome(r) {
    if (r.state === "running") return "Running on " + (r.engine || "an engine") + "...";
    if (r.state === "failed") return "Could not resume: " + r.error;
    const entry = r.entry || {};
    if (r.state === "paused") return "Paused again: " + ((entry.paused && entry.paused.reason) || "");
    if (!entry.commit) return "Finished without a commit: " + (entry.unproven_reason || "no changes");
    return "Committed " + entry.commit.slice(0, 7) + " on " + entry.branch +
      (entry.unproven_reason ? " (unproven: " + entry.unproven_reason + ")" : " (check passed)");
  }

  // Tasks waiting for a usage limit to reset. Nothing resumes on its own.
  function PausedCard({ paused, resumes, onResume, onDiscard }) {
    const [confirming, setConfirming] = useState(null);
    if (!paused.length && !resumes.length) return null;
    return h(Card, { className: "hc-card hc-paused" },
      h(CardHeader, null,
        h("div", { className: "hc-title" }, h(CardTitle, null, "Paused tasks"),
          paused.length ? h(Badge, { tone: "warning" }, String(paused.length)) : null)),
      h(CardContent, null,
        paused.length
          ? h("p", { className: "hc-muted" },
              "Stopped by usage limits. Their unfinished edits stay in their worktrees; Resume continues from there.")
          : null,
        paused.map((p) => h("div", { key: p.id, className: "hc-paused-row" },
          h("div", { className: "hc-paused-main" },
            h("div", null, h("code", null, p.task_id), " ",
              h(Badge, { tone: "outline" }, p.engine + (p.resumes_session ? " · session kept" : ""))),
            h("p", { className: "hc-muted hc-goal" }, p.goal),
            h("p", null, p.reason),
            p.ready
              ? h("p", { className: "hc-muted" }, "Can resume now, on " + p.resume_on + ".")
              : h("p", { className: "hc-warn" }, "Not yet: " + p.not_ready),
            p.worktree ? h("p", { className: "hc-muted" }, "Worktree: " + p.worktree) : null),
          h("div", { className: "hc-actions" },
            h(Button, { size: "sm", onClick: () => onResume(p.id),
                        disabled: !p.ready || p.state === "running" },
              p.state === "running" ? "Resuming..." : "Resume"),
            confirming === p.id
              ? h("div", { className: "hc-actions-row" },
                  h(Button, { size: "sm", outlined: true,
                              onClick: () => { setConfirming(null); onDiscard(p.id); } }, "Discard it"),
                  h(Button, { size: "sm", ghost: true, onClick: () => setConfirming(null) }, "Keep"))
              : h(Button, { size: "sm", ghost: true, disabled: p.state === "running",
                            onClick: () => setConfirming(p.id) }, "Discard")))),
        resumes.length
          ? h("div", { className: "hc-resumes" },
              h("span", { className: "hc-label" }, "Resumed from this page"),
              resumes.map((r) => h("p", { key: r.id + r.started_at },
                h("code", null, r.task_id), " ", resumeOutcome(r))))
          : null));
  }

  // codex's reasoning summary: none/concise/detailed/auto, saved to the next task it runs.
  // Absent when the backend has not shipped the field yet: the card draws nothing.
  function ReasoningSummaryControl({ row, reasoning, onSave, showToast }) {
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    if (row.reasoning_summary == null) return null;
    const choices = (reasoning && reasoning.choices) || ["none", "concise", "detailed", "auto"];
    const value = (reasoning && reasoning.value) || row.reasoning_summary;
    const change = (e) => {
      const next = e.target.value;
      setSaving(true);
      setError("");
      onSave(next).then(() => {}, (err) => setError(reason(err))).finally(() => setSaving(false));
    };
    return h("div", { className: "hc-models" },
      h("div", { className: "hc-models-head" }, h("span", { className: "hc-label" }, "Reasoning summary")),
      h("select", { className: "hc-input", value, disabled: saving, onChange: change },
        choices.map((c) => h("option", { key: c, value: c }, c))),
      h("p", { className: "hc-muted" },
        "What the worker's thinking looks like in Runs; applies to the next task."),
      error ? h("p", { className: "hc-error" }, error) : null);
  }

  function EnginePauseControl({ engine, usage, onSave }) {
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const toggle = () => {
      setSaving(true);
      setError("");
      onSave(engine, !usage.paused).catch((err) => setError(reason(err)))
        .finally(() => setSaving(false));
    };
    return h("div", { className: "hc-usage" },
      h("div", { className: "hc-title" },
        h(Badge, { tone: usage && (usage.paused || usage.pause_problem) ? "warning" : "outline" },
          usage ? (usage.pause_problem ? "Invalid pause setting" : usage.paused ? "Paused" : "Not paused") : "Pause state unavailable"),
        h(Button, { size: "sm", outlined: true, onClick: toggle, disabled: saving || !usage,
                    "aria-pressed": !!(usage && usage.paused) },
          saving ? "Saving..." : usage && usage.paused ? "Resume" : "Pause")),
      usage && usage.pause_problem ? h("p", { className: "hc-error" }, usage.pause_problem) : null,
      h("p", { className: "hc-muted" },
        "New tasks go to the next engine in routing.fallback. The planner still lists this engine until a new Hermes session."),
      error ? h("p", { className: "hc-error" }, error) : null);
  }

  function EngineCard({ row, sandbox, onRefresh, showToast, models, onSaveModels, catalog, onLoadCatalog,
                       usage, onSaveCap, onRefreshUsage, refreshingUsage, reasoning, onSaveReasoning,
                       onSavePaused }) {
    let signIn = null;
    if (row.installed && !row.signed_in) {
      signIn = row.sign_in_in_page
        ? h(CodexSignIn, { onSettled: onRefresh, showToast })
        : h("div", null,
            h("p", null, "Run this in a terminal, then check again:"),
            h(CommandLine, { command: row.sign_in_command, showToast }),
            h(Button, { size: "sm", ghost: true, onClick: onRefresh }, "Check again"));
    }
    return h(Card, { className: "hc-card" },
      h(CardHeader, null,
        h("div", { className: "hc-title" }, h(CardTitle, null, row.name), h(StatusBadge, { row }))),
      h(CardContent, null,
        row.version ? h("p", { className: "hc-muted" }, row.version + " at " + row.path) : null,
        row.problem ? h("p", null, row.problem) : null,
        row.name === "codex"
          ? h("p", { className: "hc-muted" }, "Verify sandbox: " +
              (sandbox.available ? "available" : "unavailable (needs the codex CLI installed)"))
          : null,
        signIn,
        ["codex", "gemini"].includes(row.name)
          ? h(EnginePauseControl, { engine: row.name, usage, onSave: onSavePaused })
          : null,
        h(UsageBlock, { engine: row.name, usage, onSaveCap, onRefresh: onRefreshUsage,
                        refreshing: refreshingUsage }),
        models && models.settings
          ? h(ModelSettings, { engine: row.name, block: models.settings[row.name],
                               efforts: (models.efforts && models.efforts[row.name]) || [],
                               onSave: onSaveModels, catalog, onLoadCatalog })
          : null,
        row.name === "codex"
          ? h(ReasoningSummaryControl, { row, reasoning, onSave: onSaveReasoning, showToast })
          : null));
  }

  // Read configured planner labels without resolving credentials.
  function PlannerCard() {
    const [state, setState] = useState({ loading: true });
    useEffect(() => {
      api("/planner").then(
        (login) => setState({ info: login, login }),
        (err) => setState({ error: reason(err) }));
    }, []);

    const info = state.info || {};
    const login = state.login;
    return h(Card, { className: "hc-card" },
      h(CardHeader, null,
        h("div", { className: "hc-title" }, h(CardTitle, null, "planner (Hermes)"),
          login
            ? (login.signed_in == null
                ? h(Badge, { tone: "outline" }, "sign-in state not checked")
                : login.signed_in
                ? h(Badge, { tone: "success" }, "Signed in")
                : h(Badge, { tone: "warning" }, "Not signed in"))
            : null)),
      h(CardContent, null,
        state.loading ? h("p", { className: "hc-muted" }, "Checking...") : null,
        state.error ? h("p", { className: "hc-error" }, state.error) : null,
        state.info
          ? h("p", null, (info.provider || "no provider") + (info.model ? " / " + info.model : ""))
          : null,
        login && login.signed_in && login.source
          ? h("p", { className: "hc-muted" }, "Using " + login.source)
          : null,
        login && !login.signed_in && login.detail ? h("p", null, login.detail) : null,
        h("p", { className: "hc-muted" }, "The planner signs in on the Models page."),
        h("a", { className: "hc-link", href: "/models" }, "Open the Models page")));
  }

  function EnginesPage() {
    const { showToast, toast } = useToast();
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);

    const [models, setModels] = useState(null);
    // {loading, data} | {data} | {error}: the model lists behind the pickers, fetched
    // when an editor first opens (codex takes a few seconds), shared by both cards.
    const [catalog, setCatalog] = useState(null);
    const loadCatalog = useCallback((refresh) => {
      setCatalog((current) => ({ data: current && current.data, loading: true }));
      api("/models/available" + (refresh ? "?refresh=true" : "")).then(
        (data) => setCatalog({ data }),
        (err) => setCatalog({ error: reason(err) }));
    }, []);

    const [limits, setLimits] = useState(null);
    const [refreshingUsage, setRefreshingUsage] = useState(false);
    // force: read now and renew codex's sign-in (a changed plan shows); otherwise the
    // server answers from a reading at most a minute old.
    const refreshUsage = useCallback((force) => {
      setRefreshingUsage(true);
      return api("/limits" + (force ? "?refresh=true" : "")).then(
        setLimits, (err) => setLimits({ loadError: reason(err) })).finally(() => setRefreshingUsage(false));
    }, []);
    // Usage moves while the page is open: read it again every five minutes.
    useEffect(() => {
      const timer = setInterval(() => refreshUsage(false), USAGE_POLL_MS);
      return () => clearInterval(timer);
    }, [refreshUsage]);
    const [paused, setPaused] = useState({ paused: [], resumes: [] });
    const loadPaused = useCallback(() => {
      api("/paused").then(setPaused, () => {});
    }, []);

    // codex's reasoning-summary setting: absent (404) on a backend that has not shipped
    // it yet, in which case the card below draws nothing.
    const [reasoning, setReasoning] = useState(null);
    const loadReasoning = useCallback(() => {
      api("/reasoning-summary").then(
        (data) => setReasoning({ value: data.reasoning_summary, choices: data.choices }),
        () => setReasoning(null));
    }, []);
    const saveReasoning = useCallback((value) => api("/reasoning-summary", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reasoning_summary: value }) })
      .then((data) => {
        setReasoning({ value: data.reasoning_summary, choices: data.choices });
        showToast("Reasoning summary set to " + value, "success");
      }), [showToast]);

    // refresh: read usage now rather than from the minute-long cache.
    const load = useCallback((refresh) => {
      setLoading(true);
      api("/engines").then(
        (next) => { setData(next); setError(""); },
        (err) => setError(reason(err))).finally(() => setLoading(false));
      api("/models").then(setModels, (err) => setModels({ loadError: reason(err) }));
      api("/limits" + (refresh === true ? "?refresh=true" : "")).then(
        setLimits, (err) => setLimits({ loadError: reason(err) }));
      loadPaused();
      loadReasoning();
    }, [loadPaused, loadReasoning]);
    useEffect(() => { load(false); }, [load]);

    // A resume runs in the background; follow it until it settles.
    const running = paused.resumes.some((r) => r.state === "running");
    useEffect(() => {
      if (!running) return undefined;
      const timer = setInterval(loadPaused, POLL_MS);
      return () => clearInterval(timer);
    }, [running, loadPaused]);
    const wasRunning = useRef(false);
    useEffect(() => {
      if (wasRunning.current && !running) load(true);
      wasRunning.current = running;
    }, [running, load]);

    const resumeTask = useCallback((id) => {
      api("/paused/" + encodeURIComponent(id) + "/resume", { method: "POST" }).then(
        () => { showToast("Resuming", "success"); loadPaused(); },
        (err) => { showToast(reason(err), "error"); loadPaused(); });
    }, [loadPaused, showToast]);
    const discardTask = useCallback((id) => {
      api("/paused/" + encodeURIComponent(id), { method: "DELETE" }).then(
        () => { showToast("Discarded; its worktree is kept", "success"); loadPaused(); },
        (err) => showToast(reason(err), "error"));
    }, [loadPaused, showToast]);

    // The route saves every engine's cap at once, so one card's edit carries the others.
    const saveCap = useCallback((engine, value) => {
      const engines = (limits && limits.engines) || {};
      const body = {};
      Object.keys(engines).forEach((name) => { body[name] = { max_percent: engines[name].cap }; });
      body[engine] = { max_percent: value };
      return api("/limits", { method: "PUT", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ limits: body }) })
        .then(() => { showToast("Cap saved for " + engine, "success"); load(false); });
    }, [limits, load, showToast]);

    const savePaused = useCallback((engine, paused) => api("/engines/" + engine + "/paused", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused }) })
      .then((saved) => {
        setLimits((current) => current && current.engines
          ? ({ ...current, engines: { ...current.engines,
              [engine]: { ...current.engines[engine], paused: saved.paused, pause_problem: null } } }) : current);
        showToast(engine + (saved.paused ? " paused" : " resumed"), "success");
        return refreshUsage(false).then(loadPaused);
      }), [refreshUsage, loadPaused, showToast]);

    // The route saves every engine at once, so one engine's edit carries the others.
    const saveModels = useCallback((engine, block) => {
      const settings = { ...((models && models.settings) || {}), [engine]: block };
      return api("/models", { method: "PUT", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ settings }) })
        .then((saved) => {
          setModels((current) => ({ ...current, settings: saved.settings, problem: null }));
          showToast("Models saved for " + engine, "success");
        });
    }, [models, showToast]);

    return h("div", { className: "hc-page" },
      h("div", { className: "hc-header" },
        h("div", null,
          h("h1", { className: "hc-h1" }, "Engines"),
          h("p", { className: "hc-muted" },
            "Hermes-Control sends coding tasks to these CLIs. Tasks for an engine that is not signed in, paused, or over its usage cap follow routing.fallback.")),
        h(Button, { size: "sm", outlined: true, onClick: () => load(true), disabled: loading },
          loading ? "Checking..." : "Check again")),
      error ? h("p", { className: "hc-error" }, error) : null,
      models && models.loadError
        ? h("p", { className: "hc-error" }, "Could not load model settings: " + models.loadError +
            ". A dashboard started before Hermes-Control was updated lacks this route; restart it (hermes dashboard).")
        : null,
      models && models.problem
        ? h("p", { className: "hc-error" }, "Model settings problem: " + models.problem +
            ". Saving from an engine card below replaces them.")
        : null,
      limits && limits.loadError
        ? h("p", { className: "hc-error" }, "Could not load usage: " + limits.loadError + ".")
        : null,
      limits && limits.problem
        ? h("p", { className: "hc-error" }, "Limit settings problem: " + limits.problem +
            ". The default cap applies until a card saves a new one.")
        : null,
      h(PausedCard, { paused: paused.paused, resumes: paused.resumes,
                      onResume: resumeTask, onDiscard: discardTask }),
      data
        ? h("div", { className: "hc-grid" },
            data.engines.map((row) => h(EngineCard, {
              key: row.name, row, sandbox: data.sandbox, onRefresh: () => load(true), showToast,
              models, onSaveModels: saveModels, catalog, onLoadCatalog: loadCatalog,
              usage: limits && limits.engines ? limits.engines[row.name] : null,
              onSaveCap: saveCap, onSavePaused: savePaused, refreshingUsage,
              onRefreshUsage: () => refreshUsage(true).then(() => showToast("Usage read again", "success")),
              reasoning, onSaveReasoning: saveReasoning })),
            h(PlannerCard, null))
        : (error ? null : h("p", { className: "hc-muted" }, "Checking engines...")),
      h(Toast, { toast }));
  }

  // --- Runs view: what the planner decided and what each engine did ------------------

  const RUN_LIST_POLL_MS = 10000;
  const STATUS_LABEL = { done: "done", unproven: "unproven", failed: "failed", paused: "paused",
                         running: "running", waiting: "waiting", stopped: "stopped" };

  function ago(epoch) {
    const seconds = Math.max(0, Date.now() / 1000 - epoch);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
    return when(epoch);
  }

  function lasted(start, end) {
    const seconds = Math.max(0, Math.round((end || start) - start));
    if (seconds < 60) return seconds + " s";
    if (seconds < 3600) return Math.round(seconds / 60) + " min";
    return Math.floor(seconds / 3600) + " h " + Math.round((seconds % 3600) / 60) + " min";
  }

  function basename(path) {
    if (!path) return "";
    const parts = String(path).split("/").filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  function RunStateBadge({ state }) {
    if (state === "running") return h(Badge, { tone: "secondary" }, h("span", { className: "hc-live" }), "running");
    if (state === "paused") return h(Badge, { tone: "outline" }, "paused");
    if (state === "stopped") return h(Badge, { tone: "outline" }, "stopped");
    return h(Badge, { tone: "success" }, "finished");
  }

  function Steps({ attempt }) {
    const [all, setAll] = useState(false);
    const steps = all ? attempt.steps : attempt.steps.slice(0, 60);
    if (!attempt.steps.length) return h("p", { className: "hc-muted" }, "No steps recorded.");
    return h("div", { className: "hc-steps" },
      steps.map((step, i) => h("div", { key: i, className: "hc-step" + (step.is_error ? " hc-step-error" : "") },
        h("span", { className: "hc-step-kind" },
          step.type === "tool" ? step.name : step.type === "text" ? "said" : step.type === "thinking" ? "thought" : "error"),
        h("span", { className: step.type === "thinking" ? "hc-muted" : "" },
          step.type === "tool" ? (step.detail || "") + (step.is_error ? "  (failed)" : "") : step.text))),
      attempt.steps.length > 60 && !all
        ? h(Button, { size: "sm", ghost: true, onClick: () => setAll(true) }, "Show all " + attempt.steps.length + " steps")
        : null);
  }

  function lastLine(text) {
    const lines = String(text || "").trim().split("\n");
    return lines[lines.length - 1] || "";
  }

  const OUTCOME_TONE = { done: "ok", unproven: "warn", failed: "bad", paused: "warn",
                         running: "live", waiting: "idle", stopped: "bad" };

  // The one line that says how a task turned out, outcome first.
  function outcomeText(task) {
    const last = task.attempts[task.attempts.length - 1];
    if (task.status === "done") {
      const tail = task.verify && task.verify.output_tail;
      return "✓ " + (tail ? lastLine(tail) : "done");
    }
    if (task.status === "unproven") return "⚠ " + (task.status_reason || "not verified");
    if (task.status === "failed" || task.status === "stopped")
      return "✗ " + (task.status_reason || task.status);
    if (task.status === "paused") return "⏸ " + ((task.paused && task.paused.reason) || "paused");
    if (task.status === "running") {
      const steps = last ? last.steps : [];
      const step = steps.length ? steps[steps.length - 1] : null;
      if (!step) return "working...";
      if (step.type === "tool") return step.name + (step.detail ? ": " + step.detail : "") + (step.is_error ? " (failed)" : "");
      if (step.type === "text") return "said: " + step.text;
      if (step.type === "thinking") return "thought: " + step.text;
      return step.text || "error";
    }
    if (task.status === "waiting")
      return task.depends_on && task.depends_on.length ? "waits for " + task.depends_on.join(", ") : "waiting";
    return "";
  }

  function engineModelCell(task) {
    const last = task.attempts[task.attempts.length - 1];
    const actual = last ? last.engine + (last.model ? " · " + last.model : "") : "";
    const text = !last ? (task.planned_engine || "–")
      : (task.planned_engine && last.engine !== task.planned_engine
          ? task.planned_engine + " → " + actual : actual);
    return h("span", { className: "hc-row-engine", title: text }, text);
  }

  function durationCell(task) {
    if (!task.attempts.length) return "–";
    const total = task.attempts.reduce((s, a) => s + Math.max(0, (a.ended_at || a.started_at) - a.started_at), 0);
    return lasted(0, total);
  }

  function callsCell(task) {
    let calls = 0, errs = 0;
    task.attempts.forEach((a) => { calls += a.tools || 0; errs += a.tool_errors || 0; });
    return h("span", null, calls + " calls", errs > 0 ? h("span", { className: "hc-error" }, " " + errs + " ✗") : null);
  }

  function ActionsCell({ task, expanded, showToast }) {
    return h("div", { className: "hc-row-actions" },
      task.branch
        ? h(Button, { size: "sm", ghost: true,
                      onClick: (e) => { e.stopPropagation(); copy("git merge --no-ff " + task.branch, showToast); } }, "Copy")
        : null,
      h("span", { className: "hc-chevron" }, expanded ? "▾" : "▸"));
  }

  // One engine attempt, expanded: enough to find a refused call without reading every step.
  // allowSteps: false once the Timeline (below) carries the live detail instead - the
  // fallback path (no trace route yet) still wants the old show/hide steps toggle.
  function AttemptBlock({ attempt, allowSteps }) {
    const [showSteps, setShowSteps] = useState(false);
    const [errorsOnly, setErrorsOnly] = useState(false);
    const result = attempt.result;
    const filtered = errorsOnly
      ? { ...attempt, steps: attempt.steps.filter((s) => s.is_error || s.type === "error") } : attempt;
    return h("div", { className: "hc-attempt" },
      h("p", null,
        h("b", null, attempt.engine), attempt.model ? " · " + attempt.model : "",
        " · " + attempt.how, " · " + attempt.tools + " calls",
        attempt.tool_errors
          ? h("span", { className: "hc-error" }, " · " + attempt.tool_errors + " errors")
          : " · 0 errors",
        " · " + lasted(attempt.started_at, attempt.ended_at)),
      result ? h("p", { className: "hc-muted" },
        "tokens in " + (result.input_tokens ?? "–") +
        " · cached " + (result.cached_input_tokens ?? "–") +
        " · out " + (result.output_tokens ?? "–") +
        " · reasoning " + (result.reasoning_tokens ?? "–") +
        (typeof result.cost_usd === "number" ? " · $" + result.cost_usd.toFixed(2) : "")) : null,
      attempt.limit ? h("p", { className: "hc-error" }, "hit its usage limit" +
        (attempt.limit.next && attempt.limit.next !== "pause" ? "; " + attempt.limit.next + " takes over" : "")) : null,
      attempt.error && !attempt.limit ? h("p", { className: "hc-error" }, attempt.error) : null,
      attempt.last_text ? h("p", { className: "hc-said" }, attempt.last_text) : null,
      allowSteps ? h("div", { className: "hc-actions-row" },
        h(Button, { size: "sm", ghost: true, onClick: () => setShowSteps(!showSteps) },
          showSteps ? "hide steps" : "show steps"),
        showSteps
          ? h(Button, { size: "sm", ghost: true, onClick: () => setErrorsOnly(!errorsOnly) },
              errorsOnly ? "all steps" : "errors only")
          : null) : null,
      allowSteps && showSteps ? h(Steps, { attempt: filtered }) : null);
  }

  function VerifyBlock({ verify }) {
    const tail = (verify.output_tail || "").trim().split("\n").slice(-8).join("\n");
    return h("div", { className: "hc-verify" },
      h("p", { className: "hc-clamp", title: verify.command }, h("code", null, verify.command || "")),
      h("p", { className: "hc-muted" }, "exit " + verify.exit_code + (verify.passed ? " · passed" : " · failed") +
        (verify.unproven_reason ? " · " + verify.unproven_reason : "")),
      tail ? h("pre", { className: "hc-tail" }, tail) : null);
  }

  // --- Timeline: what a worker thought, said, and did, in order, as trace entries land --

  function hhmmss(ts) {
    const d = new Date((ts || 0) * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function clip(text, limit) {
    const s = String(text == null ? "" : text);
    return s.length <= limit ? s : s.slice(0, limit - 1) + "…";
  }

  function safeJson(value) {
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  // Tool entries arrive twice under the same tool_id (pending, then completed); every
  // other kind is unique by its log sequence number.
  function traceKey(entry) {
    if (entry.type === "tool") return "tool:" + entry.tool_id;
    // run-level planner entries have their own seq counter: keep them apart from the task's.
    return (entry.type === "planner" ? "planner:" : "seq:") + entry.seq;
  }

  // seq is only comparable within one task_id's own sequence (run-level "" entries
  // have their own counter); sort merged lists by wall-clock ts instead, fixed at
  // first arrival so a tool's completed replacement stays where it started.
  function mergeEntry(list, entry) {
    if (!entry) return list;
    const key = traceKey(entry);
    const idx = list.findIndex((it) => it.key === key);
    if (idx >= 0) {
      const next = list.slice();
      next[idx] = { key, sortTs: list[idx].sortTs, entry };
      return next;
    }
    const next = list.concat([{ key, sortTs: entry.ts, entry }]);
    next.sort((a, b) => a.sortTs - b.sortTs);
    return next;
  }

  function mergeEntries(list, entries) {
    return (entries || []).reduce(mergeEntry, list);
  }

  function matchesTraceFilter(entry, filter) {
    if (filter === "all") return true;
    if (filter === "said") return entry.type === "thinking" || entry.type === "text";
    if (filter === "tools") return entry.type === "tool";
    if (filter === "errors") return (entry.type === "tool" && entry.is_error) ||
      ["limit", "rerouted", "paused", "error", "slot_wait", "wake"].includes(entry.type) ||
      (entry.type === "verify" && entry.data && entry.data.passed === false);
    return true;
  }

  function plannerText(e) {
    if (e.phase === "waiting") return "waiting up to " + (e.wait_seconds ?? "?") + " s";
    if (e.phase === "returned") return "returned " + (e.state || "");
    if (e.phase === "returned_running") return "returned running" +
      (e.waited_seconds != null ? " after " + Math.round(e.waited_seconds) + " s" : "");
    if (e.phase === "status_checked") return "checked status → " + (e.state || "?");
    return e.phase || "planner";
  }

  function resultLine(d) {
    const usage = d.usage || {};
    const parts = ["tokens out " + (usage.output_tokens ?? "–")];
    if (typeof d.total_cost_usd === "number") parts.push("$" + d.total_cost_usd.toFixed(2));
    if (d.ok === false) parts.push("failed");
    return parts.join(" · ");
  }

  function ThinkingEntry({ text }) {
    const [open, setOpen] = useState(false);
    const lines = text.split("\n");
    if (lines.length <= 6) return h("span", { className: "hc-tl-thinking" }, "💭 " + text);
    return h("span", { className: "hc-tl-thinking" },
      "💭 " + (open ? text : lines.slice(0, 6).join("\n") + " …"),
      h(Button, { size: "sm", ghost: true, onClick: () => setOpen(!open) }, open ? "collapse" : "expand"));
  }

  function toolInputLine(entry) {
    const raw = entry.input_raw != null ? String(entry.input_raw)
      : (entry.input && typeof entry.input === "object" ? safeJson(entry.input)
        : (entry.input != null ? String(entry.input) : ""));
    return clip(raw.replace(/\s+/g, " ").trim(), 200);
  }

  function ToolEntry({ entry }) {
    const [open, setOpen] = useState(false);
    const pending = entry.output === undefined && !entry.is_error;
    const input = toolInputLine(entry);
    return h("div", { className: "hc-tl-tool" },
      h("span", null, (entry.is_error ? "✗ " : "⚙ ") + entry.name + (input ? ": " + input : "")),
      entry.duration_s != null ? h("span", { className: "hc-muted" }, " · " +
        (entry.duration_s < 10 ? entry.duration_s.toFixed(2) : Math.round(entry.duration_s)) + " s") : null,
      entry.exit_code != null ? h("span", { className: "hc-muted" }, " · exit " + entry.exit_code) : null,
      pending ? h("span", { className: "hc-tl-spinner", title: "running" }) : null,
      !pending ? h(Button, { size: "sm", ghost: true, onClick: () => setOpen(!open) },
        (open ? "▾" : "▸") + " output") : null,
      open ? h("pre", { className: "hc-tl-output" },
        (entry.output || "") + (entry.output_truncated ? "\n(truncated)" : "") +
        (entry.stderr ? "\n--- stderr ---\n" + entry.stderr : "")) : null);
  }

  function verifyEntryNode(d) {
    const tail = String(d.output_tail || "").trim().split("\n").slice(-3).join("\n");
    return h("div", null,
      h("span", null, (d.passed ? "✓ " : "✗ ") + "exit " + d.exit_code),
      tail ? h("pre", { className: "hc-tl-output" }, tail) : null);
  }

  // One entry, rendered by type; {tone, node} or null when there is nothing to show.
  function traceEntryNode(entry) {
    const d = entry.data || {};
    switch (entry.type) {
      case "planner":
        return { tone: "amber", node: h("span", null, "planner · " + plannerText(entry)) };
      case "plan":
        return { tone: "", node: h("div", null,
          entry.why ? h("div", { className: "hc-tl-why" }, "“" + entry.why + "”") : null,
          h("div", { className: "hc-muted" },
            [entry.engine, entry.model, entry.effort, entry.policy].filter(Boolean).join(" · ")),
          entry.goal ? h("div", { className: "hc-muted" }, entry.goal) : null) };
      case "session":
        return { tone: "", node: h("span", { className: "hc-muted" },
          "session · " + [entry.model, entry.session_id, entry.how].filter(Boolean).join(" · ")) };
      case "thinking":
        return entry.text ? { tone: "dim", node: h(ThinkingEntry, { text: entry.text }) } : null;
      case "text":
        return entry.text ? { tone: "", node: h("span", null, "💬 " + entry.text) } : null;
      case "tool":
        return { tone: entry.is_error ? "rose" : "", node: h(ToolEntry, { entry }) };
      case "commit":
        return { tone: "", node: h("span", null,
          "✓ " + (d.sha ? d.sha.slice(0, 7) : "") + (d.branch ? " on " + d.branch : "")) };
      case "verify":
        return { tone: d.passed ? "" : "rose", node: verifyEntryNode(d) };
      case "proof":
        return { tone: d.verdict === "red-green-proven" ? "" : "amber", node: h("span", null,
          "proof · " + d.verdict + " · red exit " + ((d.red || {}).exit_code ?? "unknown") +
          (d.reason ? " · " + d.reason : "")) };
      case "limit":
        return { tone: "rose", node: h("span", null, "hit its usage limit" +
          (d.next && d.next !== "pause" ? "; " + d.next + " takes over" : "") +
          (d.message ? ": " + d.message : "")) };
      case "rerouted":
        return { tone: "rose", node: h("span", null, "moved from " + d.from + ": " + (d.reason || "")) };
      case "paused":
        return { tone: "rose", node: h("span", null, (d.reason || "paused") +
          (d.resets_at ? " · resets " + when(d.resets_at) : "")) };
      case "slot_wait":
        return { tone: "amber", node: h("span", null, d.state === "waiting"
          ? "waiting for a free " + d.engine + " slot"
          : "got a " + d.engine + " slot after " + d.seconds + " s") };
      case "wake":
        return { tone: "", node: h("span", null, d.message || "machine woke") };
      case "resumed":
        return { tone: "rose", node: h("span", null, "resumed" + (d.engine ? " on " + d.engine : "")) };
      case "error":
        return { tone: "rose", node: h("span", null, d.message || d.error || "error") };
      case "result":
        return { tone: "", node: h("span", { className: "hc-muted" }, resultLine(d)) };
      default:
        return null;
    }
  }

  // The live transcript for one task: what it thought, said, and did, growing as trace
  // entries land. Auto-scrolls while the task runs, unless the user has scrolled up.
  function Timeline({ task, entries, status, reasoningSummary }) {
    const [filter, setFilter] = useState("all");
    const [stuckToBottom, setStuckToBottom] = useState(true);
    const containerRef = useRef(null);
    const running = task.status === "running";
    const visible = entries.filter((e) => matchesTraceFilter(e, filter));

    useEffect(() => {
      const el = containerRef.current;
      if (!el || !running || !stuckToBottom) return;
      el.scrollTop = el.scrollHeight;
    }, [visible.length, running, stuckToBottom]);

    const onScroll = () => {
      const el = containerRef.current;
      if (!el) return;
      setStuckToBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
    };
    const jumpToBottom = () => {
      const el = containerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
      setStuckToBottom(true);
    };

    const last = task.attempts[task.attempts.length - 1];
    const engine = (last && last.engine) || task.planned_engine;

    return h("div", { className: "hc-timeline-wrap" },
      h("div", { className: "hc-tl-filters" },
        [["all", "all"], ["said", "thinking+said"], ["tools", "tools"], ["errors", "errors"]].map(
          ([key, label]) => h("button", { key, type: "button",
            className: "hc-tl-filter" + (filter === key ? " hc-on" : ""), onClick: () => setFilter(key) },
            label)),
        engine === "codex" && reasoningSummary
          ? h(Badge, { tone: "outline" }, "reasoning: " + reasoningSummary) : null,
        status === "loading" && !entries.length ? h("span", { className: "hc-muted" }, "Loading...") : null),
      h("div", { className: "hc-timeline", ref: containerRef, onScroll },
        visible.length ? visible.map((e) => {
          const body = traceEntryNode(e);
          if (!body) return null;
          return h("div", { key: traceKey(e), className: "hc-tl-row" + (body.tone ? " hc-tl-" + body.tone : "") },
            h("span", { className: "hc-tl-time" }, hhmmss(e.ts)),
            h("div", { className: "hc-tl-body" }, body.node));
        }) : (status === "loading" ? null : h("p", { className: "hc-muted" }, "Nothing yet."))),
      !stuckToBottom && running
        ? h("button", { type: "button", className: "hc-tl-jump", onClick: jumpToBottom }, "↓ newest")
        : null);
  }

  // Above the task table: what phase the planner's wait/check loop is in. Hidden for
  // older logs that never recorded planner entries.
  function RunPhaseStrip({ plannerEntries, runState }) {
    if (!plannerEntries.length) return null;
    const checkedCount = plannerEntries.filter((e) => e.phase === "status_checked").length;
    const last = plannerEntries[plannerEntries.length - 1];
    const stages = [
      { key: "planned", label: "planned" },
      { key: "waiting", label: last.phase === "waiting" ? plannerText(last) : "waiting" },
      { key: "returned", label: last.phase === "returned_running" ? plannerText(last) : "returned" },
      { key: "checked", label: "checked" + (checkedCount ? " ×" + checkedCount : "") },
      { key: "finished", label: runState === "paused" ? "paused" : "finished" },
    ];
    let current = 0;
    if (runState !== "running") current = 4;
    else if (last.phase === "status_checked") current = 3;
    else if (last.phase === "returned_running") current = 2;
    else if (last.phase === "waiting") current = 1;
    return h("div", { className: "hc-phase-strip" },
      stages.map((s, i) => h(React.Fragment, { key: s.key },
        i > 0 ? h("span", { className: "hc-phase-arrow" }, "→") : null,
        h("span", { className: "hc-phase" + (i === current ? " hc-phase-on" : "") }, s.label))));
  }

  // Pure diff helpers. Keep response content as text for React children.
  function diffCounts(diff) {
    return diff ? "+" + diff.added + "/-" + diff.removed : "+?/-?";
  }

  function diffSummary(diff) {
    return diff.files.length + (diff.files.length === 1 ? " file" : " files") + " · " + diffCounts(diff);
  }

  function diffSecretWarning(diff) {
    return diff.secret_hits > 0 ? diff.secret_hits +
      " line(s) hide a possible secret (shown as «masked:…»); see git diff in a terminal for the full text" : "";
  }

  function diffFileNote(file) {
    const notes = [];
    if (file.binary) notes.push("binary file");
    if (file.truncated) notes.push("file cut at " +
      file.hunks.reduce((n, hunk) => n + hunk.lines.length, 0) + " lines");
    return notes.join(" · ");
  }

  function diffLineMarker(kind) {
    return kind === "add" ? "+" : kind === "del" ? "-" : " ";
  }
  // End pure diff helpers.

  function DiffContent({ diff, runId, showToast, review }) {
    const command = "hermes control review-pack " + runId + " --diff";
    const warning = diffSecretWarning(diff);
    return h("div", { className: "hc-diff-content" },
      h("p", { className: "hc-muted" }, diffSummary(diff)),
      warning ? h("p", { className: "hc-warn" }, warning) : null,
      diff.truncated ? h("div", { className: "hc-warn" },
        "Diff too large for the page; full diff: ", command, " ",
        h(Button, { size: "sm", outlined: true, onClick: () => copy(command, showToast) }, "Copy")) : null,
      diff.files.map((file, i) => h("details", {
        key: i, className: "hc-diff-file", open: diff.files.length <= 10 },
        h("summary", null, file.status, " · ", file.path, " · ", diffCounts(file),
          diffFileNote(file) ? " · " + diffFileNote(file) : ""),
        h("div", { className: "hc-diff-scroll", tabIndex: 0, role: "region", "aria-label": "File diff" },
          file.hunks.map((hunk, j) => h(React.Fragment, { key: j },
            h("div", { className: "hc-diff-hunk" }, hunk.header),
            hunk.lines.map((line, k) => h(React.Fragment, { key: k },
              h("div", {
              className: "hc-diff-line hc-diff-" +
                (["add", "del", "ctx", "note"].includes(line.kind) ? line.kind : "note") },
              ...(review ? [h("span", { className: "hc-review-gutter" },
                ["add", "del", "ctx"].includes(line.kind)
                  ? h(ReviewAdd, { path: file.path, line, review }) : null)] : []),
              h("span", { className: "hc-diff-number" }, line.old == null ? "" : line.old),
              h("span", { className: "hc-diff-number" }, line.new == null ? "" : line.new),
              h("span", { className: "hc-diff-marker" }, diffLineMarker(line.kind)),
              h("span", null, line.text)),
              review && ["add", "del", "ctx"].includes(line.kind)
                ? h(ReviewLine, { path: file.path, line, review }) : null))))))));
  }

  // Review comments: all user content remains React text children.
  function commentOnLine(comment, path, line) {
    return ["add", "del", "ctx"].includes(line.kind) && comment.path === path &&
      comment.line === (comment.side === "old" ? line.old : line.new);
  }

  function ReviewComment({ comment, review }) {
    return h("div", { className: "hc-review-comment" },
      h("span", null, comment.path + ":" + comment.line + " (" + comment.side + ") — " + comment.text),
      h(Button, { size: "sm", outlined: true, disabled: review.disabled,
        onClick: () => review.setEditor({ ...comment }) }, "Edit"),
      h(Button, { size: "sm", outlined: true, disabled: review.disabled,
        onClick: () => review.save(review.comments.filter(c => c.id !== comment.id)) }, "Delete"));
  }

  function ReviewEditor({ review }) {
    const editor = review.editor;
    return h("div", { className: "hc-review-editor" },
      h("textarea", { value: editor.text, maxLength: 2000, disabled: review.disabled,
        "aria-label": "Review comment", onChange: event => review.setEditor({ ...editor, text: event.target.value }) }),
      h(Button, { size: "sm", disabled: review.disabled || !editor.text.trim(), onClick: () => {
        const comment = { ...editor, text: editor.text.trim() };
        review.save(editor.id ? review.comments.map(c => c.id === editor.id ? comment : c)
          : [...review.comments, comment]);
      } }, "Save"),
      h(Button, { size: "sm", outlined: true, disabled: review.disabled,
        onClick: () => review.setEditor(null) }, "Cancel"));
  }

  function ReviewAdd({ path, line, review }) {
    const side = line.kind === "del" ? "old" : "new";
    return h("button", { className: "hc-review-add", disabled: review.disabled,
      "aria-label": "Comment on " + path + " " + side + " line " + line[side],
      onClick: () => review.setEditor({ path, side, line: line[side], text: "" }) }, "+");
  }

  function ReviewLine({ path, line, review }) {
    const comments = review.comments.filter(c => commentOnLine(c, path, line));
    const editing = review.editor && commentOnLine(review.editor, path, line);
    if (!comments.length && !editing) return null;
    return h("div", { className: "hc-review-line" },
      comments.map(comment => h(ReviewComment, { key: comment.id, comment, review })),
      editing ? h(ReviewEditor, { review }) : null);
  }

  function ReviewCopy({ review, showToast }) {
    return review.comments.length ? h("div", { className: "hc-review-copy" },
      h(Button, { size: "sm", outlined: true, onClick: () => copy(review.note, showToast) }, "Copy review note"),
      h("span", { className: "hc-muted" }, "Pasted into Hermes chat, this note becomes the next task's goal and its commit message.")) : null;
  }

  function TaskReview({ runId, taskId, diff, showToast, editor, setEditor }) {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const [conflict, setConflict] = useState(false);
    const path = "/runs/" + encodeURIComponent(runId) + "/tasks/" + encodeURIComponent(taskId) + "/review";
    useEffect(() => {
      let active = true;
      api(path).then(value => { if (active) setData(value); },
        err => { if (active) setError(reason(err)); });
      return () => { active = false; };
    }, [path]);
    const reload = async () => {
      setBusy(true);
      try {
        setData(await api(path));
        setConflict(false);
        setEditor(null);
        setError("");
      } catch (err) { setError(reason(err)); }
      finally { setBusy(false); }
    };
    const save = async comments => {
      if (busy || conflict || !data) return;
      setBusy(true);
      setError("");
      try {
        // Send only the editable fields; timestamps and commit come from the server.
        const items = comments.map(c => ({ ...(c.id ? { id: c.id } : {}),
          path: c.path, side: c.side, line: c.line, text: c.text }));
        setData(await api(path, { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ version: data.version, comments: items }) }));
        setEditor(null);
      } catch (err) {
        if (/^409\b/.test(String(err.message || err))) {
          setConflict(true);
          setError("Changed in another tab; reload the review");
        } else setError(reason(err));
      } finally { setBusy(false); }
    };
    const review = { comments: data ? data.comments : [], note: data ? data.note : "",
      editor, setEditor, save, disabled: busy || conflict || !data };
    const shown = comment => diff && !diff.error && diff.files.some(file =>
      file.hunks.some(hunk => hunk.lines.some(line => commentOnLine(comment, file.path, line))));
    const hidden = review.comments.filter(comment => !shown(comment));
    return h("div", { className: "hc-review" },
      error ? h("p", { className: "hc-error" }, error) : null,
      error ? h(Button, { size: "sm", outlined: true, disabled: busy, onClick: reload }, "Reload review") : null,
      !data && !error ? h("p", { className: "hc-muted" }, "Loading review…") : null,
      h(ReviewCopy, { review, showToast }),
      diff && !diff.error ? h(DiffContent, { diff, runId, showToast, review }) : null,
      hidden.length ? h("div", { className: "hc-review-hidden" },
        h("p", null, "Comments not on a shown line"),
        hidden.map(comment => h(ReviewComment, { key: comment.id, comment, review })),
        editor && !shown(editor) ? h(ReviewEditor, { review }) : null) : null);
  }
  // End review comments.

  // Shared across panel unmounts and run switches, for this page's lifetime only.
  const taskDiffCache = new Map();

  function TaskChanges({ runId, taskId, commitSha, showToast }) {
    // Keep unsaved comments when the Changes panel is closed.
    const [editor, setEditor] = useState(null);
    const [open, setOpen] = useState(false);
    const [diff, setDiff] = useState(null);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);
    const onToggle = async () => {
      const next = !open;
      setOpen(next);
      if (!next || loading || (diff && !diff.error)) return;
      setLoading(true);
      setError("");
      const key = JSON.stringify([runId, taskId, commitSha]);
      try {
        if (!taskDiffCache.has(key)) {
          taskDiffCache.set(key, api("/runs/" + encodeURIComponent(runId) +
            "/tasks/" + encodeURIComponent(taskId) + "/diff"));
        }
        const response = await taskDiffCache.get(key);
        if (response.error != null) taskDiffCache.delete(key);
        setDiff(response);
      } catch (err) {
        taskDiffCache.delete(key);
        setError(reason(err));
      } finally {
        setLoading(false);
      }
    };
    return h("div", { className: "hc-diff" },
      h(Button, { size: "sm", outlined: true, onClick: onToggle, "aria-expanded": open },
        "Changes (" + diffCounts(diff) + ")"),
      open ? h("div", null,
        loading ? h("p", { className: "hc-muted" }, "Loading changes…") : null,
        error || (diff && diff.error) ? h("p", { className: "hc-error" }, error || diff.error) : null,
        h(TaskReview, { runId, taskId, diff, showToast, editor, setEditor })) : null);
  }

  function ExpandedTask({ task, trace, runId, showToast }) {
    useEffect(() => { trace.ensureTrace(task.id); }, [task.id]);
    const status = trace.statusFor(task.id);
    const fallback = status === "notfound" || status === "error";
    return h("div", { className: "hc-row-expand" },
      task.why ? h("p", { className: "hc-why" }, "“" + task.why + "”") : null,
      task.rerouted ? h("p", { className: "hc-warn" }, (task.rerouted.stayed ? "stays on " : "moved from ") + task.rerouted.from + ": " + task.rerouted.reason) : null,
      (task.limits || []).map((limit, i) => h("p", { key: "lim" + i, className: "hc-warn" },
        "hit its usage limit" + (limit.next && limit.next !== "pause" ? "; " + limit.next + " takes over" : "") +
        (limit.message ? ": " + limit.message : ""))),
      task.paused ? h("p", { className: "hc-warn" }, task.paused.reason +
        (task.paused.resets_at ? " · an engine frees up " + when(task.paused.resets_at) : "")) : null,
      task.attempts.map((attempt, i) => h(AttemptBlock, { key: i, attempt, allowSteps: fallback })),
      fallback
        ? h("p", { className: "hc-muted" }, status === "notfound"
            ? "Live timeline isn't recorded for this run; showing recorded steps."
            : "Could not load the live timeline; showing recorded steps.")
        : h(Timeline, { task, entries: trace.entriesFor(task.id), status, reasoningSummary: trace.reasoningSummary }),
      task.verify ? h(VerifyBlock, { verify: task.verify }) : null,
      task.commit && task.commit.sha ? h(TaskChanges, { key: task.commit.sha, runId, taskId: task.id, commitSha: task.commit.sha, showToast }) : null,
      (task.commit || task.branch || task.worktree) ? h("p", { className: "hc-muted" },
        task.commit ? h("code", null, task.commit.sha.slice(0, 7)) : null,
        task.branch ? " on " + task.branch : "",
        task.worktree ? " · worktree " + task.worktree : "") : null);
  }

  function TaskRow({ task, expanded, onToggle, showToast, trace, runId }) {
    return h("div", { className: "hc-row-wrap" },
      h("div", { className: "hc-row", onClick: onToggle },
        h("span", { className: "hc-dot hc-s-" + task.status, title: STATUS_LABEL[task.status] || task.status }),
        h("code", null, task.id),
        h(Badge, { tone: "outline" }, task.policy || "task"),
        engineModelCell(task),
        h("span", { className: "hc-muted" }, durationCell(task)),
        callsCell(task),
        h("div", { className: "hc-row-outcome hc-out-" + (OUTCOME_TONE[task.status] || "idle") }, outcomeText(task)),
        h(ActionsCell, { task, expanded, showToast })),
      expanded ? h(ExpandedTask, { task, trace, runId, showToast }) : null);
  }

  function summarizeTasks(tasks) {
    const order = ["done", "unproven", "failed", "paused", "running", "waiting", "stopped"];
    const label = { done: "proven", unproven: "unproven", failed: "failed", paused: "paused",
                    running: "running", waiting: "waiting", stopped: "stopped" };
    const counts = {};
    tasks.forEach((t) => { counts[t.status] = (counts[t.status] || 0) + 1; });
    const parts = [tasks.length + (tasks.length === 1 ? " task" : " tasks")];
    order.forEach((k) => { if (counts[k]) parts.push(counts[k] + " " + label[k]); });
    return parts.join(" · ");
  }

  function taskTotals(tasks) {
    let calls = 0, errs = 0, outTok = 0, cacheTok = 0, cost = 0, hasCost = false;
    tasks.forEach((t) => t.attempts.forEach((a) => {
      calls += a.tools || 0;
      errs += a.tool_errors || 0;
      const r = a.result;
      if (r) {
        outTok += r.output_tokens || 0;
        cacheTok += r.cached_input_tokens || 0;
        if (typeof r.cost_usd === "number") { hasCost = true; cost += r.cost_usd; }
      }
    }));
    return { calls, errs, outTok, cacheTok, cost, hasCost };
  }

  function totalsLine(t) {
    const parts = [t.calls + " tool calls"];
    if (t.errs) parts.push(t.errs + " refused or failed");
    parts.push(t.outTok.toLocaleString() + " tokens out");
    parts.push(t.cacheTok.toLocaleString() + " cached in");
    if (t.hasCost) parts.push("$" + t.cost.toFixed(2));
    return parts.join(" · ");
  }

  function RunHeader({ run, streaming, showToast }) {
    const planner = run.planner || {};
    const repo = run.repo || "";
    return h("div", { className: "hc-run-head" },
      h("div", { className: "hc-title" },
        h("h2", { className: "hc-h2" }, "Run ", h("code", null, run.run_id)),
        h("div", { className: "hc-run-badges" },
          h(RunStateBadge, { state: run.state }),
          streaming ? h("span", { className: "hc-stream-flag", title: "Live via server-sent events" }, "live") : null)),
      h("p", { className: "hc-muted", title: repo || undefined },
        (basename(repo) || "unknown repo") + " · started " + when(run.started_at) +
        " · " + lasted(run.started_at, run.updated_at)),
      h("p", { className: "hc-muted" }, summarizeTasks(run.tasks)),
      h("p", { className: "hc-muted" }, totalsLine(taskTotals(run.tasks))),
      h("div", { className: "hc-run-planner" },
        h("span", { className: "hc-muted" }, (planner.model || "model not recorded") +
          (planner.provider ? " · " + planner.provider : "")),
        planner.session_id
          ? h(CommandLine, { command: "hermes chat --resume " + planner.session_id, showToast })
          : null));
  }

  function RunTaskTable({ run, showToast, trace }) {
    const [expanded, setExpanded] = useState({});
    const autoExpanded = useRef(new Set());
    const runningIds = run.tasks.filter((t) => t.status === "running").map((t) => t.id).join(",");
    // A task auto-expands the first time it appears running, so live steps are visible;
    // collapsing it is remembered until the next task starts running.
    useEffect(() => {
      const ids = runningIds ? runningIds.split(",") : [];
      const fresh = ids.filter((id) => !autoExpanded.current.has(id));
      if (!fresh.length) return;
      fresh.forEach((id) => autoExpanded.current.add(id));
      setExpanded((cur) => { const next = { ...cur }; fresh.forEach((id) => { next[id] = true; }); return next; });
    }, [runningIds]);
    const onToggle = (id) => setExpanded((cur) => ({ ...cur, [id]: !cur[id] }));
    const allExpanded = run.tasks.every((task) => expanded[task.id]);
    const onToggleAll = () => setExpanded((cur) => {
      const expand = run.tasks.some((task) => !cur[task.id]);
      const next = { ...cur };
      run.tasks.forEach((task) => { next[task.id] = expand; });
      return next;
    });
    return h(React.Fragment, null,
      run.tasks.length >= 2
        ? h("div", { className: "hc-task-controls" },
            h(Button, { size: "sm", ghost: true, onClick: onToggleAll }, allExpanded ? "Collapse all" : "Expand all"))
        : null,
      h("div", { className: "hc-rows" },
        run.tasks.map((task) => h(TaskRow, {
          key: task.id, task, runId: run.run_id, expanded: Boolean(expanded[task.id]), onToggle: () => onToggle(task.id),
          showToast, trace }))));
  }

  function RunTable({ run, showToast, streaming, trace }) {
    return h("div", { className: "hc-run" },
      h(RunHeader, { run, streaming, showToast }),
      h(RunPhaseStrip, { plannerEntries: trace.plannerEntries, runState: run.state }),
      // Keyed on the run id: a fresh table (fresh expand state) when the selected run changes,
      // but the same table across the live updates of one run.
      h(RunTaskTable, { key: run.run_id, run, showToast, trace }));
  }

  // One SSE frame ("event: x\ndata: {...}\ndata: more\n"), or null when it carries no data
  // (a bare ":" comment, a frame with only an event line).
  function parseSSEFrame(frame) {
    let eventName = "message";
    const dataLines = [];
    frame.split("\n").forEach((line) => {
      if (!line || line[0] === ":") return;
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return null;
    try { return { event: eventName, data: JSON.parse(dataLines.join("\n")) }; }
    catch (_) { return null; }
  }

  function RunsView() {
    const { showToast, toast } = useToast();
    const [list, setList] = useState(null);
    const [error, setError] = useState("");
    const [selected, setSelected] = useState(null);
    const [run, setRun] = useState(null);
    const [runError, setRunError] = useState("");
    const [streaming, setStreaming] = useState(false);

    // codex's current reasoning-summary setting, shown as a tag in a codex task's
    // timeline filter bar. Absent (404) on a backend that has not shipped it yet.
    const [reasoningSummary, setReasoningSummary] = useState(null);
    useEffect(() => {
      api("/reasoning-summary").then((data) => setReasoningSummary(data.reasoning_summary), () => {});
    }, []);

    // Per-task trace entries, fed by one fetch per task (on its first expand) and by
    // "trace" frames off the same stream the table already uses. Entries for the
    // run-level planner (task_id "") are kept separately and merged into every task's
    // list, and drive the phase strip above the table.
    const [traceMap, setTraceMap] = useState({});
    const [traceStatus, setTraceStatus] = useState({});
    const startedTraces = useRef({});
    useEffect(() => {
      setTraceMap({});
      setTraceStatus({});
      startedTraces.current = {};
    }, [selected]);

    const ensureTrace = useCallback((taskId) => {
      if (startedTraces.current[taskId]) return;
      startedTraces.current[taskId] = true;
      setTraceStatus((s) => ({ ...s, [taskId]: "loading" }));
      const runId = selected;
      api("/runs/" + encodeURIComponent(runId) + "/tasks/" + encodeURIComponent(taskId) + "/trace").then(
        (body) => {
          const all = body.entries || [];
          const own = all.filter((e) => e.type !== "planner");
          const planner = all.filter((e) => e.type === "planner");
          setTraceMap((tm) => ({ ...tm, [taskId]: mergeEntries(tm[taskId] || [], own),
                                 "": mergeEntries(tm[""] || [], planner) }));
          setTraceStatus((s) => ({ ...s, [taskId]: "loaded" }));
        },
        (err) => {
          const notFound = /^404\b/.test(String((err && err.message) || err));
          setTraceStatus((s) => ({ ...s, [taskId]: notFound ? "notfound" : "error" }));
        });
    }, [selected]);

    const applyTrace = useCallback((taskId, entry) => {
      if (!entry) return;
      setTraceMap((tm) => ({ ...tm, [taskId]: mergeEntry(tm[taskId] || [], entry) }));
    }, []);

    const entriesFor = useCallback((taskId) => {
      const own = traceMap[taskId] || [];
      const planner = traceMap[""] || [];
      return own.concat(planner).sort((a, b) => a.sortTs - b.sortTs).map((it) => it.entry);
    }, [traceMap]);

    const trace = {
      entriesFor, ensureTrace, reasoningSummary,
      statusFor: (id) => traceStatus[id] || "idle",
      plannerEntries: (traceMap[""] || []).map((it) => it.entry),
    };

    const loadList = useCallback(() => {
      api("/runs").then((body) => {
        setList(body.runs);
        setError("");
        setSelected((current) => current || (body.runs[0] ? body.runs[0].run_id : null));
      }, (err) => setError(reason(err)));
    }, []);
    useEffect(() => {
      loadList();
      const timer = setInterval(loadList, RUN_LIST_POLL_MS); // new runs appear by themselves
      return () => clearInterval(timer);
    }, [loadList]);

    const loadRun = useCallback((id) => {
      api("/runs/" + encodeURIComponent(id)).then((view) => { setRun(view); setRunError(""); },
                                                    (err) => setRunError(reason(err)));
    }, []);
    useEffect(() => { if (selected) loadRun(selected); }, [selected, loadRun]);

    // Follow the open run while it is running: a live SSE stream when the backend has one,
    // 3 s polling as a fallback (the backend may not have shipped /stream yet, or the
    // connection failed). Abandoned when the selection changes, or the run settles.
    const live = run && run.run_id === selected && run.state === "running";
    const paused = run && run.run_id === selected && run.state === "paused";
    useEffect(() => {
      if (paused) {
        setStreaming(false);
        const timer = setInterval(() => loadRun(selected), RUN_LIST_POLL_MS);
        return () => clearInterval(timer);
      }
      if (!live) { setStreaming(false); return undefined; }
      let cancelled = false;
      let pollTimer = null;
      const controller = new AbortController();
      const runId = selected;

      const startPolling = () => {
        if (cancelled || pollTimer) return;
        setStreaming(false);
        pollTimer = setInterval(() => loadRun(runId), POLL_MS);
      };

      SDK.authedFetch(API + "/runs/" + encodeURIComponent(runId) + "/stream", { signal: controller.signal }).then(
        (response) => {
          if (cancelled) return;
          const ctype = response.headers.get("content-type") || "";
          if (!response.ok || !response.body || ctype.indexOf("text/event-stream") < 0) {
            startPolling();
            return;
          }
          setStreaming(true);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          let sawGone = false;
          const pump = () => reader.read().then(({ done, value }) => {
            if (cancelled) return;
            if (done) {
              setStreaming(false);
              if (!sawGone) loadRun(runId); // the backend ends the stream once the run settles
              return;
            }
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const frame = parseSSEFrame(buf.slice(0, idx));
              buf = buf.slice(idx + 2);
              if (!frame) continue;
              if (frame.event === "view") { setRun(frame.data); setRunError(""); }
              else if (frame.event === "gone") { sawGone = true; setRun(null); loadList(); }
              else if (frame.event === "trace") {
                const d = frame.data || {};
                applyTrace(d.task_id || "", d.entry);
              }
            }
            pump();
          }, () => { if (!cancelled) { setStreaming(false); startPolling(); } });
          pump();
        },
        () => { if (!cancelled) startPolling(); });

      return () => {
        cancelled = true;
        controller.abort();
        if (pollTimer) clearInterval(pollTimer);
        setStreaming(false);
      };
    }, [live, paused, selected, loadRun, loadList, applyTrace]);

    return h("div", { className: "hc-runs" },
      h("div", { className: "hc-runs-list" },
        h("div", { className: "hc-models-head" }, h("span", { className: "hc-label" }, "Runs"),
          h(Button, { size: "sm", ghost: true, onClick: loadList }, "Refresh")),
        error ? h("p", { className: "hc-error" }, "Could not load runs: " + error) : null,
        list && !list.length ? h("p", { className: "hc-muted" }, "No runs yet. They appear here when Hermes delegates work.") : null,
        (list || []).map((item) => h("button", {
          key: item.run_id, type: "button", onClick: () => setSelected(item.run_id),
          className: "hc-run-item" + (item.run_id === selected ? " hc-on" : "") },
          h("div", { className: "hc-run-item-head" },
            h("code", null, item.run_id),
            item.state === "running" ? h("span", { className: "hc-live" }) : null,
            h("span", { className: "hc-muted" }, ago(item.updated_at))),
          h("div", { className: "hc-muted" }, basename(item.repo) || "unknown repo"),
          h("div", { className: "hc-dots" }, item.tasks.map((t) => h("span", {
            key: t.id, className: "hc-dot hc-s-" + t.status, title: t.id + ": " + t.status + " (" + t.engine + ")" })))))),
      h("div", { className: "hc-runs-detail" },
        runError ? h("p", { className: "hc-error" }, runError) : null,
        run && run.run_id === selected ? h(RunTable, { run, showToast, streaming, trace })
          : h("p", { className: "hc-muted" }, list && list.length ? "Loading run..." : "")),
      h(Toast, { toast }));
  }

  // --- Diagrams: deterministic drawing of the reviewed repo spec -------------------

  // Pure diagram helpers (also exercised without the dashboard by the node tests).
  function mermaidLabel(value) {
    // Mermaid 11.16.1 encode/decodeEntities turns #...; into HTML entities; its
    // SVG updateTextContentAndStyles only decodes &amp;/&lt;/&gt;. Use visible
    // substitutes instead: SVG text shows them literally and the parser cannot
    // treat them as quotes, markup, comments, directives or statement breaks.
    const visible = { '"': "”", "'": "’", "[": "［", "]": "］", "#": "＃",
      ";": "；", "<": "‹", ">": "›", "&": "＆", "\n": " ", "\r": " ",
      "`": "ˋ", "|": "｜", "\\": "＼", "%": "％", "{": "（", "}": "）" };
    return Array.from(String(value ?? "")).slice(0, 80)
      .map((c) => visible[c] || c).join("");
  }

  function diagramNodeId(renderedId) {
    const match = /flowchart-(n\d+)-\d+$/.exec(renderedId || "");
    return match ? match[1] : null;
  }

  function diagramEdges(view) {
    const edges = (view.edges || []).slice();
    return view.kind === "module" ? edges : edges.sort((a, b) => (a.order || 0) - (b.order || 0));
  }

  function diagramNumberedEdges(view) {
    // Number before dropping undrawable edges so legacy Steps and drawing agree.
    return diagramEdges(view).map((edge, i) => ({ edge, number: edge.order ?? i + 1 }));
  }

  function diagramSummary(items) {
    const verdicts = items.map((item) => item.verdict || "unreviewed");
    const verdict = ["wrong", "unverifiable", "unreviewed"].find((value) => verdicts.includes(value))
      || (items.length ? "confirmed" : "unreviewed");
    return { verdict, claude_edited: items.some((item) => item.claude_edited),
      evidence_invalid: items.some((item) => item.evidence_invalid) };
  }

  function diagramMode(view) {
    const count = (view.nodes || []).length;
    return (view.kind === "module" || view.kind === "flow") && (view.groups || []).length >= 2 && count > 15
      ? "overview" : "all";
  }

  function diagramStepRanges(numbers) {
    const sorted = Array.from(new Set(numbers)).sort((a, b) => a - b);
    const ranges = [];
    for (let i = 0; i < sorted.length; i++) {
      const start = sorted[i];
      let end = start;
      while (sorted[i + 1] === end + 1) end = sorted[++i];
      ranges.push(start === end ? String(start) : start + "-" + end);
    }
    const single = ranges.length === 1 && !ranges[0].includes("-");
    const label = (single ? "step " : "steps ") + ranges.join(", ");
    return label.length > 24 ? label.slice(0, 21) + "..." : label;
  }

  function diagramOverview(view) {
    const original = view.nodes || [];
    let prefix = "overview-";
    while (original.some((node) => String(node.id).startsWith(prefix))) prefix += "-";
    const mapping = new Map();
    const nodes = [];
    (view.groups || []).forEach((group, i) => {
      const members = original.filter((node) => node.group === group.id);
      if (!members.length) return;
      const id = prefix + i;
      members.forEach((node) => mapping.set(node.id, id));
      const suffix = " (" + members.length + " nodes)";
      nodes.push({ id, label: Array.from(String(group.label || "")).slice(0, 80 - suffix.length).join("") + suffix,
        overviewGroup: group.id, ...diagramSummary(members) });
    });
    original.filter((node) => !mapping.has(node.id)).forEach((node) => {
      mapping.set(node.id, node.id); nodes.push(node);
    });
    const merged = new Map();
    diagramNumberedEdges(view).forEach(({ edge, number }) => {
      const from = mapping.get(edge.from), to = mapping.get(edge.to);
      if (from == null || to == null || (from === to && from.startsWith(prefix))) return;
      const key = JSON.stringify([from, to]);
      if (!merged.has(key)) merged.set(key, { from, to, members: [], numbers: [] });
      merged.get(key).members.push(edge);
      merged.get(key).numbers.push(number);
    });
    const edges = Array.from(merged.values()).map((edge, i) => ({
      id: prefix + "edge-" + i, from: edge.from, to: edge.to,
      label: view.kind === "flow" ? diagramStepRanges(edge.numbers)
        : edge.members.length + (edge.members.length === 1 ? " link" : " links"), ...diagramSummary(edge.members) }));
    return { ...view, nodes, edges, groups: [], overview: true };
  }

  function diagramGroup(view, groupId) {
    const members = new Set((view.nodes || []).filter((node) => node.group === groupId).map((node) => node.id));
    const included = new Set(members);
    (view.edges || []).forEach((edge) => {
      if (members.has(edge.from)) included.add(edge.to);
      if (members.has(edge.to)) included.add(edge.from);
    });
    return { ...view, groups: (view.groups || []).filter((group) => group.id === groupId),
      nodes: (view.nodes || []).filter((node) => included.has(node.id)).map((node) => members.has(node.id)
        ? node : { ...node, group: undefined, neighbour: true }),
      edges: (view.edges || []).filter((edge) => included.has(edge.from) && included.has(edge.to)) };
  }

  function diagramSteps(view) {
    const labels = new Map((view.nodes || []).map((node) => [node.id, node.label]));
    return diagramNumberedEdges(view).map(({ edge, number }) => ({ edge,
      text: number + ". " + (labels.get(edge.from) || edge.from) + " → " +
        (labels.get(edge.to) || edge.to) + ": " + (edge.label || "") }));
  }

  function diagramEdgeGroups(view, filter) {
    const labels = new Map((view.nodes || []).map((node) => [node.id, node.label]));
    const groups = new Map();
    diagramEdges(view).filter((edge) => filter === "All" || (filter === "claude-edited"
      ? edge.claude_edited : (edge.verdict || "unreviewed") === filter)).forEach((edge) => {
      if (!groups.has(edge.from)) groups.set(edge.from, { id: edge.from, label: labels.get(edge.from) || edge.from, edges: [] });
      groups.get(edge.from).edges.push(edge);
    });
    return Array.from(groups.values());
  }

  function diagramMissing(entries) {
    return { open: (entries || []).filter((entry) => entry.status !== "resolved" && entry.status !== "dismissed"),
      settled: (entries || []).filter((entry) => entry.status === "resolved" || entry.status === "dismissed") };
  }

  function diagramProgress(record, now = Date.now() / 1000) {
    if (["done", "failed"].includes(record.status)) return "";
    const stages = record.stages || [], stage = stages[stages.length - 1];
    if (!stage) return "In progress: " + record.status;
    const minutes = (start) => Math.max(0, Math.floor((now - start) / 60));
    const total = minutes(record.created_at ?? stages[0].started_at ?? stage.started_at ?? stage.finished_at ?? now);
    if (stage.finished_at != null) return "Between stages: " + stage.stage + " finished, next starting (total " + total + " min)";
    if (stage.started_at == null) return "In progress: " + record.status;
    return "Stage " + stage.stage + " on " + stage.engine + " running for " + minutes(stage.started_at) +
      " min (total " + total + " min)";
  }

  function diagramTheme(theme) {
    const dark = theme === "dark";
    const fill = dark ? "#202938" : "#f8fafc", text = dark ? "#e5e7eb" : "#1f2937";
    const border = dark ? "#64748b" : "#94a3b8";
    return { darkMode: dark, background: dark ? "#111827" : "#ffffff", primaryColor: fill,
      primaryTextColor: text, primaryBorderColor: border, secondaryColor: fill, tertiaryColor: fill,
      secondaryTextColor: text, tertiaryTextColor: text, lineColor: border, textColor: text,
      clusterBkg: "transparent", clusterBorder: border, titleColor: text,
      edgeLabelBackground: fill, actorBkg: fill, actorBorder: border, actorTextColor: text,
      signalColor: text, signalTextColor: text, labelBoxBkgColor: fill, labelTextColor: text };
  }

  function diagramFit(width, height, viewportWidth, viewportHeight) {
    const scale = width > 0 && height > 0 && viewportWidth > 0 && viewportHeight > 0
      ? Math.min(1, viewportWidth / width, viewportHeight / height) : 1;
    return { scale, x: (viewportWidth - width * scale) / 2, y: (viewportHeight - height * scale) / 2 };
  }

  function diagramZoom(transform, requestedScale, x, y) {
    const scale = Math.max(0.01, Math.min(8, requestedScale));
    const ratio = scale / transform.scale;
    return { scale, x: x - (x - transform.x) * ratio, y: y - (y - transform.y) * ratio };
  }

  function toMermaid(view, options = {}) {
    const nodes = view.nodes || [];
    const ids = new Map(nodes.map((node, i) => [node.id, "n" + i]));
    const numberedEdges = diagramNumberedEdges(view).filter(({ edge }) => ids.has(edge.from) && ids.has(edge.to));
    const edges = numberedEdges.map(({ edge }) => edge);
    const lines = [];
    if (view.kind === "sequence") {
      lines.push("sequenceDiagram");
      nodes.forEach((node) => lines.push("participant " + ids.get(node.id) + " as " + mermaidLabel(node.label)));
      edges.forEach((edge) => lines.push(ids.get(edge.from) + "->>" + ids.get(edge.to) + ": " + mermaidLabel(edge.label)));
      return lines.join("\n");
    }
    lines.push(view.overview || view.kind === "flow" ? "flowchart TD" : "flowchart LR");
    const nodeLine = (node) => ids.get(node.id) + '["' + mermaidLabel(node.label) + '"]';
    const grouped = new Set();
    (view.groups || []).forEach((group, i) => {
      lines.push('subgraph g' + i + '["' + mermaidLabel(group.label) + '"]');
      nodes.filter((node) => node.group === group.id).forEach((node) => {
        lines.push(nodeLine(node)); grouped.add(node.id);
      });
      lines.push("end");
    });
    nodes.filter((node) => !grouped.has(node.id)).forEach((node) => lines.push(nodeLine(node)));
    numberedEdges.forEach(({ edge, number }) => {
      const label = view.overview ? edge.label : view.kind === "flow" ? String(number)
        : options.showEdgeLabels ? edge.label : "";
      lines.push(ids.get(edge.from) + (label ? ' -->|"' + mermaidLabel(label) + '"| ' : " --> ") + ids.get(edge.to));
    });
    lines.push("classDef confirmed stroke:#16a34a,stroke-width:2px",
      "classDef wrong stroke:#dc2626,stroke-width:2px,stroke-dasharray:5 3",
      "classDef unverifiable stroke:#6b7280,stroke-width:2px",
      "classDef unreviewed stroke:#6b7280,stroke-width:2px",
      "classDef claude_edited stroke:#2563eb,stroke-width:3px",
      "classDef evidence_invalid stroke:#f97316,stroke-width:3px",
      "classDef neighbour opacity:0.55");
    nodes.forEach((node) => {
      const verdict = ["confirmed", "wrong", "unverifiable"].includes(node.verdict) ? node.verdict : "unreviewed";
      lines.push("class " + ids.get(node.id) + " " + verdict);
      if (node.claude_edited) lines.push("class " + ids.get(node.id) + " claude_edited");
      if (node.evidence_invalid) lines.push("class " + ids.get(node.id) + " evidence_invalid");
      if (node.neighbour) lines.push("class " + ids.get(node.id) + " neighbour");
    });
    edges.forEach((edge, i) => {
      const color = edge.evidence_invalid ? "#f97316" : edge.claude_edited ? "#2563eb"
        : edge.verdict === "confirmed" ? "#16a34a" : edge.verdict === "wrong" ? "#dc2626" : "#6b7280";
      lines.push("linkStyle " + i + " stroke:" + color + ",stroke-width:" +
        (edge.evidence_invalid || edge.claude_edited ? "3px" : "2px") +
        (edge.verdict === "wrong" ? ",stroke-dasharray:5 3" : ""));
    });
    return lines.join("\n");
  }
  // End pure diagram helpers.

  const DIAGRAM_POLL_MS = 5000;
  const diagramFinal = (status) => status === "done" || status === "failed";
  let mermaidLoading = null;
  let mermaidSerial = 0;
  // Mermaid uses global configuration and DOM ids: serialize concurrent renders.
  let mermaidQueue = Promise.resolve();

  function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (!mermaidLoading) {
      mermaidLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/dashboard-plugins/hermes_control/dist/vendor/mermaid.min.js";
        script.onload = () => window.mermaid ? resolve(window.mermaid) : reject(new Error("Mermaid did not initialize"));
        script.onerror = () => reject(new Error("Could not load Mermaid"));
        document.head.appendChild(script);
      });
    }
    return mermaidLoading;
  }

  function DiagramStatus({ status }) {
    return h(Badge, { tone: status === "done" ? "success" : status === "failed" ? "warning" : "secondary" },
      !diagramFinal(status) ? h("span", { className: "hc-live" }) : null, status);
  }

  function DiagramCounts({ counts, stale }) {
    return h("span", { className: "hc-diagram-badges" },
      Object.entries(counts || {}).map(([key, count]) => h(Badge, { key, tone: "outline" },
        key.replace(/_/g, " ") + " " + count)),
      stale === true ? h(Badge, { tone: "warning" }, "may be stale") : null);
  }

  function DiagramEvidence({ evidence }) {
    return h("div", { className: "hc-diagram-evidence" }, (evidence || []).map((entry, i) =>
      h("code", { key: i }, entry.path + ":" + entry.lines[0] + "-" + entry.lines[1])));
  }

  function DiagramItem({ item }) {
    return h(Card, { className: "hc-card hc-diagram-item" },
      h(CardHeader, null, h(CardTitle, null, item ? item.label : "Item details")),
      h(CardContent, null, item ? h(React.Fragment, null,
        h("p", { className: "hc-muted" }, item.type),
        item.note ? h("p", null, item.note) : null,
        h(DiagramEvidence, { evidence: item.evidence }),
        h("p", { className: "hc-diagram-verdict hc-diagram-" + (item.verdict || "unreviewed") }, item.verdict || "unreviewed"),
        item.review_note ? h("p", null, item.review_note) : null,
        item.evidence_invalid ? h("p", { className: "hc-warn" }, "evidence invalid") : null,
        item.claude_edited ? h("p", { className: "hc-diagram-edited" }, "claude edited") : null)
        : h("p", { className: "hc-muted" }, "Select a node in the drawing or an edge below it.")));
  }

  function DiagramFallback({ view, onSelect }) {
    return h("table", { className: "hc-diagram-table" },
      h("thead", null, h("tr", null, ["Item", "Type / connection", "Verdict"].map((label) => h("th", { key: label }, label)))),
      h("tbody", null,
        (view.nodes || []).map((node) => h("tr", { key: "node-" + node.id },
          h("td", null, h(Button, { size: "sm", ghost: true, onClick: () => onSelect("node", node.id) }, node.label)),
          h("td", null, node.type), h("td", null, node.verdict || "unreviewed"))),
        view.overview ? h("tr", null, h("td", { colSpan: 3, className: "hc-muted" }, "Select a group to see its links."))
          : diagramEdges(view).map((edge) => h("tr", { key: "edge-" + edge.id },
          h("td", null, h(Button, { size: "sm", ghost: true, onClick: () => onSelect("edge", edge.id) }, edge.label || edge.id)),
          h("td", null, edge.from + " → " + edge.to + " · " + edge.type), h("td", null, edge.verdict || "unreviewed")))));
  }

  function DiagramDrawing({ view, onSelect, theme, showEdgeLabels = false }) {
    const [drawing, setDrawing] = useState(null);
    const [error, setError] = useState("");
    const container = useRef(null);
    const frame = useRef(null);
    const svgHost = useRef(null);
    const transform = useRef({ scale: 1, x: 0, y: 0 });
    const size = useRef({ width: 1, height: 1 });
    const fitMode = useRef(true);
    const drag = useRef(null);
    const [scale, setScale] = useState(1);
    const [controlError, setControlError] = useState("");
    const mermaidText = toMermaid(view, { showEdgeLabels });
    const apply = (next) => {
      transform.current = next;
      const svg = container.current && container.current.querySelector("svg");
      if (svg) {
        // Restore natural dimensions as well as the transform on the current node.
        svg.style.width = size.current.width + "px"; svg.style.height = size.current.height + "px";
        container.current.style.setProperty("--hc-diagram-height", size.current.height * next.scale + "px");
        svg.style.transform = "translate(" + next.x + "px," + next.y + "px) scale(" + next.scale + ")";
      }
      setScale(next.scale);
    };
    const fit = () => {
      if (!container.current) return;
      fitMode.current = true;
      const viewport = container.current;
      // Size from the width and height ceiling first; measuring the resulting
      // CSS clamp avoids retaining a tall viewport after a short drawing.
      const heightLimit = document.fullscreenElement === frame.current ? viewport.clientHeight
        : window.innerHeight * 0.7 || viewport.clientHeight;
      const fitted = diagramFit(size.current.width, size.current.height, viewport.clientWidth, heightLimit);
      viewport.style.setProperty("--hc-diagram-height", size.current.height * fitted.scale + "px");
      apply(diagramFit(size.current.width, size.current.height, viewport.clientWidth, viewport.clientHeight));
    };
    const zoom = (nextScale, x, y) => {
      if (!container.current) return;
      fitMode.current = false;
      apply(diagramZoom(transform.current, nextScale,
        x ?? container.current.clientWidth / 2, y ?? container.current.clientHeight / 2));
    };
    useEffect(() => {
      let cancelled = false;
      setDrawing(null); setError("");
      const render = () => loadMermaid().then(async (mermaid) => {
        if (cancelled) return;
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", htmlLabels: false,
          flowchart: { htmlLabels: false }, theme: "base", themeVariables: diagramTheme(theme) });
        const id = "hc-diagram-" + (++mermaidSerial);
        try {
          const result = await mermaid.render(id, mermaidText);
          if (!cancelled) setDrawing({ svg: result.svg });
        } finally {
          // Mermaid can leave an error graphic outside React after a rejected render.
          const leftover = document.getElementById("d" + id);
          if (leftover) leftover.remove();
        }
      });
      const pending = mermaidQueue.then(render);
      mermaidQueue = pending.catch(() => {});
      pending.catch((err) => { if (!cancelled) setError(reason(err)); });
      return () => { cancelled = true; };
    }, [mermaidText, theme]);

    useEffect(() => {
      if (!drawing || !svgHost.current) return;
      // Keep Mermaid's DOM outside React's markup props: scale updates must
      // preserve the SVG's dimensions, transform and node event listeners.
      svgHost.current.innerHTML = drawing.svg;
    }, [drawing && drawing.svg]);

    useEffect(() => {
      if (!drawing || !container.current) return undefined;
      const removers = [];
      const nodes = new Map((view.nodes || []).map((node, i) => ["n" + i, node]));
      const attached = new Set();
      container.current.querySelectorAll("g.node, rect.actor[name]").forEach((element) => {
        // Flowchart ids include the render prefix. Sequence actor rectangles
        // carry the participant's safe id in name; select their enclosing group
        // so the overlaid actor text also receives clicks and keyboard focus.
        const flowId = diagramNodeId(element.id);
        const safe = flowId || element.getAttribute("name");
        const node = nodes.get(safe);
        const target = flowId ? element : element.closest("g");
        if (!node || !target || attached.has(target)) return;
        attached.add(target);
        const select = () => onSelect("node", node.id);
        const keydown = (event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
        };
        target.setAttribute("tabindex", "0"); target.setAttribute("role", "button");
        target.setAttribute("aria-label", node.label);
        target.addEventListener("click", select); target.addEventListener("keydown", keydown);
        removers.push(() => { target.removeEventListener("click", select); target.removeEventListener("keydown", keydown); });
      });
      return () => removers.forEach((remove) => remove());
    }, [drawing, view, onSelect]);

    useEffect(() => {
      const viewport = container.current;
      const svg = viewport && viewport.querySelector("svg");
      if (!drawing || !svg) return undefined;
      const box = svg.viewBox.baseVal;
      size.current = { width: box.width || svg.getBBox().width || 1, height: box.height || svg.getBBox().height || 1 };
      fit();
      const wheel = (event) => {
        if (!(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        const rect = viewport.getBoundingClientRect();
        zoom(transform.current.scale * Math.exp(-event.deltaY * 0.002), event.clientX - rect.left, event.clientY - rect.top);
      };
      const down = (event) => {
        if (event.button !== 0) return;
        drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, start: transform.current, moved: false };
      };
      const move = (event) => {
        const current = drag.current;
        if (!current || current.id !== event.pointerId) return;
        const dx = event.clientX - current.x, dy = event.clientY - current.y;
        if (!current.moved && Math.hypot(dx, dy) < 4) return;
        current.moved = true; fitMode.current = false;
        viewport.setPointerCapture(event.pointerId);
        apply({ ...current.start, x: current.start.x + dx, y: current.start.y + dy });
      };
      const up = (event) => {
        if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
        if (drag.current && drag.current.id === event.pointerId) drag.current.id = null;
      };
      const click = (event) => {
        if (drag.current && drag.current.moved) { event.preventDefault(); event.stopPropagation(); }
        drag.current = null;
      };
      const cancel = (event) => { up(event); drag.current = null; };
      const resize = () => { if (fitMode.current) fit(); };
      const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
      if (observer) observer.observe(viewport);
      const listeners = [["wheel", wheel, { passive: false }], ["pointerdown", down], ["pointermove", move],
        ["pointerup", up], ["pointercancel", cancel], ["click", click, true]];
      listeners.forEach(([name, fn, options]) => viewport.addEventListener(name, fn, options));
      window.addEventListener("resize", resize);
      return () => {
        if (observer) observer.disconnect();
        window.removeEventListener("resize", resize);
        listeners.forEach(([name, fn, options]) => viewport.removeEventListener(name, fn, options));
        drag.current = null;
      };
    }, [drawing]);

    return h("div", { className: "hc-diagram-drawing", ref: frame,
        style: { background: diagramTheme(theme).background, color: diagramTheme(theme).primaryTextColor } },
      h("div", { className: "hc-diagram-toolbar", role: "toolbar", "aria-label": "Diagram zoom" },
        h(Button, { size: "sm", ghost: true, onClick: fit }, "Fit"),
        h(Button, { size: "sm", ghost: true, onClick: () => zoom(1) }, "100%"),
        h(Button, { size: "sm", ghost: true, "aria-label": "Zoom out", onClick: () => zoom(transform.current.scale / 1.25) }, "−"),
        h(Button, { size: "sm", ghost: true, "aria-label": "Zoom in", onClick: () => zoom(transform.current.scale * 1.25) }, "+"),
        h("span", { className: "hc-muted", "aria-live": "polite" }, Math.round(scale * 100) + "%"),
        h(Button, { size: "sm", ghost: true, onClick: () => {
          setControlError("");
          const pending = document.fullscreenElement ? document.exitFullscreen()
            : frame.current.requestFullscreen ? frame.current.requestFullscreen() : Promise.reject(new Error("Full screen unavailable"));
          Promise.resolve(pending).catch((err) => setControlError(reason(err)));
        } }, "Full screen")),
      controlError ? h("p", { className: "hc-error" }, controlError) : null,
      error ? h(React.Fragment, null, h("p", { className: "hc-error" }, "Could not draw diagram: " + error),
        h(DiagramFallback, { view, onSelect }))
        : drawing ? h("div", { className: "hc-diagram-svg", ref: container },
            h("div", { ref: svgHost }))
          : h("p", { className: "hc-muted" }, "Drawing diagram..."),
      h("p", { className: "hc-muted" }, "Drag to pan · Ctrl/Cmd + wheel to zoom. Neutral fills; borders: green confirmed · red dashed wrong · grey unverifiable / unreviewed · orange evidence invalid · thick blue claude edited. Faded nodes are neighbours."),
      view.kind === "sequence" ? h("div", { className: "hc-diagram-edges" },
        h("span", { className: "hc-label" }, "Participants"),
        (view.nodes || []).map((node) => h(Button, { key: node.id, size: "sm", ghost: true,
          onClick: () => onSelect("node", node.id) }, node.label + " · " + (node.verdict || "unreviewed")))) : null);
  }

  function DiagramEdgeList({ view, onSelect }) {
    const [filter, setFilter] = useState("All");
    const groups = diagramEdgeGroups(view, filter);
    const numbers = new Map(diagramNumberedEdges(view).map(({ edge, number }) => [edge, number]));
    const labels = new Map((view.nodes || []).map((node) => [node.id, node.label]));
    return h("div", { className: "hc-diagram-edge-list" },
      h("div", { className: "hc-diagram-toolbar", "aria-label": "Filter edges" },
        ["All", "wrong", "unverifiable", "unreviewed", "claude-edited"].map((value) =>
          h("button", { key: value, type: "button", className: "hc-switch-btn" + (filter === value ? " hc-on" : ""),
            "aria-pressed": filter === value, onClick: () => setFilter(value) }, value))),
      h("details", { key: filter }, h("summary", null, "Edges (" + groups.reduce((sum, group) => sum + group.edges.length, 0) + ")"),
        groups.map((group) => h("details", { key: group.id }, h("summary", null, group.label + " (" + group.edges.length + ")"),
          h("div", { className: "hc-diagram-edges" }, group.edges.map((edge) => h(Button, { key: edge.id, size: "sm", ghost: true,
            onClick: () => onSelect("edge", edge.id) }, numbers.get(edge) + ". " +
            (labels.get(edge.to) || edge.to) + ": " + (edge.label || edge.id) + " · " + (edge.verdict || "unreviewed"))))))));
  }

  function DiagramExplorer({ view, theme, onSelect }) {
    const [mode, setMode] = useState(() => diagramMode(view));
    const [groupId, setGroupId] = useState(null);
    const [showEdgeLabels, setShowEdgeLabels] = useState(false);
    const group = (view.groups || []).find((entry) => entry.id === groupId);
    const drawing = mode === "overview" ? diagramOverview(view) : mode === "group" ? diagramGroup(view, groupId) : view;
    const select = (type, id) => {
      const node = type === "node" && drawing.nodes.find((entry) => entry.id === id);
      if (node && node.overviewGroup != null) { setGroupId(node.overviewGroup); setMode("group"); }
      else onSelect(type, id);
    };
    return h("div", null,
      view.kind !== "sequence" ? h("div", { className: "hc-diagram-toolbar", "aria-label": "Diagram scope" },
        (view.groups || []).length >= 2 ? h(Button, { size: "sm", ghost: true,
          onClick: () => { setMode("overview"); setGroupId(null); } }, "Overview") : null,
        mode === "group" ? h("span", null, " > " + (group?.label || groupId)) : null,
        h(Button, { size: "sm", ghost: true, onClick: () => setMode("all") }, "All"),
        view.kind === "module" ? h("label", { className: "hc-radio" }, h("input", { type: "checkbox", checked: showEdgeLabels,
          onChange: (event) => setShowEdgeLabels(event.target.checked) }), "Show edge labels") : null) : null,
      h(DiagramDrawing, { view: drawing, theme, onSelect: select, showEdgeLabels }),
      view.kind === "flow" ? h("div", { className: "hc-diagram-steps" }, h("b", null, "Steps"),
        diagramSteps(mode === "overview" ? view : drawing).map(({ edge, text }, i) => h("div", { key: edge.id || i },
          h(Button, { size: "sm", ghost: true, onClick: () => onSelect("edge", edge.id) }, text)))) : null,
      h(DiagramEdgeList, { view: mode === "overview" ? view : drawing, onSelect }));
  }

  function DiagramMissing({ entries, onSelect }) {
    const { open, settled } = diagramMissing(entries);
    const omission = (entry, i) => h("div", { key: entry.id || i, className: "hc-diagram-omission" },
      h("p", null, entry.label), entry.note ? h("p", null, entry.note) : null,
      h(DiagramEvidence, { evidence: entry.evidence }),
      entry.status === "resolved" ? h("div", null, "Resolved by: ", (entry.by || []).map((id) =>
        h(Button, { key: id, size: "sm", ghost: true, onClick: () => onSelect("item", id) }, id))) : null,
      entry.status === "dismissed" ? h("p", null, "Dismissed" + (entry.reason ? ": " + entry.reason : "")) : null);
    return h(Card, { className: "hc-card" }, h(CardHeader, null, h(CardTitle, null, "Missing")),
      h(CardContent, null, open.length ? open.map(omission) : h("p", { className: "hc-muted" }, "None reported."),
        settled.length ? h("details", null, h("summary", null, "Settled omissions", " (" + settled.length + ")"), settled.map(omission)) : null));
  }

  function DiagramDetail({ diagram, theme }) {
    const [active, setActive] = useState(0);
    const [selection, setSelection] = useState(null);
    const onSelect = useCallback((type, id) => setSelection({ type, id }), []);
    const views = diagram.views || [];
    const view = views[active] || views[0];
    const candidates = selection?.type === "item" ? [view, ...views.filter((entry) => entry !== view)] : [view];
    const item = selection && candidates.filter(Boolean).flatMap((entry) => selection.type === "item"
      ? [...(entry.nodes || []), ...(entry.edges || [])] : (selection.type === "node" ? entry.nodes : entry.edges) || [])
      .find((entry) => entry.id === selection.id);
    return h("div", { className: "hc-diagram-detail" },
      h("div", { className: "hc-header" }, h("h2", { className: "hc-h1" }, diagram.title || diagram.id),
        h(DiagramStatus, { status: diagram.status })),
      h("p", null, diagram.summary || ""),
      h("p", { className: "hc-muted" }, diagram.repo + " · " + (diagram.commit || "") + " · spec author: " + diagram.spec_author),
      diagram.spec_author === "claude" && diagram.counts?.claude_edited > 0
        ? h("p", { className: "hc-diagram-edited" }, "edited by claude after review") : null,
      diagram.reason ? h("p", { className: diagram.status === "failed" ? "hc-error" : "hc-muted" }, diagram.reason) : null,
      !diagramFinal(diagram.status) ? h("p", { className: "hc-muted" }, diagramProgress(diagram)) : null,
      h(DiagramCounts, { counts: diagram.counts, stale: diagram.stale }),
      h("div", { className: "hc-switch", role: "tablist" }, views.map((entry, i) => h("button", {
        key: i, type: "button", role: "tab", "aria-selected": view === entry,
        className: "hc-switch-btn" + (view === entry ? " hc-on" : ""),
        onClick: () => { setActive(i); setSelection(null); } }, entry.title || entry.kind))),
      view ? h("div", { className: "hc-diagram-layout" }, h(DiagramExplorer, { key: active + ":" + diagramMode(view), view, theme, onSelect }),
        h(DiagramItem, { item })) : h("p", { className: "hc-muted" }, "No drawing available yet."),
      h(DiagramMissing, { entries: view?.missing, onSelect }),
      h(Card, { className: "hc-card" }, h(CardHeader, null, h(CardTitle, null, "Unknowns")),
        h(CardContent, null, (diagram.unknowns || []).length
          ? h("ul", null, diagram.unknowns.map((entry, i) => h("li", { key: i }, entry)))
          : h("p", { className: "hc-muted" }, "None reported."))),
      h(Card, { className: "hc-card" }, h(CardHeader, null, h(CardTitle, null, "Stages")),
        h(CardContent, null, (diagram.stages || []).map((stage, i) => h("div", { key: i, className: "hc-diagram-stage" },
          h("b", null, stage.stage), " · " + stage.engine + " · " + (stage.model || "default") +
          " · " + (stage.finished_at == null || stage.ok == null ? "pending" : "ok: " + String(stage.ok)) + " · " +
          (stage.started_at ? lasted(stage.started_at, stage.finished_at || Date.now() / 1000) : "–"),
          stage.reason ? h("p", { className: stage.finished_at != null && stage.ok === false ? "hc-error" : "hc-muted" }, stage.reason) : null)))));
  }

  function DiagramsView({ initialId }) {
    const [list, setList] = useState(null);
    const [selected, setSelected] = useState(initialId);
    const [diagram, setDiagram] = useState(null);
    const [error, setError] = useState("");
    const [detailError, setDetailError] = useState("");
    const [note, setNote] = useState("");
    // Current SDK has no theme hook. If a host exposes one, follow its reactive state.
    const themeState = SDK.hooks.useTheme ? SDK.hooks.useTheme() : null;
    const themeName = typeof themeState === "string" ? themeState : themeState && (themeState.resolvedTheme || themeState.theme);
    const theme = themeName === "light" ? "default" : "dark";
    const loadList = useCallback(() => {
      api("/diagrams").then((body) => {
        setList((body.diagrams || []).slice().sort((a, b) => b.created_at - a.created_at)); setError("");
      }, (err) => setError(reason(err)));
    }, []);
    useEffect(() => { loadList(); }, [loadList]);
    const listLive = (list || []).some((entry) => !diagramFinal(entry.status));
    useEffect(() => {
      if (!listLive) return undefined;
      const timer = setInterval(loadList, DIAGRAM_POLL_MS);
      return () => clearInterval(timer);
    }, [listLive, loadList]);
    // Ignore late responses when the selection changes; unknown links leave the list open.
    const detailLive = diagram && diagram.id === selected && !diagramFinal(diagram.status);
    useEffect(() => {
      if (!selected) return undefined;
      let cancelled = false;
      const load = () => api("/diagrams/" + encodeURIComponent(selected)).then((body) => {
        if (cancelled) return;
        setDiagram({ ...body.diagram, stale: body.stale }); setDetailError("");
      }, (err) => {
        if (cancelled) return;
        if (/^404\b/.test(String((err && err.message) || err))) {
          setNote("Diagram not found. Choose a diagram from the list."); setSelected(null); setDiagram(null);
        } else setDetailError(reason(err));
      });
      load();
      const timer = detailLive ? setInterval(load, DIAGRAM_POLL_MS) : null;
      return () => { cancelled = true; if (timer) clearInterval(timer); };
    }, [selected, Boolean(detailLive)]);
    return h("div", { className: "hc-runs hc-diagrams" },
      h("div", { className: "hc-runs-list" },
        h("div", { className: "hc-models-head" }, h("span", { className: "hc-label" }, "Diagrams"),
          h(Button, { size: "sm", ghost: true, onClick: loadList }, "Refresh")),
        error ? h("p", { className: "hc-error" }, "Could not load diagrams: " + error) : null,
        note ? h("p", { className: "hc-muted" }, note) : null,
        list === null && !error ? h("p", { className: "hc-muted" }, "Loading diagrams...") : null,
        list && !list.length ? h("p", { className: "hc-muted" }, "No diagrams yet. Ask the planner for a repo diagram.") : null,
        (list || []).map((entry) => h("button", { key: entry.id, type: "button",
          className: "hc-run-item" + (selected === entry.id ? " hc-on" : ""),
          onClick: () => { setSelected(entry.id); setDetailError(""); setNote(""); } },
          h("b", null, entry.title || entry.id),
          h("span", { className: "hc-muted", title: entry.repo }, basename(entry.repo) + " · " + (entry.commit || "").slice(0, 7)),
          h("span", { className: "hc-muted" }, (entry.kinds || []).join(" · ") + " · " + ago(entry.updated_at)),
          h(DiagramStatus, { status: entry.status }), h(DiagramCounts, { counts: entry.counts, stale: entry.stale })))),
      h("div", { className: "hc-runs-detail" },
        detailError ? h("p", { className: "hc-error" }, "Could not load diagram: " + detailError) : null,
        diagram && diagram.id === selected ? h(DiagramDetail, { key: selected, diagram, theme })
          : h("p", { className: "hc-muted" }, selected ? "Loading diagram..." : "Select a diagram.")));
  }

  function diagramFromQuery() {
    const id = new URLSearchParams(window.location.search).get("diagram");
    return /^d-\d{8}-[0-9a-f]{6}$/.test(id || "") ? id : null;
  }

  // Three views in the one tab a plugin gets.
  // The last view chosen, per browser: the dashboard rewrites the URL, so a hash would not stick.
  const VIEW_KEY = "hermes-control.view";

  function ControlPage() {
    const [initialDiagram] = useState(diagramFromQuery);
    const [view, setView] = useState(() => {
      if (initialDiagram) return "diagrams";
      try {
        const stored = window.localStorage.getItem(VIEW_KEY);
        return ["runs", "diagrams"].includes(stored) ? stored : "engines";
      }
      catch (_) { return "engines"; }
    });
    const choose = (next) => {
      setView(next);
      try { window.localStorage.setItem(VIEW_KEY, next); } catch (_) { /* the view still switches */ }
    };
    return h("div", { className: "hc-page" },
      h("div", { className: "hc-switch", role: "tablist" },
        [["engines", "Engines"], ["runs", "Runs"], ["diagrams", "Diagrams"]].map(([key, label]) => h("button", {
          key, type: "button", role: "tab", "aria-selected": view === key,
          className: "hc-switch-btn" + (view === key ? " hc-on" : ""), onClick: () => choose(key) }, label))),
      view === "diagrams" ? h(DiagramsView, { initialId: initialDiagram })
        : view === "runs" ? h(RunsView, null) : h(EnginesPage, null));
  }

  window.__HERMES_PLUGINS__.register("hermes_control", ControlPage);
})();
