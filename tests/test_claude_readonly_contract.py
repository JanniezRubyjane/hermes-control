from pathlib import Path

from hermes_control.engines import claude
from hermes_control.engines.base import ControlTask
from hermes_control.policy import ExecPolicy
from hermes_control.worker_report import REPORT_REQUEST


def _prompt(monkeypatch, *, write):
    monkeypatch.setattr(claude, "_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude, "_supports_restricted", lambda path: False)
    task = ControlTask(id="t1", goal="review the diff", engine="claude",
                       policy=ExecPolicy(write=write))
    argv = claude.ClaudeAdapter().build_argv(task, cwd=Path("/wt"))
    return argv[argv.index("--append-system-prompt") + 1]


def test_readonly_prompt_explains_readonly_shell_commands(monkeypatch):
    prompt = _prompt(monkeypatch, write=False)
    paragraph = (
        "This task is read-only (plan mode): you cannot edit files. Read-only shell "
        "commands such as git diff, git show, git log and git status can run; try "
        "the one you need before reporting that you cannot. Commands that change "
        "files or need the network are refused; do not retry a refused command "
        "in another spelling, with sudo, through env, sh -c, a pipe, or by writing "
        "a scratch file. In your final message, say which checks you could not "
        "run and why."
    )
    assert prompt == claude._WORKER_CONTRACT.split("\n\n", 1)[0] + "\n\n" + paragraph
    assert "git diff" in prompt
    assert "every Bash command is refused" not in prompt


def test_write_prompt_preserves_worker_contract_and_report_request(monkeypatch):
    assert _prompt(monkeypatch, write=True) == claude._WORKER_CONTRACT + "\n\n" + REPORT_REQUEST
