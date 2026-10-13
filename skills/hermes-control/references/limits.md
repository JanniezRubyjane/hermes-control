# Limits, moves and pauses

Hermes-Control checks installation and sign-in before work. A task on an
unavailable engine, including a pin, moves to the other engine. If neither can
run, the call fails before starting and names each reason; relay those reasons.
`rerouted` names the original choice, move reason, sign-in command where
relevant, and the destination policy's resolved model and effort.

Each engine has a user-set usage cap, default 80% of any window. Usage is
checked before every task. A cap or provider limit can move a task. If a limit
arrives mid-task, the other engine continues in the same worktree with existing
edits; `rerouted.continued` is true. Moved and resumed work receives the
destination engine's policy model and effort.

When neither engine can take a task, it pauses. `paused` gives its id, reason
and `resets_at`; dependent tasks pause too. Nothing is committed. The user
resumes with the dashboard Engines tab's Resume button. Report the pause and
reset time; never dispatch the task again, which would duplicate its work.
