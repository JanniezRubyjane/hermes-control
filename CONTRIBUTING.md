# Contributing

The public repository receives one commit per release, exported from the
maintainer's development tree. Accepted pull requests are applied there and
appear in the next release rather than being merged directly into the public
repository. Contributors are credited in the CHANGELOG if they wish.

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
otherwise it follows `hermes --print-runtime-command` and records the repo and
dependency import path loaded by `hermes_bootstrap`. Older launchers fall back to
the launcher or the install directory reported by `hermes --version`.
If discovery fails, set `HERMES_PYTHON` to the interpreter Hermes runs. For managed
Hermes, also set `HERMES_PYTHON_PATH` to the repo root first, then the managed env
site-packages, colon-separated: the bundled interpreter can import `hermes_cli`
only with that bootstrap import path. Older Hermes interpreters import it alone
and leave `HERMES_PYTHON_PATH` empty. Rerun setup. It installs pytest 9.1.1 and
PyYAML 6.0.3 into `$HERMES_HOME/control/verify/site` (default
`~/.hermes/control/verify/site`) for offline verification and records the
interpreter. Rerun it after every Hermes update. A setup made before PyYAML was
added must rerun `scripts/verify-setup.sh`; `hc-update` does this by itself when
the overlay has no `yaml`. `scripts/check.sh` uses a
temporary `uv` overlay with the same pytest and PyYAML versions and may need
network access on its first run.

Runtime dependencies are inherited from Hermes's Python environment: FastAPI
for the dashboard API and Rich. Keep those dependencies available in that
environment; the offline verify overlay provides only pytest and PyYAML for the
test suite.

## Public export

The maintainers' development notes are kept out of the public repository.
Commit the intended release tree before exporting:

```sh
scripts/export_public.sh /path/to/hermes-control-public
```

The destination must be absent or empty. The script exports only HEAD, filters
its explicit private-path list at the repository root, creates one commit with
the public maintainer identity, then scans for personal paths and known sensitive markers. Any hit
fails the command except the exact fake-key fixture at
`tests/test_planner_status.py:7`. It never pushes. Review the resulting tree and
the README against the release code before publishing it. Uncommitted edits are
not exported.

An optional, unpublished local marker list can add case-insensitive checks.

Screenshots must be taken from a demo dashboard with the dashboard and browser
set to UTC, emails replaced, and metadata chunks stripped.
