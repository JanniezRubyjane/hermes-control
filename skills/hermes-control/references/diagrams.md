# Repository diagrams

When a user asks for a diagram, design picture or work flow of a repo, run
`hermes control diagram --repo <path> --ask "<their request>"` in the terminal
in the background with completion notify. Pass the request as one quoted
argument; escape shell metacharacters rather than executing them. The pipeline
passes the spec between workers; never copy it into the planner's context or
paste it to the user.

Use `--kind module` for components, boundaries and dependencies; `--kind flow`
for numbered work flow steps; `--kind sequence` for ordered interactions
between participants. Multiple kinds can be requested with
`--kind module --kind flow --kind sequence`. Without `--kind`, module and flow
views are drawn.
Optional overrides are `--author-model M` (`--codex-model M` alias), `--claude-model M` and `--lang LANGUAGE`
(default `en`). Use `--lang` for the user's language: summaries, notes, missing
notes and unknowns use it; ids, labels, code names and paths stay English.
The language is stored in the record's `lang` field. Normal command
output gives the short report; `--json` returns the full record, so avoid it
when only the summary and link are needed.

Set `plugins.entries.hermes_control.settings.diagram.author` to `codex` or
`gemini` to choose the engine that runs the `spec` and `fix` author stages and
is recorded in `spec_author`. When absent, the author is codex, or gemini when
codex is turned off. `claude` is rejected because claude reviews the author's
spec; an explicit author that is disabled, no enabled author engine, or an
invalid setting fails before any stage is dispatched. When no `--author-model` / `--codex-model`
is passed, the author stages use that engine's `default` from `settings.models`.

The configured author (`codex` by default, or `gemini`) writes, claude reviews, the author gets one fix round, claude reviews again,
then claude fixes remaining wrong or missing items. Each review requests exact
item ids. If it leaves items unreviewed, one `review-rest` or `re-review-rest`
stage checks only those ids, preserving earlier verdicts and appending new
omissions. These stages use status `reviewing` and `re-reviewing`, respectively.
If incomplete after that, the stage reason reports the remaining count.
The limit is 1 author spec + up to 4 claude review stages + 1 author fix + 1 claude
fix. Report title, summary, review counts and the link in the language the
user writes in. The link opens the diagram directly in the dashboard's Engines
tab via `?diagram=<id>`. The configured `dashboard_url` defaults to
`http://127.0.0.1:9119`.

Green means confirmed; red dashed means wrong; grey means unverifiable or
unreviewed. Orange outlines flag invalid code evidence, blue outlines mark
claude edits. Click nodes or select edges for `path:start-end` evidence and the
review note. Missing lists omissions and Unknowns lists unsettled questions.
Only behaviour no node or edge covers belongs in Missing. Each omission has
a view-local `missing-N` id and status `open`, `resolved` (with `by` item ids),
or `dismissed` (with a `reason`). `counts.missing` counts open omissions;
`counts.missing_settled` counts resolved and dismissed ones. A diagram is clean
when it has no wrong items and no open omissions.

Fix specs can explicitly settle omissions with a top-level `resolves` list:
`[{"view":"module","missing":"missing-1","by":["store"]}]` or
`[{"view":"module","missing":"missing-1","dismiss":"Already covered"}]`.
Dismissal reasons are at most 200 characters. Unknown ids are ignored. Codex's
claims go to re-review, which produces fresh omissions. Final claude claims
resolve an omission only if every referenced item survives the read checks;
matching labels alone never clears an omission.

Claude must open cited code with Read. Confirmations without any fully opened
evidence range become unverifiable. Final added or changed items require all
their evidence ranges to have been opened; unsupported edits are reverted,
and added nodes' dependent edges are dropped. Only wrong items can be deleted.
If claude reads no code, or filtering makes the spec invalid, the reviewed spec
stays. Unreadable logs skip these checks with a stage reason.

Every new view is limited to 30 nodes and 50 edges; node, edge and group labels
are at most 40 characters, item notes 200 and summary 600. Fixes merge or drop
minor items to fit; an oversized fix fails and keeps the last valid spec.
Old stored diagrams remain loadable. Summaries omit the separately shown repo
path and commit hash.
If `spec_author` is claude and `counts.claude_edited > 0`, say it was edited
by claude after review; that final fix has no further independent check.
Confirmation does not prove completeness.

`stale: true` means repo HEAD moved since the stamped commit: the dashboard
marks **may be stale**. `null` means freshness could not be checked, not that
the diagram is current. Keep the original link; do not regenerate without a
new user request.

Cost estimates from past runs, not measurements of this feature: codex
0.5–2M tokens (spec and possible fix), claude 150–300k per review; two reviews
and a possible fix can reach ~0.9M Claude tokens; follow-up reviews add cost, shared with the planner's
quota. The planner adds one command and a short report.

On failure, report status and reason, plus the link if available. The last
valid spec remains viewable. Do not rerun automatically. If the diagram link
is unknown, the page falls back to the list; if Mermaid fails, selectable
tables show the items. The user can refresh after the dashboard is restarted
following a plugin update.
