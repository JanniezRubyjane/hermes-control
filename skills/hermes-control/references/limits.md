# Limits, moves and pauses

Hermes-Control checks installation and sign-in before work. A task on an
unavailable engine, including a pin, moves to the next usable engine in the
user's `routing.fallback` list for that engine, or, when that engine has no
list, to the other enabled engines in default order. If none of them is usable,
the task stays on its engine and pauses. Only when no enabled engine is usable
at all does the call fail before starting with "no engine can take work right
now - <reasons>"; relay those reasons. `rerouted` names the original choice,
move reason, sign-in command where relevant, and the resolved model and effort.

Each engine has a user-set usage cap, default 80% of any window. Usage is
checked before every task. A cap or provider limit can move a task. If a limit
arrives mid-task, the next usable engine in that same order continues in the
same worktree with existing edits; `rerouted.continued` is true. With
`routing.fallback` configured, `rerouted.model_rule` says how the model was
chosen: "kept" (the destination's allow-list has the planned model),
"fallback_entry" (the list entry names `engine/model`) or "default" (the
destination's default).

When no engine can take a task, it pauses. `paused` gives its id, reason
and `resets_at`; dependent tasks pause too. Nothing is committed. The user
resumes with the dashboard Engines tab's Resume button. Report the pause and
reset time; never dispatch the task again, which would duplicate its work.
