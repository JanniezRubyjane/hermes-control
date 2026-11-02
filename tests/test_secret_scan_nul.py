"""NUL bytes do not exempt binary content from the pre-commit secret scan."""

from scripted_engine import _git, repo as source_repo  # noqa: F401
from test_secret_scan import AWS, _land, _settings, repo  # noqa: F401


def test_added_binary_secret_is_refused(repo):
    before = _git(["rev-parse", "HEAD"], repo)
    (repo / "image.bin").write_bytes(b"\0" + AWS.encode() + b"\n")

    result = _land(repo)

    assert "image.bin:1 (aws-access-key)" in result.unproven_reason
    assert AWS not in result.unproven_reason
    assert result.commit is result.verify is None
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_modified_binary_secret_is_refused(repo):
    image = repo / "image.bin"
    image.write_bytes(b"\0safe\n")
    _git(["add", "image.bin"], repo)
    _git(["commit", "-qm", "base"], repo)
    before = _git(["rev-parse", "HEAD"], repo)
    image.write_bytes(b"\0safe\n" + AWS.encode() + b"\n")

    result = _land(repo)

    assert "image.bin:2 (aws-access-key)" in result.unproven_reason
    assert AWS not in result.unproven_reason
    assert result.commit is result.verify is None
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_binary_without_secrets_commits(repo):
    (repo / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 16)

    assert _land(repo).commit


def test_nul_env_assignment_without_rule_match_commits(repo):
    (repo / "prod.env").write_bytes(b"\0\n" + b"API_TOKEN=q8ZtteR4mVb7Lp2NxW9kYs3D\n")

    assert _land(repo).commit


def test_allow_paths_exempts_binary_secret(repo, monkeypatch):
    _settings(monkeypatch, {"secret_scan": {"allow_paths": ["image.bin"]}})
    (repo / "image.bin").write_bytes(b"\0" + AWS.encode() + b"\n")

    assert _land(repo).commit
