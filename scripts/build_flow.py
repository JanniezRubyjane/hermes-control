"""Build docs/hermes-control-flow.html: the Hermes-Control pipeline as one dark SVG diagram.

Run from the repository root: python3 scripts/build_flow.py
Edit the DATA section, re-run; layout is a 4-column grid so boxes stay aligned.
"""
from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs/hermes-control-flow.html"

W, H = 1785, 1630

KIND = {  # fill, stroke
    "amber": ("rgba(120, 53, 15, 0.30)", "#fbbf24"),
    "emerald": ("rgba(6, 78, 59, 0.40)", "#34d399"),
    "rose": ("rgba(136, 19, 55, 0.40)", "#fb7185"),
    "violet": ("rgba(76, 29, 149, 0.40)", "#a78bfa"),
    "cyan": ("rgba(8, 51, 68, 0.40)", "#22d3ee"),
    "slate": ("rgba(30, 41, 59, 0.50)", "#94a3b8"),
}

# Columns: A (left), B/C/G (3 worker columns), B1/C1 (2 middle columns in rows 1 & 3), D (right)
COL = {
    "A": (45, 250),
    "B": (330, 310),
    "C": (695, 285),
    "G": (1035, 325),
    "B1": (330, 485),
    "C1": (875, 485),
    "D": (1420, 320),
}
ROW = {1: (200, 135), 2: (410, 125), 3: (740, 185), 4: (980, 110)}


def cx(col):
    x, w = COL[col]
    return x + w / 2


# ----------------------------------------------------------------------------- DATA
CONTEXT_CARDS = [
    (".hermes.md", ["Repo rules · verify command", "Read from cwd at session start"]),
    ("SKILL.md 0.10.0", ["Choose engine/model + why", "wait → control_status · own-check"]),
    ("config.yaml", ["Models per engine · routing.fallback", "codex.enabled · diagram.author · secret_scan"]),
    ("docs/usage.md", ["Commands · settings · routing", "proof · limits · Resume"]),
    ("tool schema", ["control_dispatch · control_status", "Build once at session start"]),
    ("Claude model list", ["CLI aliases + Hermes built-in list", "no account is contacted"]),
]

BOXES = [
    # (col, row, kind, title, lines)
    ("A", 1, "slate", "Hermes session", [
        "User task · @claude/@codex/@gemini = pin",
        "Engines: claude · codex · gemini",
        "CLI · TUI · desktop",
    ]),
    ("B1", 1, "amber", "Planner (Hermes)", [
        "Split into self-contained tasks",
        "Pick engine · model · effort + why",
        "Read 1-2 files; delegate the rest",
        "diagram.author (codex | gemini) writes/fixes repo diagram specs",
        "diagram.author unset → codex, or gemini when codex is off",
    ]),
    ("C1", 1, "amber", "control_dispatch", [
        "Check allowed model · issue run_id",
        "plan → log · wait up to 300 s",
        "Longer → state: running + run_id",
        "Opt-in: parallel.max_tasks (default 1)",
        "per_engine = host slots · slot_wait",
        "depends_on waits · results in plan order",
    ]),
    ("D", 1, "rose", "Readiness · limits", [
        "Installed + signed in? · below cap?",
        "Over cap / no usage / signed out → routing.fallback",
        "model_rule: kept | fallback_entry | default",
        "engines.codex.enabled=false: codex off as worker",
        "All fallback engines blocked → fail before start",
        "claude: rate_limit_event · claude auth status",
        "gemini: report-only buckets · never holds work back",
    ]),

    ("A", 2, "slate", "Worktree", [
        ".worktrees/subagent-control-<run>-<task>",
        "branch hermes-subagent/…",
        "Tracked files only · no .venv",
    ]),
    ("B", 2, "emerald", "claude worker", [
        "--restricted · acceptEdits",
        "No repo hooks · CLAUDE.md still loads",
        "--tools Read,Edit,Write,Grep,Glob,Bash (−55% ctx)",
        "Bash denied except own-check script",
    ]),
    ("C", 2, "emerald", "codex worker", [
        "workspace-write sandbox · no network",
        "-c model_reasoning_effort · verbosity=low",
        "tool_output_token_limit·reasoning_summary=none",
        "No own-check (sandbox has no pytest)",
        "Done → stop app-server process group",
        "Committed .codex config → refused",
    ]),
    ("G", 2, "emerald", "gemini worker", [
        "agy -p <goal> --output-format stream-json --sandbox",
        "--disable-slash-commands --mode accept-edits --model",
        "Read-only → --mode plan · quota report only",
        "Shell only if toolPermission = proceed-in-sandbox",
        "Denied cmd/outside path/network → failure_mode guard",
        "Committed/changed .agents or .gemini files → refused",
    ]),
    ("D", 2, "rose", "Limit part-way · Paused", [
        "5h/weekly/usage limit → same worktree, next fallback",
        "None available in routing.fallback → paused/<id>.json",
        "Resume / Discard in Engines tab",
    ]),

    ("A", 3, "rose", "Own-check script", [
        "~/.hermes/control/checks/<run>-<task>.sh",
        "mode 0500 · env -i (no API key leaks)",
        "claude checks worktree · commit unproven",
    ]),
    ("B1", 3, "emerald", "Commit on task branch", [
        "hardened git add/commit · redacted goal",
        "Secret scan in commit_worktree under repo lock on staged index",
        "Hit → not committed (git reset; change stays in worktree)",
        "Reason names only path:line and rule (no secret text)",
        "Settings: secret_scan.enabled · allow_paths",
        "Empty diff → unproven",
        "Clean worktree (-p no:cacheprovider)",
    ]),
    ("C1", 3, "rose", "Verify in sandbox", [
        "Separate checkout · committed files only",
        "codex sandbox -P :workspace · offline (even if codex worker off)",
        "Rebuild needed files offline or commit them",
        "verify passes → opt-in red-green proof",
        "red: base + task tests overlaid",
        "Expect fail → green: task commit must pass",
        "Both checks run in sandbox",
        "red = verify → reuse green result",
    ]),
    ("D", 3, "slate", "Result per task", [
        "ok · diff_stat · branch · commit",
        "verify{exit_code, output_tail} · task verdict",
        "red-green-proven · tests-passed · red-passed",
        "red-inconclusive · red-skipped · no-verify",
        "verify-failed · verify-blocked",
        "unproven · not-committed",
        "warnings: check_gaming · weakened_tests",
        "red_passed = advisory warning",
        "warnings leave unproven_reason unchanged",
        "Proven only if unproven_reason = null",
    ]),

    ("A", 4, "slate", "Failed / timed out / stalled", [
        "No commit · work stays in worktree",
        "'diff capture failed' ≠ 'empty diff'",
        "Planner reports failure, never completion",
    ]),
    ("D", 4, "amber", "Human merges", [
        "git diff main...<branch>",
        "git merge --no-ff <branch>",
        "No automatic merge",
        "Keep worktree · check overlaps before merge",
    ]),
]

RUNLOG = ("violet", "Run log — source of truth · ~/.hermes/control/run-<id>.jsonl", [
    "append-only · plan · planner phase · session · tool_start/end · text · limit · rerouted",
    "paused · resumed · slot_wait · wake · commit · verify · proof · summary",
    "All views: run_view() folds events into tasks/attempts · RunLog.tail() → SSE",
    "Run ends → overlaps lists tasks editing the same files from the same base",
    "No events > 35 min = stopped · logs without summary still readable",
])

OBSERVE = [
    (45, 405, "Dashboard · Runs", ["Per-task table · results first", "SSE /runs/{id}/stream · fallback poll 3 s", "errors-only steps · Copy merge"]),
    (475, 405, "hermes control watch", ["Same view in terminal", "redraw 1 s · --once · --no-usage"]),
    (905, 405, "hermes control stats", ["One row per attempt · planned → actual", "tokens · cost · verify · --json", "stats --run RUN_ID / --by-model"]),
    (1335, 405, "Dashboard · Engines", ["sign-in · usage vs cap · Resume", "models editor → config.yaml", "planner: config provider/model · no sign-in check"]),
]

BOTTOM = [
    ("rose", "Safety rules", [
        "policy.py: forbidden flags for worker CLIs · gemini guard on denied actions",
        "secret_scan on staged index under repo lock · hit → git reset (stays in worktree)",
        "verify.py: verify + red/green in codex sandbox only (even if codex worker off)",
        "Commit checkout only · no ignored/untracked files",
    ]),
    ("emerald", "Token economy", [
        "Worker base ctx 39k → 18k per turn (flags)",
        "own-check cuts denied calls · files_hint gives line ranges",
        "wait budget avoids repeat dispatch · read less, delegate more",
    ]),
    ("amber", "scripts/merge_check.sh · before merge", [
        "Combine branches in temp worktree → sandbox check",
        "No sandbox → skip check · exit 3",
        "Human may choose --unsandboxed after diff review",
        "review-pack → diff → merge_check.sh → human merges",
    ]),
]

LEGEND = [("amber", "planner / control"), ("emerald", "engine / worker"), ("rose", "safety · sandbox · limits"),
          ("violet", "source of truth"), ("cyan", "observe"), ("slate", "human input / output")]

# Arrows: (path, color, dashed, label, lx, ly, anchor)
A = "#64748b"
ARROWS = [
    ("M 295 245 L 328 245", A, False, None, 0, 0, ""),
    ("M 815 245 L 873 245", A, False, None, 0, 0, ""),
    ("M 1360 245 L 1418 245", A, False, "each task", 1389, 236, "middle"),
    ("M 1580 335 L 1580 408", "#fb7185", True, "over cap · not signed in", 1588, 365, "start"),
    ("M 1118 335 L 1118 355 L 170 355 L 170 408", A, False, "fresh worktree from HEAD, per task", 644, 350, "middle"),
    ("M 295 464 L 328 464", A, False, None, 0, 0, ""),
    ("M 295 435 L 312 435 L 312 396 L 838 396 L 838 408", A, False, None, 0, 0, ""),
    ("M 295 435 L 312 435 L 312 396 L 1198 396 L 1198 408", A, False, None, 0, 0, ""),
    ("M 640 492 L 693 492", "#fb7185", True, "same", 667, 480, "middle"),
    ("M 0 0", "#fb7185", True, "worktree", 667, 506, "middle"),
    ("M 980 492 L 1033 492", "#fb7185", True, "same", 1007, 480, "middle"),
    ("M 0 0", "#fb7185", True, "worktree", 1007, 506, "middle"),
    ("M 1420 464 L 1362 464", "#fb7185", True, "takes over", 1391, 454, "middle"),
    ("M 170 740 L 170 715 L 312 715 L 312 520 L 328 520", "#fb7185", True, "Bash(<script>) pre-approved", 180, 710, "start"),
    ("M 485 535 L 485 568", A, False, None, 0, 0, ""),
    ("M 838 535 L 838 568", A, False, None, 0, 0, ""),
    ("M 1198 535 L 1198 568", A, False, None, 0, 0, ""),
    ("M 1360 625 L 1418 625", A, False, "done", 1389, 615, "middle"),
    ("M 1580 680 L 1580 700 L 572 700 L 572 738", A, False, "finished → controller landing", 1076, 695, "middle"),
    ("M 815 787 L 873 787", A, False, "commit", 844, 776, "middle"),
    ("M 1360 787 L 1418 787", A, False, "checks", 1389, 776, "middle"),
    ("M 1740 765 L 1765 765 L 1765 175 L 572 175 L 572 198", "#fbbf24", False,
     "summary within wait · otherwise state: running → planner calls control_status", 1168, 170, "middle"),
    ("M 1580 925 L 1580 978", A, False, "branch + merge command", 1588, 955, "start"),
    ("M 572 925 L 572 978", "#a78bfa", False, None, 0, 0, ""),
    ("M 1118 925 L 1118 978", "#a78bfa", False, None, 0, 0, ""),
    ("M 45 625 L 15 625 L 15 1035 L 43 1035", "#94a3b8", True, None, 0, 0, ""),
    ("M 400 1090 L 400 1120 L 248 1120 L 248 1178", "#a78bfa", False, None, 0, 0, ""),
    ("M 678 1090 L 678 1178", "#a78bfa", False, None, 0, 0, ""),
    ("M 1108 1090 L 1108 1178", "#a78bfa", False, "read only", 1116, 1120, "start"),
]

WORKER_RUN = ("emerald", "During worker run — stall detection (sleep/wake aware)", [
    "No activity for stall_seconds → failure_mode: stall · stop worker, keep worktree",
    "Count awake time (monotonic) · after wake, wait wake_grace_seconds · log wake",
    "failure_mode: timeout / stall / guard / engine-exit / start-failure / limit",
    "Defaults: timeout 30 min · stall_seconds 1200 · wake_grace_seconds 300",
])
WORKER_REPORT = ("slate", "Worker closing report", [
    "RESULT / RAN / DEVIATIONS / FOLLOW-UPS",
    "RAN: command · exit code · last output line",
    "Claims need controller verify as proof",
    "review-pack: unbacked_claims",
    "RAN commands absent from worker log",
])

HUMAN_TOOLS = [
    (45, 405, "hermes control review-pack", ["RUN_ID · --diff / --json", "diff · verify · proof · verdict · warnings", "Worker report · unbacked_claims"]),
    (475, 405, "hermes control prune", ["prune = preview first", "--apply = remove worktree + branch", "Only merged · clean · idle"]),
    (905, 405, "hermes control lessons", ["Repeated failures from logs · read only", "first/last seen · --recent DAYS", "Separate older issues not seen recently"]),
    (1335, 405, "hermes control health", ["L1 report-only self-assessment", "sources: run logs · local main · state.db", "Read logs · main · state.db", "Suggest evals · no code/config/skills edits"]),
]

# ----------------------------------------------------------------------------- RENDER
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def box(x, y, w, h, kind, title, lines, title_size=12, line_size=9, rx=6):
    fill, stroke = KIND[kind]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="#0f172a"/>',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>',
           f'<text x="{x + 12}" y="{y + 20}" fill="white" font-size="{title_size}" font-weight="600">{esc(title)}</text>']
    ty = y + 38
    for line in lines:
        out.append(f'<text x="{x + 12}" y="{ty}" fill="#94a3b8" font-size="{line_size}">{esc(line)}</text>')
        ty += 14
    return "\n".join(out)


def boundary(x, y, w, h, color, label, note=None):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="rgba(148,163,184,0.03)" stroke="{color}" stroke-width="1" stroke-dasharray="8,4"/>',
           f'<text x="{x + 12}" y="{y + 18}" fill="{color}" font-size="10" font-weight="600">{esc(label)}</text>']
    if note:
        out.append(f'<text x="{x + w - 12}" y="{y + 18}" fill="#475569" font-size="8" text-anchor="end">{esc(note)}</text>')
    return "\n".join(out)


svg = []
svg.append(f'<rect width="100%" height="100%" fill="url(#grid)"/>')

# arrows first (behind boxes)
for d, color, dashed, label, lx, ly, anchor in ARROWS:
    dash = ' stroke-dasharray="5,5"' if dashed else ""
    marker = {A: "arrow", "#fb7185": "arrow-rose", "#fbbf24": "arrow-amber", "#a78bfa": "arrow-violet",
              "#94a3b8": "arrow-slate"}[color]
    if d != "M 0 0":
        svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"{dash} marker-end="url(#{marker})"/>')
    if label:
        svg.append(f'<text x="{lx}" y="{ly}" fill="{color if color != A else "#94a3b8"}" font-size="8" text-anchor="{anchor}">{esc(label)}</text>')

# context layer
svg.append(boundary(30, 20, 1725, 130, "#94a3b8", "Context — planner reads before each dispatch",
                    "All settings in config.yaml · no new env vars"))
x = 45
for title, lines in CONTEXT_CARDS:
    svg.append(box(x, 50, 265, 80, "cyan", title, lines, title_size=11, line_size=8))
    x += 286

# per-task lane
svg.append(boundary(30, 370, 1725, 335, "#34d399",
                    "Per task — own worktree from HEAD · no task handoffs (depends_on only orders tasks)"))

# main boxes
for col, row, kind, title, lines in BOXES:
    x, w = COL[col]
    y, h = ROW[row]
    svg.append(box(x, y, w, h, kind, title, lines))

# worker monitoring and closing report
svg.append(box(45, 570, 1315, 110, *WORKER_RUN))
svg.append(box(1420, 570, 320, 110, *WORKER_REPORT))

# run log (wide, B+C+G)
x, w = COL["B"][0], COL["G"][0] + COL["G"][1] - COL["B"][0]
y, h = ROW[4]
svg.append(box(x, y, w, h, RUNLOG[0], RUNLOG[1], RUNLOG[2]))

# observe layer
svg.append(boundary(30, 1150, 1725, 280, "#22d3ee", "After run — human tools · results / review / repo upkeep (watch also works during runs)"))
for x, w, title, lines in OBSERVE:
    svg.append(box(x, 1180, w, 90, "cyan", title, lines))

for x, w, title, lines in HUMAN_TOOLS:
    svg.append(box(x, 1290, w, 125, "cyan", title, lines))

# bottom cards
x = 45
for kind, title, lines in BOTTOM:
    svg.append(box(x, 1460, 551, 95, kind, title, lines))
    x += 572

# legend
lx = 45
for kind, label in LEGEND:
    fill, stroke = KIND[kind]
    svg.append(f'<rect x="{lx}" y="1590" width="16" height="10" rx="2" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
    svg.append(f'<text x="{lx + 22}" y="1599" fill="#94a3b8" font-size="8">{esc(label)}</text>')
    lx += 215


def marker(mid, color):
    return (f'<marker id="{mid}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">'
            f'<polygon points="0 0, 10 3.5, 0 7" fill="{color}"/></marker>')


DEFS = "\n".join([
    marker("arrow", A), marker("arrow-rose", "#fb7185"), marker("arrow-amber", "#fbbf24"),
    marker("arrow-violet", "#a78bfa"), marker("arrow-slate", "#94a3b8"),
    '<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
    '<path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/></pattern>',
])

CARDS = [
    ("cyan", "Context — read before work in each session", [
        "Planner starts with repo .hermes.md → SKILL.md · guide: docs/usage.md · pins: @claude/@codex/@gemini",
        "config.yaml: models per engine (use_for + effort) · usage cap · dispatch_wait_seconds",
        "routing.fallback orders fallback engines (model_rule: kept | fallback_entry | default); engines.codex.enabled turns codex worker off",
        "diagram.author (codex | gemini) picks the engine for repo diagram specs (unset: codex, or gemini when codex is off)",
        "dispatch: opt-in settings.parallel.max_tasks · per_engine host slots · slot_wait while waiting",
        "Tool schema built once at session start — tool changes need a new session",
        "Skill directory links to repo — SKILL.md edits take effect immediately"]),
    ("rose", "Safety rules", [
        "Workers cannot prove their work: controller commits, then runs verify in sandbox",
        "claude = --restricted + allow/deny · codex = OS sandbox; refused if repo commits .codex config",
        "gemini = agy --sandbox --disable-slash-commands (shell only on proceed-in-sandbox; denied action → failure_mode guard; refused if .agents/.gemini committed or changed)",
        "Secret scan before commit (commit_worktree, repo lock, staged index): hit runs git reset, keeps worktree change, reports only path:line + rule (secret_scan.enabled, allow_paths)",
        "verify: clean commit checkout · codex sandbox runs every verify even when engines.codex.enabled is false · exit 0 proves the check",
        "verify passes → opt-in red: base + task tests (must fail) → green: task commit (must pass)",
        "warnings: check_gaming / weakened_tests / red_passed are advisory; unproven_reason stays unchanged",
        "No automatic merge: branches go to humans · tasks cannot hand off results",
        "Claude usage comes only from rate_limit_event; account/sign-in data from claude auth status; gemini usage is report-only (dashboard), never holds work back",
        "Claude models: CLI aliases + Hermes built-in list; no account is contacted"]),
    ("emerald", "Task lifecycle", [
        "planned → running → (continued | rerouted | paused → resumed) → committed → verified → (proof if red)",
        "Final states: done · unproven · failed · stopped · paused",
        "run: running → finished | stopped (no events > 35 min)",
        "Each transition = one run log event · all views fold the same events",
        "Run ends with overlaps when tasks edit the same files from the same base",
        "Worker closes with RESULT / RAN / DEVIATIONS / FOLLOW-UPS · review-pack checks unbacked_claims"]),
    ("amber", "Tokens · time", [
        "Task duration and tool calls vary with scope, engine and model",
        "Review token usage and cache reuse in run logs before comparing costs",
        "Fewer worker turns reduce repeated context and tool overhead",
        "Better planning = fewer worker turns: keep a capable planner model"]),
]


def card(kind, title, items):
    return ("<div class=\"card\"><div class=\"card-header\">"
            f"<div class=\"card-dot {kind}\"></div><h3>{esc(title)}</h3></div><ul>"
            + "".join(f"<li>• {esc(i)}</li>" for i in items) + "</ul></div>")


HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes-Control — Pipeline Flow</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'JetBrains Mono', monospace; background: #020617; min-height: 100vh; padding: 2rem; color: white; }}
  svg text {{ font-family: 'JetBrains Mono', monospace; }}
  .container {{ max-width: 1480px; margin: 0 auto; }}
  .header {{ margin-bottom: 1.5rem; }}
  .header-row {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.4rem; }}
  .pulse-dot {{ width: 12px; height: 12px; background: #34d399; border-radius: 50%; animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}
  h1 {{ font-size: 1.5rem; font-weight: 700; letter-spacing: -0.025em; }}
  .subtitle {{ color: #94a3b8; font-size: 0.8rem; margin-left: 1.75rem; }}
  .diagram-container {{ background: rgba(15, 23, 42, 0.5); border-radius: 1rem; border: 1px solid #1e293b; padding: 1.25rem; overflow-x: auto; }}
  svg {{ width: 100%; min-width: 1100px; display: block; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 1rem; margin-top: 1.5rem; }}
  .card {{ background: rgba(15, 23, 42, 0.5); border-radius: 0.75rem; border: 1px solid #1e293b; padding: 1.1rem 1.25rem; }}
  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }}
  .card-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .card-dot.cyan {{ background: #22d3ee; }} .card-dot.emerald {{ background: #34d399; }}
  .card-dot.violet {{ background: #a78bfa; }} .card-dot.amber {{ background: #fbbf24; }} .card-dot.rose {{ background: #fb7185; }}
  .card h3 {{ font-size: 0.875rem; font-weight: 600; }}
  .card ul {{ list-style: none; color: #94a3b8; font-size: 0.75rem; }}
  .card li {{ margin-bottom: 0.35rem; line-height: 1.45; }}
  .footer {{ text-align: center; margin-top: 1.5rem; color: #475569; font-size: 0.7rem; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-row"><div class="pulse-dot"></div><h1>Hermes-Control — Delegation · Pipeline Flow</h1></div>
    <p class="subtitle">Hermes plans → claude / codex / gemini edit in their own worktrees → commit on branches → sandbox checks → humans merge · run log = source of truth · v0.2.0</p>
  </div>
  <div class="diagram-container">
    <svg viewBox="0 0 {W} {H}">
      <defs>
{DEFS}
      </defs>
{chr(10).join(svg)}
    </svg>
  </div>
  <div class="cards">
{chr(10).join(card(*c) for c in CARDS)}
  </div>
  <p class="footer">hermes-control · v0.2.0 · docs/design.md · docs/runs-view.md · docs/limits-and-resume.md · Diagram: docs/hermes-control-flow.html</p>
</div>
</body>
</html>
"""

OUT.write_text(HTML, encoding="utf-8")
print(OUT, len(HTML), "bytes")
