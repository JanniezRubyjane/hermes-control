# Dependency example

`depends_on` orders tasks but does not carry code or text from one task to
another. Each task branches from the same HEAD. If task A writes a helper and
task B depends on A to use it, B still starts without the helper and may
produce a plausible but broken diff. Put genuinely sequential work in one
task, or finish A, inspect its result, and then dispatch B with those findings
in B's self-contained goal. Dependencies are useful only to prevent harmful
interleaving.

A dependent is skipped and reported failed if an earlier dependency failed
(`ok` false, verification failed, or a requested verification never ran).
A dependent of a paused task pauses instead.
