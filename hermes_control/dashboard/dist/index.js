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
          h(Button, { size: "sm", ghost: true, onClick: onRefresh, disabled: refreshing,
                      title: "Read the usage again. For codex this also renews its sign-in, so a plan you just bought shows." },
            refreshing ? "Refreshing..." : "Refresh"))),
      usage.account ? h("p", { className: "hc-muted" }, "Signed in as " + usage.account) : null,
      usage.error
        ? h("p", { className: "hc-muted" }, "Unknown right now (" + usage.error + "). Work is not held back for it.")
        : null,
      (usage.windows || []).map((w) => {
        const known = w.used_percent !== null && w.used_percent !== undefined;
        const used = known ? Math.max(0, Math.min(100, w.used_percent)) : null;
        const tone = known && used >= cap ? "hc-bar-over" : known && used >= cap * 0.8 ? "hc-bar-near" : "";
        return h("div", { key: w.label, className: "hc-usage" },
          h("div", { className: "hc-usage-head" },
            h("span", null, w.label),
            h("span", { className: "hc-muted" },
              (known ? Math.round(used) + "% used" : "unknown") +
              (w.resets_at ? " · resets " + when(w.resets_at) : ""))),
          h("div", { className: "hc-bar", title: "cap " + cap + "%" },
            known ? h("div", { className: "hc-bar-fill " + tone, style: { width: used + "%" } }) : null,
            h("div", { className: "hc-bar-cap", style: { left: cap + "%" } })));
      }),
      usage.held_back
        ? h("p", { className: "hc-warn" }, "Holding work back: " + usage.held_back + ". New tasks go to the other engine.")
        : null,
      h("div", { className: "hc-cap-row" },
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

  function EngineCard({ row, sandbox, onRefresh, showToast, models, onSaveModels, catalog, onLoadCatalog,
                       usage, onSaveCap, onRefreshUsage, refreshingUsage, reasoning, onSaveReasoning }) {
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
            "Hermes-Control sends coding tasks to these CLIs. Tasks for an engine that is not signed in, or over its usage cap, run on the other one.")),
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
              onSaveCap: saveCap, refreshingUsage,
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

  function ExpandedTask({ task, trace }) {
    useEffect(() => { trace.ensureTrace(task.id); }, [task.id]);
    const status = trace.statusFor(task.id);
    const fallback = status === "notfound" || status === "error";
    return h("div", { className: "hc-row-expand" },
      task.why ? h("p", { className: "hc-why" }, "“" + task.why + "”") : null,
      task.rerouted ? h("p", { className: "hc-warn" }, "moved from " + task.rerouted.from + ": " + task.rerouted.reason) : null,
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
      (task.commit || task.branch || task.worktree) ? h("p", { className: "hc-muted" },
        task.commit ? h("code", null, task.commit.sha.slice(0, 7)) : null,
        task.branch ? " on " + task.branch : "",
        task.worktree ? " · worktree " + task.worktree : "") : null);
  }

  function TaskRow({ task, expanded, onToggle, showToast, trace }) {
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
      expanded ? h(ExpandedTask, { task, trace }) : null);
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
          key: task.id, task, expanded: Boolean(expanded[task.id]), onToggle: () => onToggle(task.id),
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

  // Two views in the one tab a plugin gets: engine status, and what runs did.
  // The last view chosen, per browser: the dashboard rewrites the URL, so a hash would not stick.
  const VIEW_KEY = "hermes-control.view";

  function ControlPage() {
    const [view, setView] = useState(() => {
      try { return window.localStorage.getItem(VIEW_KEY) === "runs" ? "runs" : "engines"; }
      catch (_) { return "engines"; }
    });
    const choose = (next) => {
      setView(next);
      try { window.localStorage.setItem(VIEW_KEY, next); } catch (_) { /* the view still switches */ }
    };
    return h("div", { className: "hc-page" },
      h("div", { className: "hc-switch", role: "tablist" },
        [["engines", "Engines"], ["runs", "Runs"]].map(([key, label]) => h("button", {
          key, type: "button", role: "tab", "aria-selected": view === key,
          className: "hc-switch-btn" + (view === key ? " hc-on" : ""), onClick: () => choose(key) }, label))),
      view === "runs" ? h(RunsView, null) : h(EnginesPage, null));
  }

  window.__HERMES_PLUGINS__.register("hermes_control", ControlPage);
})();
