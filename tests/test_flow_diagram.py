"""The published flow diagram stays English, portable, and reproducible."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/build_flow.py"
DIAGRAM = ROOT / "docs/hermes-control-flow.html"


@pytest.mark.parametrize("path", [GENERATOR, DIAGRAM] if GENERATOR.exists() else [DIAGRAM],
                         ids=["generator", "html"] if GENERATOR.exists() else ["html"])
def test_diagram_has_only_english_text(path):
    assert re.search(r"[\u0e00-\u0e7f]", path.read_text(encoding="utf-8")) is None


def test_public_english_html():
    diagram = DIAGRAM.read_text(encoding="utf-8")
    assert '<html lang="en">' in diagram
    # Split literals so scripts/export_public.sh's leak scan does not flag this test.
    for forbidden in ("/Us" + "ers/", "docs/dev", "oauth" + "/usage"):
        assert forbidden not in diagram


def test_current_engine_rules():
    diagram = DIAGRAM.read_text(encoding="utf-8")
    for rule in (
        "claude = --restricted + allow/deny",
        "refused if repo commits .codex config",
        "Claude usage comes only from rate_limit_event",
        "claude auth status",
        "CLI aliases + Hermes built-in list",
        "no account is contacted",
    ):
        assert rule in diagram
    assert "when CLI supports" not in diagram


@pytest.mark.skipif(not GENERATOR.exists(), reason="diagram generator is unavailable")
def test_generator_reproduces_committed_html(tmp_path):
    copied = tmp_path / "scripts/build_flow.py"
    copied.parent.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    shutil.copyfile(GENERATOR, copied)
    subprocess.run([sys.executable, str(copied)], cwd=tmp_path, check=True,
                   capture_output=True, text=True)
    assert (tmp_path / "docs/hermes-control-flow.html").read_bytes() == DIAGRAM.read_bytes()


def test_resource_card_gives_guidance_without_measured_usage():
    diagram = DIAGRAM.read_text(encoding="utf-8")
    title, items = re.search(
        r'<div class="card-dot amber"></div><h3>(.*?)</h3></div><ul>(.*?)</ul>',
        diagram,
    ).groups()
    assert title == "Tokens · time"
    assert not re.search(r"[0-9$]", items)
