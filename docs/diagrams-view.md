# Diagrams view

The dashboard's Engines tab has **Engines**, **Runs** and **Diagrams** views.
The last view chosen is remembered per browser. Ask the planner for the design
or work flow of a repository, then open the link in its reply.

## What it draws

Diagrams lists the newest records first: title, repo basename, seven-character
commit, kinds, status, review counts, relative time and a **may be stale** badge
when the repository's HEAD differs from the stamped commit. Unknown freshness
is not marked stale. Refresh reads the list again; it polls every five seconds
while any record is active, then stops.

Selecting one shows its title, summary, full repo and commit, status and reason,
spec author, and one tab per view:

- **Module** draws components and their connections from left to right, grouped
  into subgraphs. Edge labels start hidden; **Show edge labels** enables them.
- **Flow** draws the work flow from top to bottom with only step numbers on edges.
  The **Steps** list below the drawing shows `1. From → To: edge label` in order;
  selecting a step opens its edge details.
- **Sequence** draws participants and messages in step order.

Every drawing starts at **Fit** in a clipped viewport. **100%** restores natural
SVG size so text stays readable even on large records; **−** and **+** zoom around
the viewport centre. Drag to pan, or hold **Ctrl/Cmd** while using the wheel to
zoom around the pointer. Ordinary wheel scrolling still scrolls the page.
**Full screen** expands the drawing and its toolbar; Escape exits. Fit follows
viewport resizing until you zoom or pan, and can be restored at any time.

Module and flow views with at least two groups and more than 15 nodes start in
**Overview**. Each group becomes one node with its label and member count;
ungrouped nodes stay visible. Connections between overview nodes are merged by
direction: in a module view into a count such as **5 links**, in a flow view into
the step numbers they carry, such as **steps 3, 5-7**; connections inside a group are
omitted. Click or keyboard-select a group to open its subgraph and the outside
nodes directly connected to its members. Those **neighbour** nodes are faded;
only connections among the displayed nodes are drawn. Use the **Overview >
group** breadcrumb to return, or **All** to draw the complete view. Group review
marks summarise their members: any wrong member gives the group a wrong mark,
then unverifiable, then unreviewed; all confirmed members give a confirmed mark.
Evidence-invalid and claude-edited flags also carry through. These drawings are
computed locally from the stored spec and do not start workers. Sequence views
keep their participant/message presentation.

The **Edges (count)** list below the drawing starts collapsed and groups edges
by source node, with each source also collapsed and counted. Filter chips
**All**, **wrong**, **unverifiable**, **unreviewed**, and **claude-edited** narrow
it. In Overview, this list and the flow Steps list retain the real connections
from the whole view; group drilldown limits them to the displayed connections.

Click a node, or select an edge below the drawing, to see its label, type, note,
evidence as `path:start-end`, verdict and review note. Sequence participants also
have text selectors. **Missing** lists open omissions for the current view;
older entries without a status are treated as open. Resolved and dismissed
entries appear in the collapsed **Settled omissions** section. Resolved entries
link their **by** item IDs to item details; dismissed entries show their reason.
The **missing settled** count appears when the record supplies it. **Unknowns**
lists questions the workers could not settle. **Stages** shows each
stage's engine, model, success, reason and duration; running stages show
**pending**. While a record is active, the progress line reads **Stage <stage>
on <engine> running for N min (total M min)** using the latest stage's start
time and the record's creation time. Stage names include spec, review,
review-rest, fix, re-review, re-review-rest and claude-fix. An active detail
refreshes every five seconds, including any spec produced so far. A failed pipeline keeps
its last valid spec visible. If Mermaid cannot load or draw, an error and
selectable node/edge tables replace the drawing.

## Review marks

The drawing follows the dashboard's dark or light theme. Flowchart nodes have
neutral readable fills, and subgraphs have transparent fills with subtle borders.
Node borders and edges use green for **confirmed**, red dashed for **wrong**,
and grey for **unverifiable** or **unreviewed**. Orange borders mark
**evidence invalid**; thick blue borders mark **claude edited**. An item can carry
both flags. The item panel names the flags and verdict explicitly; sequence
selectors also show verdicts. A spec authored by claude with
`counts.claude_edited > 0` carries the note **edited by claude after review**. Confirmation checks a connection against code;
it does not prove the diagram is complete, so read Missing and Unknowns too.

The configured author engine (`codex` by default, or `gemini`) reads the repository and writes a JSON spec; claude reviews every diagram
against the code. If wrong or missing items remain, the author gets one fix round,
followed by a second claude review. Claude then fixes any remaining problems;
that final spec has no further independent review and its changed items are
marked. The pipeline uses read-only worker tasks and records the engines and
models actually used if routing switches them. Invalid evidence is flagged;
malformed specs or a failed stage stop the pipeline with a reason.

Set `plugins.entries.hermes_control.settings.diagram.author` in `config.yaml` to
`codex` or `gemini` to choose which engine runs the `spec` and `fix` stages and
is recorded in `spec_author`. When absent, the author is codex, or gemini when
codex is turned off, so disabling codex does not stop diagrams. `claude` is
rejected because claude reviews the author's spec; an invalid value, an explicit
author that is disabled, or no enabled author engine fails closed before any
stage runs. Unless overridden by `--author-model`
(or `--codex-model`), the author stages use that engine's `default` model from
`plugins.entries.hermes_control.settings.models`.

## Links and records

`http://127.0.0.1:9119/engines?diagram=d-20261014-a1b2c3` opens Diagrams directly
on that record, overriding the remembered view. Use the query string: the
dashboard preserves it when adding `profile`, whereas a hash does not survive.
An unknown id opens the list with a short note. Records live outside the repo
in `~/.hermes/control/diagrams/<id>.json` and retain their repo and commit stamp.
The page uses authenticated `GET /diagrams` and `GET /diagrams/{id}` under
`/api/plugins/hermes_control`. Vendored Mermaid 11.16.1 loads only when a view
is drawn, runs in strict mode with HTML labels disabled, and draws escaped
labels generated from the spec. Labels are cut to 80 characters; parser
punctuation uses visible Unicode substitutes (for example, `<script>` is drawn
as `‹script›`) so SVG text never shows entity codes or executes directives.

## Cost

Estimates from past worker runs, not measurements of this feature: codex uses
about **0.5–2M tokens** for the spec and possible fix. Each claude review uses
about **150–300k tokens**; two reviews and a possible fix can reach **~0.9M
Claude tokens**, shared with the planner's quota. The planner adds one command
and a short report. A diagram can run three to five worker tasks.

## In a terminal: `hermes control diagram`

```bash
hermes control diagram --repo PATH --ask TEXT [--kind KIND]... [--author-model M] [--codex-model M] [--claude-model M] [--json]
```

`--kind` is repeatable, one value per flag: `--kind module --kind flow --kind sequence`.
Without `--kind`, module and flow views are drawn.

The command prints the title, summary, review counts and dashboard link;
`--json` returns the record. The planner runs it in the background with
completion notification and reports the result in the user's language without
pasting the spec. If it fails, report status and reason; do not rerun
automatically. Configure the link base with
`plugins.entries.hermes_control.settings.dashboard_url` in `config.yaml`
(default `http://127.0.0.1:9119`). Restart the dashboard after updating the plugin.
