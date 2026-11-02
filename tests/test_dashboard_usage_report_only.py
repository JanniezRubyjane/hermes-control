"""Offline checks for report-only usage bars and cap controls."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


PAGE = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/dist/index.js"
SOURCE = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")


def _node(program):
    if not NODE:
        pytest.skip("node is unavailable; static dashboard checks still run")
    pure = SOURCE.split("// Pure usage helpers", 1)[1].split("// End pure usage helpers.", 1)[0]
    result = subprocess.run(
        [NODE, "-e", 'const assert = require("node:assert/strict");\n' + pure + "\n" + program],
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_report_only_bars_never_show_cap_or_warning_tones():
    _node(r'''
        const usage = {cap: 80, report_only: true};
        for (const percent of [0, 50, 95, 100]) {
            const view = usageBarView(usage, {used_percent: percent});
            assert.equal(view.used, percent);
            assert.equal(view.tone, '');
            assert.equal(view.showCap, false);
            assert(!view.title.toLowerCase().includes('cap'));
        }
    ''')


def test_normal_bars_keep_cap_thresholds_and_title():
    _node(r'''
        for (const report_only of [false, undefined]) {
            for (const [percent, tone] of [[50, ''], [64, 'hc-bar-near'],
                                          [80, 'hc-bar-over'], [95, 'hc-bar-over']]) {
                assert.deepEqual(usageBarView({cap: 80, report_only}, {used_percent: percent}),
                                 {used: percent, tone, showCap: true, title: 'cap 80%'});
            }
        }
    ''')


def test_unknown_percent_and_clamping():
    _node(r'''
        for (const report_only of [true, false]) {
            const usage = {cap: 80, report_only};
            for (const percent of [null, undefined]) {
                const view = usageBarView(usage, {used_percent: percent});
                assert.equal(view.used, null);
                assert.equal(view.tone, '');
            }
            assert.equal(usageBarView(usage, {used_percent: -20}).used, 0);
            assert.equal(usageBarView(usage, {used_percent: 120}).used, 100);
        }
    ''')


def test_usage_block_guards_cap_controls_and_uses_bar_helper():
    block = SOURCE.split("function UsageBlock(", 1)[1].split("// What a finished resume", 1)[0]
    assert 'const { used, tone, showCap, title } = usageBarView(usage, w);' in block
    assert re.search(r'showCap\s*\?\s*h\("div", \{ className: "hc-bar-cap"', block)
    assert re.search(
        r'usage\.report_only\s*\? h\("p", \{ className: "hc-muted" \}, '
        r'"Report only: this reading never holds work back\."\)\s*'
        r': h\("div", \{ className: "hc-cap-row" \},\s*'
        r'h\("span", \{ className: "hc-muted" \}, "Send tasks elsewhere at"\),\s*'
        r'h\("input", \{ className: "hc-input hc-cap"', block,
    )
    assert block.count('className: "hc-input hc-cap"') == 1
