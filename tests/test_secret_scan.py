"""The controller refuses secrets before creating a task commit."""

import json
from pathlib import Path

import pytest
from scripted_engine import ScriptedEngine, _engines, _git, _reading, repo as source_repo  # noqa: F401

from hermes_control import landing, secret_scan
from hermes_control.dispatch import dispatch, resume_paused
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.runlog import RunLog
from hermes_control.worktree import TaskWorktree


AWS = "AKIA" + "7Q" * 8
TASK = {"id": "a", "goal": "add a line", "engine": "claude", "why": "w", "policy": "write"}


def _settings(monkeypatch, value):
    import hermes_cli.config

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": value}}}})


@pytest.fixture
def repo(source_repo):
    task = source_repo / "scan-task"
    _git(["worktree", "add", "-b", "scan-task", str(task), "HEAD"], source_repo)
    return task


def _worktree(repo):
    root = Path(_git(["rev-parse", "--git-common-dir"], repo)).parent
    return TaskWorktree(repo, _git(["branch", "--show-current"], repo), root,
                        _git(["rev-parse", "HEAD"], repo))


def _land(repo):
    task = ControlTask(**TASK)
    result = ControlResult(task_id="a", engine="claude", ok=True, text="done", diff="changed",
                           session_id=None)
    return landing.land(task, result, _worktree(repo), run_id="scan", sandbox=lambda: None)


@pytest.mark.parametrize("path,value,rule", [
    ("a.txt", "-----BEGIN " + "RSA PRIVATE KEY-----", "private-key"),
    ("a.txt", AWS, "aws-access-key"),
    ("a.txt", "ASIA" + "5Z" * 8, "aws-access-key"),
    ("a.txt", "ghp_" + "A7" * 18, "github-token"),
    ("a.txt", "github_pat_" + "A7_" * 17, "github-token"),
    ("a.txt", "xoxb-" + "A7-" * 4, "slack-token"),
    ("a.txt", "AIza" + "A7_" * 11 + "AZ", "google-api-key"),
    ("a.txt", "sk-" + "ant-" + "A7_" * 12, "openai-or-anthropic-key"),
    ("a.txt", "sk-proj-" + "A7_" * 12, "openai-or-anthropic-key"),
    ("a.txt", "sk-" + "A7_" * 12, "openai-or-anthropic-key"),
    ("a.txt", "sk_live_" + "A7" * 8, "stripe-key"),
    ("a.txt", "rk_live_" + "A7" * 8, "stripe-key"),
    ("config.env", "SERVICE_TOKEN=" + "A7bc" * 5, "env-assignment"),
    ("setup.sh", "export API_KEY='" + "A7bc" * 5 + "'", "env-assignment"),
    (".env", "TOKEN=" + "A7bc" * 5, "env-assignment"),
    ("cert.pem", "ordinary", "secret-file"),
])
def test_rule_blocks_before_commit(repo, path, value, rule):
    before = _git(["rev-parse", "HEAD"], repo)
    (repo / path).write_text("# header\n" + value + "\n")

    result = _land(repo)

    line = 1 if rule == "secret-file" else 2 + value.count("\n")
    assert f"{path}:{line} ({rule})" in result.unproven_reason
    assert value not in result.unproven_reason
    assert result.commit is result.verify is None
    assert result.events[0].kind == "commit"
    assert result.events[0].payload == {"sha": None, "reason": result.unproven_reason}
    assert _git(["rev-parse", "HEAD"], repo) == before
    assert (repo / path).read_text() == "# header\n" + value + "\n"


@pytest.mark.parametrize("name", ["a.pem", "a.p12", "a.pfx", "id_rsa",
                                  "id_ed25519", "id_ecdsa.backup"])
def test_new_secret_filenames(repo, name):
    (repo / name).write_text("ordinary\n")
    assert f"{name}:1 (secret-file)" in _land(repo).unproven_reason


@pytest.mark.parametrize("path,text", [
    (".env.example", "TOKEN=" + "example" * 5),
    (".env.sample", "TOKEN=" + "dummy" * 5),
    (".env.template", "TOKEN=" + "placeholder" * 3),
    ("a.css", "mask-image-position: left; sk-image-color-pos"),
    ("a.txt", "sk-" + "letters_only" * 4),
    ("setup.sh", "TOKEN=short\nAPI_KEY=12345"),
    ("setup.sh", "\n".join("TOKEN=" + value for value in [
        "<" + "A7" * 20 + ">", "${LONG_VARIABLE_NAME}", "$(long_command_name)",
        "$LONG_VARIABLE_NAME", "xxx" * 10, "changeme" * 4, "example" * 4,
        "dummy" * 6, "fake" * 8, "placeholder" * 3, "test" * 8])),
    ("notes.txt", "TOKEN=" + "A7bc" * 5),
])
def test_safe_changes_commit(repo, path, text):
    (repo / path).write_text(text + "\n")
    assert _land(repo).commit


def test_allow_paths_exempts_fixture(repo, monkeypatch):
    _settings(monkeypatch, {"secret_scan": {"allow_paths": ["fixtures/**"]}})
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "demo.key").write_text(AWS)
    assert _land(repo).commit


def test_disabled_scan_does_not_inspect(repo, monkeypatch):
    _settings(monkeypatch, {"secret_scan": {"enabled": False}})
    (repo / "a.txt").write_text(AWS)
    assert _land(repo).commit


@pytest.mark.parametrize("value", [False, "", [], {"secret_scan": False},
                                  {"secret_scan": ""}, {"secret_scan": []},
                                  {"secret_scan": {"enabled": 0}},
                                  {"secret_scan": {"enabled": "false"}},
                                  {"secret_scan": {"allow_paths": "*"}},
                                  {"secret_scan": {"allow_paths": [False]}}])
def test_invalid_settings_warn_and_keep_scanning(repo, monkeypatch, caplog, value):
    _settings(monkeypatch, value)
    (repo / "a.txt").write_text(AWS)
    assert _land(repo).commit is None
    assert "secret scan" in caplog.text.lower()
    assert AWS not in caplog.text


def test_only_added_lines_are_scanned_and_line_numbers_are_current(repo):
    (repo / "a.txt").write_text(AWS + "\nold\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "base"], repo)
    (repo / "a.txt").write_text(AWS + "\nsafe\n")
    assert _land(repo).commit
    (repo / "a.txt").write_text("safe\nsafe\n" + AWS + "\n")
    assert "a.txt:3 (aws-access-key)" in _land(repo).unproven_reason


def test_staging_matches_commit_including_untracked_ignored_and_excluded(repo):
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text(AWS)
    (repo / "cache.pyc").write_text(AWS)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.txt").write_text(AWS)
    (repo / "safe.txt").write_text("safe")
    assert _land(repo).commit
    assert not _git(["ls-files", "ignored.txt", "cache.pyc", "__pycache__"], repo)
    (repo / "new.txt").write_text(AWS)
    assert "new.txt:1 (aws-access-key)" in _land(repo).unproven_reason


def test_already_staged_excluded_content_is_scanned(repo):
    (repo / "cache.pyc").write_text(AWS)
    _git(["add", "cache.pyc"], repo)
    assert "cache.pyc:1 (aws-access-key)" in _land(repo).unproven_reason


def test_working_content_replaces_staged_content(repo):
    (repo / "a.txt").write_text(AWS)
    _git(["add", "a.txt"], repo)
    (repo / "a.txt").write_text("safe\n")
    assert _land(repo).commit


def test_rejection_unstages_changes_without_making_secret_reachable(repo, caplog):
    (repo / "a.txt").write_text(AWS)
    (repo / "new.txt").write_text(AWS)
    _git(["add", "."], repo)
    result = _land(repo)
    assert result.commit is None
    assert not _git(["diff", "--cached", "--name-only"], repo)
    assert (repo / "a.txt").read_text() == AWS
    assert (repo / "new.txt").read_text() == AWS
    assert AWS not in _git(["log", "--all", "-p"], repo)
    assert AWS not in repr(result) + caplog.text


def test_split_index_is_supported(repo):
    (repo / "safe.txt").write_text("safe\n")
    _git(["add", "safe.txt"], repo)
    _git(["update-index", "--split-index"], repo)
    (repo / "new.txt").write_text(AWS)
    assert "new.txt:1 (aws-access-key)" in _land(repo).unproven_reason


def test_symlink_content_is_scanned_without_following_target(repo, tmp_path_factory):
    target = tmp_path_factory.mktemp("outside")
    secret = target / "secret"
    secret.write_text(AWS)
    (repo / "link").symlink_to(secret)
    assert _land(repo).commit


def test_real_secret_in_env_example_is_still_blocked(repo):
    (repo / ".env.example").write_text("TOKEN=" + "A7bc" * 5)
    assert ".env.example:1 (env-assignment)" in _land(repo).unproven_reason


def test_extensionless_shell_script_is_scanned(repo):
    (repo / "start").write_text("#!/bin/sh\nPASSWORD=" + "A7bc" * 5)
    assert "start:2 (env-assignment)" in _land(repo).unproven_reason


@pytest.mark.parametrize("result_values", [{"unproven_reason": "already refused"},
                                           {"ok": False}, {"diff": ""}])
def test_early_returns_do_not_run_scanner(repo, monkeypatch, result_values):
    def unexpected(*args):
        pytest.fail("scanner called before early return")

    monkeypatch.setattr(secret_scan, "scan", unexpected)
    values = {"task_id": "a", "engine": "claude", "ok": True, "text": "done",
              "session_id": None, "diff": "changed", **result_values}
    result = landing.land(ControlTask(**TASK), ControlResult(**values), _worktree(repo),
                          run_id="early", sandbox=lambda: None)
    assert result.commit is None and not result.events


def test_content_after_cap_is_skipped(repo):
    (repo / "large.txt").write_text("a\n" * (secret_scan.MAX_FILE_BYTES // 2) + AWS)
    assert _land(repo).commit


def test_first_bytes_of_large_file_are_scanned(repo):
    (repo / "large.txt").write_text(AWS + "\n" + "a" * secret_scan.MAX_FILE_BYTES)
    assert "large.txt:1 (aws-access-key)" in _land(repo).unproven_reason


def test_at_most_five_hits_are_reported(repo):
    (repo / "a.txt").write_text((AWS + "\n") * 8)
    reason = _land(repo).unproven_reason
    assert reason.count("(aws-access-key)") == 5
    assert "+3 more" in reason
    assert reason.endswith("; the change is left in the task worktree; remove the secret, "
                           "or list the path in settings.secret_scan.allow_paths if it is a test fixture")


def test_exception_fails_closed_without_exception_text(repo, monkeypatch, caplog):
    (repo / "a.txt").write_text("safe\n")

    def broken(*args):
        raise ValueError(AWS)

    monkeypatch.setattr(secret_scan, "scan", broken)
    result = _land(repo)
    assert result.commit is None
    assert result.unproven_reason == "not committed: the secret scan could not run (ValueError)"
    assert AWS not in repr(result) + caplog.text


class SecretEngine(ScriptedEngine):
    def build_argv(self, task, *, cwd):
        argv = super().build_argv(task, cwd=cwd)
        argv[-1] = f"open('leak.txt', 'w').write({AWS!r});" + argv[-1]
        return argv


def _assert_blocked(entry, repo, run_id):
    assert not entry["commit"]
    assert "leak.txt:1 (aws-access-key)" in entry["unproven_reason"]
    worktree = Path(entry["worktree"])
    assert (worktree / "leak.txt").read_text() == AWS
    assert _git(["rev-parse", "HEAD"], worktree) == _git(["rev-parse", "HEAD"], repo)
    assert AWS not in _git(["log", "--all", "-p"], repo)
    log = RunLog(run_id)
    assert AWS not in log.path.read_text()
    assert AWS not in json.dumps(entry)
    summary = [event.payload for event in log.read_all() if event.kind == "summary"]
    assert summary and AWS not in json.dumps(summary)
    assert any(e.kind == "commit" and e.payload["sha"] is None for e in log.read_all())


def test_dispatch_blocks_and_does_not_log_secret(repo, monkeypatch):
    _engines(monkeypatch, SecretEngine("claude", "ok"), ScriptedEngine("codex"))
    [entry] = dispatch([TASK], parent_cwd=str(repo), run_id="secret")["tasks"]
    _assert_blocked(entry, repo, "secret")


def test_resume_paused_also_blocks(repo, monkeypatch):
    _engines(monkeypatch, SecretEngine("claude", "limit", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    [paused] = dispatch([TASK], parent_cwd=str(repo), run_id="resume-secret")["tasks"]
    assert paused["paused"]
    entry = resume_paused(paused["paused"]["id"])
    _assert_blocked(entry, repo, "resume-secret")


def test_settings_documentation_and_planner_text_agree():
    from hermes_control import build_description

    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills/hermes-control/SKILL.md").read_text()
    for text in (build_description(), skill.split("## Results", 1)[1]):
        assert "looks like it contains a secret" in text
        assert "path:line" in text and "rule" in text
        assert "worktree keeps the change" in text
    assert "secret_scan" in (root / "README.md").read_text()


@pytest.mark.parametrize("attribute", ["-diff", "binary"])
def test_attributes_cannot_hide_added_secret_lines(repo, attribute):
    (repo / "a.txt").write_text("safe\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "base"], repo)
    (repo / ".gitattributes").write_text(f"a.txt {attribute}\n")
    (repo / "a.txt").write_text("safe\n" + AWS + "\n")
    assert "a.txt:2 (aws-access-key)" in _land(repo).unproven_reason


@pytest.mark.parametrize("name", [".env", ".env.test", ".env.development", "talk.key",
                                  "id_rsa.pub", "id_ed25519.pub", "id_ecdsa.pub",
                                  "public.pem.pub", "public.pfx.pub"])
def test_safe_env_public_keys_and_presentations_commit(repo, name):
    (repo / name).write_text("TOKEN=${LONG_VARIABLE_NAME}\n")
    assert _land(repo).commit


@pytest.mark.parametrize("name", [".env", ".env.test", ".env.development", "talk.key",
                                  "id_rsa.pub"])
def test_filename_exemptions_still_scan_content(repo, name):
    (repo / name).write_text(AWS)
    assert f"{name}:1 (aws-access-key)" in _land(repo).unproven_reason


@pytest.mark.parametrize("name", ["TOKENIZER", "MYTOKEN", "SECRETARY", "PASSWORDLESS",
                                  "API_KEYSTONE", "PRIVATE_KEYRING", "TOKEN_PATH",
                                  "TOKEN_FILE", "TOKEN_NAME", "TOKEN_DIR", "TOKEN_URL",
                                  "TOKEN_ID"])
def test_ordinary_env_names_are_not_secrets(repo, name):
    (repo / "setup.sh").write_text(name + "=" + "A7bc" * 5)
    assert _land(repo).commit


@pytest.mark.parametrize("value", ["/some/long/credential/path", "~/long/credential/path",
                                   "https://service.example/api", "git+ssh://host/resource"])
def test_env_paths_and_urls_are_not_secrets(repo, value):
    (repo / "setup.sh").write_text("TOKEN=" + value)
    assert _land(repo).commit


@pytest.mark.parametrize("name", ["TOKEN", "MY_TOKEN", "TOKEN_FOR_SERVICE", "API_KEY",
                                  "SERVICE_API_KEY", "PRIVATE_KEY", "DB_PASSWORD",
                                  "SERVICE_SECRET", "SERVICE_PASSWD"])
def test_secret_name_components_are_scanned(repo, name):
    (repo / "setup.sh").write_text(name + "=" + "A7bc" * 5)
    assert "setup.sh:1 (env-assignment)" in _land(repo).unproven_reason


def test_scan_runs_after_staging_under_commit_lock_without_restaging(repo, monkeypatch):
    from contextlib import contextmanager
    from hermes_control import worktree

    locked = False
    original_lock = worktree._repository_lock
    original_scan = secret_scan.scan

    @contextmanager
    def lock(path):
        nonlocal locked
        with original_lock(path):
            locked = True
            try:
                yield
            finally:
                locked = False

    def scan(tree, env):
        assert locked, "scan must run inside the commit lock"
        assert not {"GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES"}.intersection(env)
        assert _git(["show", ":a.txt"], repo) == "scanned safe content"
        reason = original_scan(tree, env)
        # An edit after the scan must stay uncommitted, even for an excluded path.
        (repo / "a.txt").write_text(AWS)
        (repo / "cache.pyc").write_text(AWS)
        return reason

    monkeypatch.setattr(worktree, "_repository_lock", lock)
    monkeypatch.setattr(secret_scan, "scan", scan)
    (repo / "a.txt").write_text("scanned safe content\n")
    result = _land(repo)
    assert result.commit
    assert _git(["show", "HEAD:a.txt"], repo) == "scanned safe content"
    assert "cache.pyc" not in _git(["ls-tree", "--name-only", "HEAD"], repo)
    assert (repo / "a.txt").read_text() == AWS


def test_type_change_patch_headers_are_not_added_lines(repo):
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env

    (repo / "link").write_text("old\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "base"], repo)
    (repo / "link").unlink()
    (repo / "link").symlink_to("safe-target")
    _git(["add", "."], repo)
    env = noninteractive_repo_git_env(str(repo))
    assert list(secret_scan._added_lines(_worktree(repo), env, "link", 10)) == [1]
    assert _land(repo).commit


@pytest.mark.parametrize("existing", [False, True])
def test_nul_after_git_binary_probe_does_not_hide_secrets(repo, existing):
    if existing:
        (repo / "a.txt").write_text("old\n")
        _git(["add", "."], repo)
        _git(["commit", "-qm", "base"], repo)
    (repo / "a.txt").write_bytes(AWS.encode() + b"\n" + b"a" * 8000 + b"\0")
    assert "a.txt:1 (aws-access-key)" in _land(repo).unproven_reason


@pytest.mark.parametrize("word", ["test", "example", "dummy", "fake", "placeholder", "xxx",
                                  "changeme"])
def test_placeholder_substrings_do_not_hide_real_values(repo, word):
    (repo / "setup.sh").write_text("TOKEN=A7bc" + word + "Z9xy" * 5)
    assert "setup.sh:1 (env-assignment)" in _land(repo).unproven_reason


@pytest.mark.parametrize("value,rule", [
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "private-key"),
    ("sk-svcacct-" + "A7_" * 12, "openai-or-anthropic-key"),
])
def test_additional_private_key_formats(repo, value, rule):
    (repo / "a.txt").write_text(value)
    assert f"a.txt:1 ({rule})" in _land(repo).unproven_reason


@pytest.mark.parametrize("path,prefix", [("a.txt", ""), (".env.test", "AWS_ACCESS_KEY_ID="),
                                        ("setup.sh", "TOKEN=")])
def test_documented_aws_example_key_is_safe(repo, path, prefix):
    (repo / path).write_text(prefix + "AKIAIOSFODNN7EXAMPLE\n")
    assert _land(repo).commit


def test_unknown_settings_warn_without_values(repo, monkeypatch, caplog):
    _settings(monkeypatch, {"secret_scan": {"allow_path": [AWS]}})
    (repo / "a.txt").write_text(AWS)
    assert _land(repo).commit is None
    assert "unknown" in caplog.text.lower() and "secret_scan" in caplog.text
    assert AWS not in caplog.text


def test_scan_failure_logs_only_type_and_module_line(repo, monkeypatch, caplog):
    (repo / "a.txt").write_text("safe\n")
    _git(["add", "."], repo)

    def broken(*args, **kwargs):
        raise ValueError(AWS)

    monkeypatch.setattr(secret_scan, "scan", broken)
    result = _land(repo)
    assert not result.commit and "(ValueError)" in result.unproven_reason
    assert "ValueError" in caplog.text
    assert f"test_secret_scan:{broken.__code__.co_firstlineno + 1}" in caplog.text
    assert AWS not in caplog.text + repr(result)
    assert not _git(["diff", "--cached", "--name-only"], repo)
    assert (repo / "a.txt").read_text() == "safe\n"
    assert all(record.exc_info is None for record in caplog.records)


def test_blob_reader_has_no_stdin(repo, monkeypatch):
    import subprocess

    (repo / "a.txt").write_text("safe\n")
    original = subprocess.Popen

    def popen(argv, *args, **kwargs):
        if "cat-file" in argv:
            assert kwargs.get("stdin") is subprocess.DEVNULL
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert _land(repo).commit


def test_allow_paths_fnmatch_semantics_are_documented():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "fnmatch" in readme and "`*` also matches `/`" in readme


@pytest.mark.parametrize("configured", [False, True])
def test_successful_guarded_commit_keeps_split_index_readable(repo, configured):
    if configured:
        _git(["config", "core.splitIndex", "true"], repo)
    (repo / "a.txt").write_text("safe\n")
    _git(["add", "."], repo)
    _git(["update-index", "--split-index"], repo)
    assert _land(repo).commit
    assert not _git(["status", "--porcelain", "--untracked-files=no"], repo)
    (repo / "a.txt").write_text("another safe change\n")
    assert _land(repo).commit
    assert not _git(["status", "--porcelain", "--untracked-files=no"], repo)


@pytest.mark.parametrize("operation", ["add", "commit"])
def test_guarded_git_errors_keep_commit_failure_detail(repo, monkeypatch, operation):
    from hermes_control import worktree

    (repo / "a.txt").write_text("safe\n")
    original = worktree._run_git

    def broken(args, *positional, **kwargs):
        if operation in args:
            raise OSError("git unavailable")
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(worktree, "_run_git", broken)
    result = _land(repo)
    assert not result.commit
    assert result.unproven_reason == "not committed: commit failed: git unavailable"


def test_successful_commit_needs_no_index_copy(repo, monkeypatch):
    import shutil

    original = shutil.copyfile
    index = Path(_git(["rev-parse", "--git-path", "index"], repo))

    def copy(source, destination, *args, **kwargs):
        if Path(destination) == index:
            raise OSError("index replacement failed")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", copy)
    (repo / "a.txt").write_text("safe\n")
    result = _land(repo)
    assert result.commit == _git(["rev-parse", "HEAD"], repo)
    assert result.events[0].payload["sha"] == result.commit


def test_concurrently_written_readonly_object_does_not_break_commit(repo, monkeypatch):
    original = secret_scan.scan

    def scan(tree, env):
        reason = original(tree, env)
        # Another Git process can write the same object while the scan runs.
        sha = _git(["hash-object", "-w", "a.txt"], repo)
        blob = Path(_git(["rev-parse", "--git-path", "objects"], repo)) / sha[:2] / sha[2:]
        blob.chmod(0o444)
        return reason

    monkeypatch.setattr(secret_scan, "scan", scan)
    (repo / "a.txt").write_text("safe\n")
    assert _land(repo).commit


def test_guarded_commit_preserves_shared_object_directory_mode(repo):
    import stat

    objects = Path(_git(["rev-parse", "--git-path", "objects"], repo))
    objects.chmod(0o2775)
    mode = stat.S_IMODE(objects.stat().st_mode)
    (repo / "a.txt").write_text("safe\n")
    assert _land(repo).commit
    assert stat.S_IMODE(objects.stat().st_mode) == mode


def test_guarded_commit_respects_real_index_lock(repo, monkeypatch):
    original = secret_scan.scan
    index_lock = Path(_git(["rev-parse", "--git-path", "index.lock"], repo))

    def scan(tree, env):
        reason = original(tree, env)
        index_lock.write_text("another git process")
        return reason

    monkeypatch.setattr(secret_scan, "scan", scan)
    (repo / "a.txt").write_text("safe\n")
    before = _git(["rev-parse", "HEAD"], repo)
    try:
        result = _land(repo)
        assert result.commit is None
        assert "git commit failed:" in result.unproven_reason
        assert _git(["rev-parse", "HEAD"], repo) == before
        assert index_lock.read_text() == "another git process"
    finally:
        index_lock.unlink()


@pytest.mark.parametrize("value", ["/" + "A7bc" * 5, "/A7bc/" + "Z9xy" * 5 + "=",
                                   "/A7bc/" + "Z9xy" * 5 + "+"])
def test_base64_env_secrets_starting_with_slash_are_blocked(repo, value):
    (repo / "setup.sh").write_text("TOKEN=" + value)
    assert "setup.sh:1 (env-assignment)" in _land(repo).unproven_reason


@pytest.mark.parametrize("suffix", ["", "A7bc", "/" + "Z9xy" * 5])
def test_aws_documented_example_secret_is_exempt_as_whole_value(repo, suffix):
    example = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    (repo / "setup.sh").write_text("AWS_SECRET_ACCESS_KEY=" + example + suffix)
    result = _land(repo)
    if suffix:
        assert "setup.sh:1 (env-assignment)" in result.unproven_reason
    else:
        assert result.commit


@pytest.mark.parametrize("value", ["<" + "A7" * 20 + ">", "${LONG_VARIABLE_NAME}",
                                   "$(long_command_name)", "$LONG_VARIABLE_NAME"])
def test_placeholder_patterns_only_cover_supported_value_characters(value):
    assert not secret_scan._VALUE.fullmatch(value)
    assert not secret_scan._PLACEHOLDER.fullmatch(value)
    assert not secret_scan._env_secret("TOKEN=" + value)
