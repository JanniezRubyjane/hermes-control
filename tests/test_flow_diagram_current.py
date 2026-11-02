"""Current pipeline diagram facts and layout invariants."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/build_flow.py"
DIAGRAM = ROOT / "docs/hermes-control-flow.html"


def _load_data() -> dict:
    source = GENERATOR.read_text(encoding="utf-8")
    data_part = source.split("# ----------------------------------------------------------------------------- RENDER")[0]
    ns: dict = {"__file__": str(GENERATOR)}
    exec(compile(data_part, str(GENERATOR), "exec"), ns)
    return ns


def test_outdated_dev_generator_removed():
    assert not (ROOT / "docs/dev/build_flow.py").exists()


def test_status_md_line_21_points_to_scripts_build_flow():
    status_path = ROOT / "docs/dev/STATUS.md"
    if status_path.exists():
        line_21 = status_path.read_text(encoding="utf-8").splitlines()[20]
        assert "python3 scripts/build_flow.py" in line_21
        assert "docs/dev/build_flow.py" not in line_21


def test_diagram_mentions_current_engines_routing_author_and_secret_scan():
    diagram = DIAGRAM.read_text(encoding="utf-8")
    for required in (
        "gemini",
        "gemini worker",
        "@gemini",
        "agy",
        "proceed-in-sandbox",
        "failure_mode guard",
        "routing.fallback",
        "model_rule",
        "engines.codex.enabled",
        "diagram.author",
        "commit_worktree",
        "secret_scan.enabled",
        "allow_paths",
        "path:line",
    ):
        assert required in diagram


def test_boxes_do_not_overlap_and_text_fits():
    ns = _load_data()
    rects = []
    cx = 45
    for title, lines in ns["CONTEXT_CARDS"]:
        rects.append((cx, 50, 265, 80, title, lines, 8))
        cx += 286
    for col, row, _kind, title, lines in ns["BOXES"]:
        x, w = ns["COL"][col]
        y, h = ns["ROW"][row]
        rects.append((x, y, w, h, title, lines, 9))
    rects.append((45, 570, 1315, 110, ns["WORKER_RUN"][1], ns["WORKER_RUN"][2], 9))
    rects.append((1420, 570, 320, 110, ns["WORKER_REPORT"][1], ns["WORKER_REPORT"][2], 9))
    rx = ns["COL"]["B"][0]
    rw = ns["COL"]["G"][0] + ns["COL"]["G"][1] - ns["COL"]["B"][0]
    ry, rh = ns["ROW"][4]
    rects.append((rx, ry, rw, rh, ns["RUNLOG"][1], ns["RUNLOG"][2], 9))
    for x, w, title, lines in ns["OBSERVE"]:
        rects.append((x, 1180, w, 90, title, lines, 9))
    for x, w, title, lines in ns["HUMAN_TOOLS"]:
        rects.append((x, 1290, w, 125, title, lines, 9))
    bx = 45
    for _kind, title, lines in ns["BOTTOM"]:
        rects.append((bx, 1460, 551, 95, title, lines, 9))
        bx += 572

    for i, (x1, y1, w1, h1, title1, lines1, fsize) in enumerate(rects):
        assert x1 >= 0 and y1 >= 0
        assert x1 + w1 <= ns["W"]
        assert y1 + h1 <= ns["H"]
        last_line_y = 38 + 14 * max(0, len(lines1) - 1)
        assert last_line_y + 6 <= h1, f"{title1} overflows height {h1}"
        char_w = 0.6 * fsize
        for line in lines1:
            assert 24 + len(line) * char_w <= w1 + 4, f"{title1} line too wide for {w1}: {line!r}"
        for x2, y2, w2, h2, title2, _lines2, _fsize2 in rects[i + 1:]:
            overlaps = not (x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1)
            assert not overlaps, f"{title1} overlaps {title2}"


def test_arrows_start_and_end_on_boxes():
    ns = _load_data()
    boxes = []
    for col, row, _kind, _title, _lines in ns["BOXES"]:
        x, w = ns["COL"][col]
        y, h = ns["ROW"][row]
        boxes.append((x, y, x + w, y + h))
    boxes.append((45, 570, 45 + 1315, 570 + 110))
    boxes.append((1420, 570, 1420 + 320, 570 + 110))
    rx = ns["COL"]["B"][0]
    rw = ns["COL"]["G"][0] + ns["COL"]["G"][1] - ns["COL"]["B"][0]
    ry, rh = ns["ROW"][4]
    boxes.append((rx, ry, rx + rw, ry + rh))
    for x, w, _title, _lines in ns["OBSERVE"]:
        boxes.append((x, 1180, x + w, 1180 + 90))

    def on_box_border(px, py, tol=3):
        for x1, y1, x2, y2 in boxes:
            on_v = (abs(px - x1) <= tol or abs(px - x2) <= tol) and (y1 - tol <= py <= y2 + tol)
            on_h = (abs(py - y1) <= tol or abs(py - y2) <= tol) and (x1 - tol <= px <= x2 + tol)
            if on_v or on_h:
                return True
        return False

    for path, *_ in ns["ARROWS"]:
        if path == "M 0 0":
            continue
        pts = [(int(m.group(1)), int(m.group(2))) for m in re.finditer(r"[ML]\s*(\d+)\s+(\d+)", path)]
        assert len(pts) >= 2
        assert on_box_border(*pts[0]), f"arrow start {pts[0]} not on any box border: {path}"
        assert on_box_border(*pts[-1]), f"arrow end {pts[-1]} not on any box border: {path}"
