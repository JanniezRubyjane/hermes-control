# Review procedure

Review a proven codex write read-only on claude. Use the review model listed in
settings; select the risky-diff model for `policy.py`, `verify.py`, worktrees,
cross-module or task-contract changes, or a diff over about 300 lines. A claude
read-only worker starts from HEAD and cannot run git, so include the original
goal and `git diff main...<branch>` in its review goal. If the diff is too long
to paste, use the codex model listed for hard problems; it can read git diff in
its sandbox. If claude is signed out or over cap, use that codex hard-problems
model. Never use the model that wrote the diff to review it.

Ask for one finding per line in this exact shape:
`[critical|warning|nit] path:line - finding - evidence`.
Choose one severity per finding. Only "no findings" on its own line means a
clean review (Markdown bullets or emphasis and a trailing period are allowed).
"No issues found" or "LGTM" alone count as unparsed. End with a final line
`VERDICT: READY` or `VERDICT: NOT READY`.
The last `VERDICT:` line wins over prose. Without a `VERDICT:` line, the last
line containing `NOT READY` or `READY TO MERGE` supplies the legacy verdict.
Name review tasks `review<N>-<task>` (N = round number; health reads the round
from that id). An unparsed review counts as not reviewed. If findings
hold, send them verbatim to a new codex write task. Allow at most two review
rounds. Report the task and review costs in calls and seconds after the review.
Relay the worker report's DEVIATIONS and review-pack's unbacked claims to the planner.
`unbacked_claims` is advisory: it matches commands across all attempts and does not compare exit codes.
