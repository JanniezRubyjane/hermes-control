"""Public community entry points and the standalone CI contract."""

from pathlib import Path
import re
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
YAML_FILES = (
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/checks.yml",
)
COMMUNITY_FILES = (
    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "SUPPORT.md", "THIRD_PARTY.md",
    ".github/PULL_REQUEST_TEMPLATE.md", *YAML_FILES,
)
CI_COMMAND = (
    "python -m pytest --noconftest -q -p no:cacheprovider "
    "tests/test_export_public.py tests/test_flow_diagram.py"
)


@pytest.mark.parametrize("name", COMMUNITY_FILES)
def test_community_file_exists(name):
    assert (ROOT / name).is_file()


@pytest.mark.parametrize("name", YAML_FILES)
def test_community_yaml_parses(name):
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load((ROOT / name).read_text())
    assert isinstance(parsed, dict)
    if name.endswith("checks.yml"):
        steps = parsed["jobs"]["checks"]["steps"]
        assert CI_COMMAND in [step.get("run") for step in steps]
    elif name.endswith("config.yml"):
        assert any(link["url"].endswith("/SECURITY.md")
                   for link in parsed["contact_links"])
    else:
        assert parsed["name"] and parsed["description"]
        body = parsed["body"]
        labels = {item["attributes"]["label"] for item in body}
        assert {"Hermes Agent version", "claude version", "codex version", "OS",
                "Steps to reproduce", "Expected behavior", "Actual behavior"} <= labels
        options = [option for item in body if item["type"] == "checkboxes"
                   for option in item["attributes"]["options"]]
        assert {"label": "I removed secrets and private paths from logs", "required": True} in options


def test_workflow_runs_standalone_tests():
    assert CI_COMMAND in (ROOT / ".github/workflows/checks.yml").read_text()


def test_code_of_conduct_has_no_email_address():
    text = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text) is None
    assert "version 2.1" in text
    assert "[@JanniezRubyjane](https://github.com/JanniezRubyjane)" in text


def tracked_public_files():
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout
    for raw_name in tracked.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="surrogateescape")
        if (name.startswith(("docs/dev/", ".worktrees/"))
                or name in (".hermes.md", "scripts/pstack_cite_check.py")):
            continue
        yield name, (ROOT / name).read_bytes()


def test_tracked_public_files_have_no_private_markers():
    marker_file = ROOT / "docs/dev/leak-markers.txt"
    if not marker_file.exists():
        pytest.skip("public export omits private marker configuration")
    lines = [line.removesuffix(b"\r") for line in marker_file.read_bytes().split(b"\n")]
    markers = [line.lower() for line in lines
               if line.strip() and not line.lstrip().startswith(b"#")]
    leaks = [name for name, content in tracked_public_files()
             if any(marker in content.lower() for marker in markers)]
    assert not leaks, leaks


def test_tracked_public_files_have_no_thai_script():
    leaks = []
    for name, content in tracked_public_files():
        if b"\0" in content:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if any(0x0E00 <= ord(character) <= 0x0E7F for character in text):
            leaks.append(name)
    assert not leaks, leaks


@pytest.mark.parametrize("content, leaks", [
    (b"\0" + chr(0x0E01).encode("utf-8"), []),
    (b"\xff" + chr(0x0E01).encode("utf-8"), []),
    (chr(0x0E01).encode("utf-8"), ["fixture.txt"]),
])
def test_thai_scan_distinguishes_binary_and_text(monkeypatch, content, leaks):
    monkeypatch.setitem(globals(), "tracked_public_files", lambda: [("fixture.txt", content)])
    if leaks:
        with pytest.raises(AssertionError, match="fixture.txt"):
            test_tracked_public_files_have_no_thai_script()
    else:
        test_tracked_public_files_have_no_thai_script()


def test_tracked_public_pngs_have_only_approved_chunks():
    allowed = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"sRGB", b"gAMA",
               b"cHRM", b"pHYs", b"tRNS", b"bKGD"}
    for name, content in tracked_public_files():
        if not name.endswith(".png"):
            continue
        assert content.startswith(b"\x89PNG\r\n\x1a\n"), name
        offset = 8
        chunks = []
        while offset < len(content):
            assert offset + 12 <= len(content), name
            length, kind = struct.unpack_from(">I4s", content, offset)
            assert kind in allowed, (name, kind)
            offset += 12 + length
            assert offset <= len(content), name
            chunks.append(kind)
        assert chunks[0] == b"IHDR" and chunks[-1] == b"IEND", name


def test_license_metadata_agrees_on_mit():
    import yaml

    plugin = yaml.safe_load((ROOT / "hermes_control/plugin.yaml").read_text())
    frontmatter = (ROOT / "skills/hermes-control/SKILL.md").read_text().split("---", 2)[1]
    skill = yaml.safe_load(frontmatter)
    assert plugin["license"] == skill["license"] == "MIT"
    assert (ROOT / "LICENSE").read_text().splitlines()[0] == "MIT License"


def test_code_of_conduct_license_attribution():
    text = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert ("Contributor Covenant 2.1 is licensed under "
            "[Creative Commons Attribution 4.0 International]"
            "(https://creativecommons.org/licenses/by/4.0/). "
            "This copy changes only the enforcement contact and Markdown formatting.") in text


def test_third_party_notices():
    text = (ROOT / "THIRD_PARTY.md").read_text()
    assert text.startswith("# Third-party notices\n")
    for notice in ("Copyright (c) 2025 Nous Research", "ISC", "CC BY 4.0"):
        assert notice in text


def test_readme_public_release_entry_points():
    text = (ROOT / "README.md").read_text()
    for heading in ("Status", "Support and contributing", "License"):
        assert f"## {heading}\n" in text
    assert "not affiliated with or endorsed by" in text
    for name in ("THIRD_PARTY.md", "SUPPORT.md", "SECURITY.md", "CONTRIBUTING.md",
                 "CODE_OF_CONDUCT.md", "CHANGELOG.md"):
        assert f"]({name})" in text


def test_readme_internal_anchors_resolve():
    text = (ROOT / "README.md").read_text()
    headings = re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)
    anchors = {re.sub(r"[^\w\s-]", "", heading.lower()).replace("_", "").replace(" ", "-")
               for heading in headings}
    links = re.findall(r"\]\(#([^)]*)\)", text)
    assert links
    assert not set(links) - anchors
