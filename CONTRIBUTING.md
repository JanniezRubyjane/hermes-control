# Contributing

Start with a failing test for the behavior you want to change, implement the
change, then run the relevant tests. Install the plugin with `scripts/install.sh`
and prepare its offline test environment once with `scripts/verify-setup.sh`.
Every script that uses Hermes honors `HERMES_HOME`; set it to a disposable home
when experimenting. Tests never touch the real `~/.hermes`: `tests/conftest.py`
isolates `HERMES_HOME` and stubs account usage readers. Do not remove that isolation
or make tests depend on real logins, credentials, network access or usage data.

Before submitting a change, run the entire suite:

```sh
/bin/sh scripts/sandbox-check.sh
```

When Hermes-Control verifies a task, it runs that command in its OS sandbox.
Running the script directly uses the current shell's containment; it does not
create a sandbox itself. The optional real-CLI integration tests consume quota
and require explicit selection with `scripts/check.sh -m integration`.

Safety-guard files need a second review: `hermes_control/policy.py`,
`hermes_control/verify.py`, `hermes_control/worktree.py`, worker launch flags and
anything changing sandbox, credential or cleanup boundaries. Call those changes
out in the pull request and have someone other than the author review them.
Describe the user-visible change and the checks that passed. Model names in
examples must be presented as examples; the user's allow-list chooses the route.

## Set up on a clean machine

1. Install [Hermes Agent following its README](https://github.com/NousResearch/hermes-agent#readme),
   including its environment and `uv`. Confirm `hermes --version` works.
2. Install the Claude Code and Codex CLIs you intend to use, put them on `PATH`,
   and sign in with `claude auth login` and `codex login`.
3. Clone this repository and enter the checkout:

   ```sh
   git clone https://github.com/JanniezRubyjane/hermes-control.git
   cd hermes-control
   scripts/install.sh
   scripts/verify-setup.sh
   /bin/sh scripts/sandbox-check.sh
   scripts/check.sh
   ```

`scripts/verify-setup.sh` finds Hermes's Python using `HERMES_PYTHON` when set,
the `hermes` launcher, or the install directory reported by `hermes --version`.
If discovery fails, set `HERMES_PYTHON` to the interpreter that can import
`hermes_cli` and rerun setup. It installs pytest 9.1.1 into
`$HERMES_HOME/control/verify/site` (default `~/.hermes/control/verify/site`) for
offline verification and records the interpreter. Rerun it after a Hermes update
changes that interpreter. `scripts/check.sh` uses a temporary `uv` overlay with
the same pytest version and may need network access on its first run.

Runtime dependencies are inherited from Hermes's Python environment: FastAPI
for the dashboard API, Rich, and PyYAML. Keep those dependencies available in
that environment; the offline pytest installation does not provide them.

## Public export

The maintainers' development notes are kept out of the public repository.
Commit the intended release tree before exporting:

```sh
scripts/export_public.sh /tmp/hermes-control-public
```

The destination must be absent or empty. The script exports only HEAD, filters
its explicit private-path list at the repository root, creates one commit with
the public maintainer identity, then scans for personal paths and known sensitive markers. Any hit
fails the command except the exact fake-key fixture at
`tests/test_planner_status.py:7`. It never pushes. Review the resulting tree and
the README against the release code before publishing it. Uncommitted edits are
not exported.

`docs/dev/leak-markers.txt` is a local, optional, unpublished list of extra
case-insensitive markers.

Screenshots must be taken from a demo dashboard with the dashboard and browser
set to UTC, emails replaced, and metadata chunks stripped.
