"""Exercise the public exporter against disposable Git histories, never this repo."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_public.sh"
IDENTITY = "JanniezRubyjane <273864440+JanniezRubyjane@users.noreply.github.com>"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "private repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Private Author")
    git(repo, "config", "user.email", "private@example.invalid")
    (repo / "public.txt").write_text("old tree")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Private history")
    (repo / "scripts").mkdir()
    shutil.copyfile(SCRIPT, repo / "scripts/export_public.sh")
    for name in ("docs/dev/STATUS.md", "docs/dev/health/report.md", ".hermes.md",
                 ".worktrees/task/secret.txt"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private diary")
    (repo / "docs/public.md").write_text("public documentation")
    (repo / "public.txt").write_text("HEAD tree")
    git(repo, "add", "-f", ".")
    git(repo, "commit", "-qm", "Another private commit")
    return repo


def export(source, destination):
    return subprocess.run(["/bin/bash", str(source / "scripts/export_public.sh"),
                           str(destination)], cwd=source, capture_output=True, text=True,
                          env={**os.environ, "TZ": "America/Los_Angeles",
                               "GIT_AUTHOR_NAME": "Wrong author",
                               "GIT_AUTHOR_EMAIL": "wrong@example.invalid",
                               "GIT_COMMITTER_NAME": "Wrong committer",
                               "GIT_COMMITTER_EMAIL": "wrong@example.invalid"})


def test_exports_only_head_as_one_commit_without_private_files(source, tmp_path):
    (source / "public.txt").write_text("uncommitted change")
    (source / "untracked.txt").write_text("not in HEAD")
    destination = tmp_path / "public repo"
    result = export(source, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(destination, "rev-list", "--count", "HEAD") == "1"
    assert git(destination, "log", "-1", "--format=%an <%ae>%n%cn <%ce>").splitlines() == [IDENTITY] * 2
    assert git(destination, "remote") == ""
    assert (destination / "public.txt").read_text() == "HEAD tree"
    assert (destination / "docs/public.md").is_file()
    for name in ("docs/dev", ".hermes.md", ".worktrees", "untracked.txt"):
        assert not (destination / name).exists()
    assert git(source, "rev-list", "--count", "HEAD") == "2"


def test_export_dates_use_utc_in_a_non_utc_environment(source, tmp_path):
    destination = tmp_path / "public"
    result = export(source, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    dates = git(destination, "log", "-1", "--format=%ai%n%ci").splitlines()
    assert len(dates) == 2
    assert all(date.endswith("00:00:00 +0000") for date in dates)


def test_excludes_are_anchored_at_root_and_preserve_exact_public_paths(source, tmp_path):
    names = ("examples/docs/dev/guide.md", "examples/.hermes.md",
             "examples/.worktrees/task/example.txt", "docs/development/guide.md",
             ".hermes.md.example", ".worktrees-example/task.txt",
             "examples/space name.md", "examples/line\nbreak.md", "-public.txt")
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public example")
    (source / "examples/link").symlink_to("space name.md")
    git(source, "add", "-f", ".")
    git(source, "commit", "-qm", "Nested public paths")
    destination = tmp_path / "public"
    result = export(source, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in names:
        assert (destination / name).read_text() == "public example"
    assert (destination / "examples/link").is_symlink()
    assert os.readlink(destination / "examples/link") == "space name.md"
    for name in ("docs/dev", ".hermes.md", ".worktrees"):
        assert not (destination / name).exists()


@pytest.mark.parametrize("name", ["existing.txt", ".hidden"])
def test_refuses_nonempty_destination_without_changing_it(source, tmp_path, name):
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / name).write_text("keep me")
    result = export(source, destination)
    assert result.returncode != 0
    assert "non-empty" in result.stderr
    assert sorted(p.name for p in destination.iterdir()) == [name]
    assert (destination / name).read_text() == "keep me"


def test_accepts_an_existing_empty_destination(source, tmp_path):
    destination = tmp_path / "empty"
    destination.mkdir()
    assert export(source, destination).returncode == 0


@pytest.mark.parametrize("marker", ["/" + "Users/", "/" + "home/", "@" + "gmail",
                                    "sk-" + "ant-", "oauth/" + "usage"])
def test_reports_each_forbidden_marker_and_fails(source, tmp_path, marker):
    (source / "leak.txt").write_text("safe\n" + marker + "sensitive\n")
    git(source, "add", "leak.txt")
    git(source, "commit", "-qm", "Fixture leak")
    result = export(source, tmp_path / "export")
    assert result.returncode == 1
    assert "leak.txt:2:" + marker in result.stdout


@pytest.mark.parametrize("marker", ["example-private-marker", "-literal.*[x]\\suffix", "  literal space  "])
def test_reports_private_marker_file_hits_and_allows_them_when_absent(source, tmp_path, marker):
    markers = source / "docs/dev/leak-markers.txt"
    # This local configuration need not be committed to apply to exported HEAD.
    markers.write_text("# ignored comment\n\n   \n" + marker + "\n")
    (source / "leak.txt").write_text("safe\n" + marker.upper() + "\n")
    git(source, "add", "leak.txt")
    git(source, "commit", "-qm", "Private marker fixture")
    result = export(source, tmp_path / "blocked")
    assert result.returncode == 1
    assert "leak.txt:2:" + marker.upper() in result.stdout
    assert not (tmp_path / "blocked/docs/dev").exists()
    markers.unlink()
    result = export(source, tmp_path / "allowed")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("contents", ["", "# ignored comment\n\n  \n"])
def test_empty_private_marker_file_adds_no_patterns(source, tmp_path, contents):
    (source / "docs/dev/leak-markers.txt").write_text(contents)
    result = export(source, tmp_path / "export")
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_private_marker_file_reports_generic_scan(source, tmp_path):
    result = export(source, tmp_path / "export")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "export_public: no private marker file; generic scan only\n" in result.stderr


def test_crlf_private_marker_matches_case_insensitively(source, tmp_path):
    (source / "docs/dev/leak-markers.txt").write_bytes(b"private-crlf-marker\r\n")
    (source / "leak.txt").write_text("PRIVATE-CRLF-MARKER\n")
    git(source, "add", "leak.txt")
    git(source, "commit", "-qm", "CRLF marker fixture")
    result = export(source, tmp_path / "export")
    assert result.returncode == 1
    assert "leak.txt:1:PRIVATE-CRLF-MARKER" in result.stdout


def test_indented_private_marker_comment_is_ignored(source, tmp_path):
    comment = " \t# ignored private comment"
    (source / "docs/dev/leak-markers.txt").write_text(comment + "\n")
    (source / "public.txt").write_text(comment + "\n")
    git(source, "add", "public.txt")
    git(source, "commit", "-qm", "Indented comment fixture")
    result = export(source, tmp_path / "export")
    assert result.returncode == 0, result.stdout + result.stderr


def test_thai_bytes_in_binary_file_are_allowed(source, tmp_path):
    (source / "binary.dat").write_bytes(b"\0" + chr(0x0E01).encode("utf-8"))
    git(source, "add", "binary.dat")
    git(source, "commit", "-qm", "Binary script bytes fixture")
    result = export(source, tmp_path / "export")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("codepoint", [0x0E00, 0x0E01, 0x0E3F, 0x0E40, 0x0E7F])
@pytest.mark.parametrize("generic_hit", [False, True])
def test_reports_thai_script_and_generic_hits(source, tmp_path, codepoint, generic_hit):
    character = chr(codepoint)
    marker = "@" + "gmail"
    (source / "leak.txt").write_text("safe\n" + character + "\n"
                                     + (marker + "\n" if generic_hit else ""))
    git(source, "add", "leak.txt")
    git(source, "commit", "-qm", "Script character fixture")
    result = export(source, tmp_path / "export")
    assert result.returncode == 1
    assert "leak.txt:2:" + character in result.stdout
    if generic_hit:
        assert "leak.txt:3:" + marker in result.stdout


def test_characters_outside_script_range_are_allowed(source, tmp_path):
    (source / "public.txt").write_text(chr(0x0DFF) + chr(0x0E80))
    git(source, "add", "public.txt")
    git(source, "commit", "-qm", "Other characters")
    result = export(source, tmp_path / "export")
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_exact_fake_key_file_and_line_are_allowed(source, tmp_path):
    path = source / "tests/test_planner_status.py"
    path.parent.mkdir()
    fake = 'SECRET = "sk-' + 'ant-oat01-must-never-leave"'
    path.write_text("\n" * 6 + fake + "\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Allowed fixture")
    assert export(source, tmp_path / "allowed").returncode == 0
    path.write_text("\n" * 7 + fake + "\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Wrong fixture line")
    result = export(source, tmp_path / "wrong-line")
    assert result.returncode == 1
    assert "tests/test_planner_status.py:8:" in result.stdout
    other = source / "other.py"
    other.write_text("\n" * 6 + fake + "\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Wrong fixture file")
    result = export(source, tmp_path / "wrong-file")
    assert result.returncode == 1
    assert "other.py:7:" in result.stdout


def test_refuses_a_destination_symlink(source, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert export(source, link).returncode != 0
    assert list(target.iterdir()) == []


def test_export_keeps_raw_head_even_with_archive_attributes(source, tmp_path):
    (source / ".gitattributes").write_text("public.txt export-ignore\ndocs/public.md export-subst\n")
    (source / "docs/public.md").write_text("$Format:%an <%ae>$\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Archive attributes")
    destination = tmp_path / "public"
    result = export(source, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "public.txt").read_text() == "HEAD tree"
    assert (destination / "docs/public.md").read_text() == "$Format:%an <%ae>$\n"


def test_scanner_checks_binary_contents_and_reports_all_hits(source, tmp_path):
    markers = ["/" + "Users/", "/" + "home/", "@" + "gmail",
               "sk-" + "ant-", "oauth/" + "usage"]
    (source / "binary.dat").write_bytes(b"\0\n" + "\n".join(markers).encode())
    git(source, "add", ".")
    git(source, "commit", "-qm", "Binary fixture")
    result = export(source, tmp_path / "binary-export")
    assert result.returncode == 1
    for line, marker in enumerate(markers, 2):
        assert f"binary.dat:{line}:{marker}" in result.stdout
