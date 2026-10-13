## Change

Describe the problem and the resulting behavior. Link the issue if applicable.

## Validation and review

- [ ] Tests first: added or updated a test and confirmed it failed before implementing the change.
- [ ] Relevant tests pass; list the commands and results below.
- [ ] `/bin/sh scripts/sandbox-check.sh` passes.
- [ ] Safety-guard files touched? If yes, identify them below and obtain a second review from someone other than the author before merging.
- [ ] Docs updated for changes to behavior or setup (or explain why none are needed).

Safety-guard files include `hermes_control/policy.py`, `hermes_control/verify.py`,
`hermes_control/worktree.py`, worker launch flags, and anything changing sandbox,
credential or cleanup boundaries.

Commands and results:

Safety-guard changes and second reviewer (or "none"):
