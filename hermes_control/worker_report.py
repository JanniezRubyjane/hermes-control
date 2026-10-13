"""Requested worker reports and review findings; verification remains proof."""

from __future__ import annotations

import re


REPORT_REQUEST = (
    "End your final message with exactly this report (under 500 characters):\n"
    "RESULT: PASS | ISSUES | BLOCKED\n"
    "RAN: one line per command: <command> -> exit <n>: <last output line>, or none\n"
    "DEVIATIONS: <what differs from the goal, or none>\n"
    "FOLLOW-UPS: <suggested next tasks, or none>"
)

REVIEW_FORMAT = (
    "Return one finding per line in this exact shape:\n"
    "[critical|warning|nit] path:line - finding - evidence\n"
    "Choose one severity per finding; 'no findings' on its own line is a valid answer.\n"
    "End with a final line: VERDICT: READY or VERDICT: NOT READY"
)

# Copy health's label vocabulary and recognition; health keeps its own parser.
_SEVERITY_LEVELS = {
    "critical": "critical", "warning": "warning", "nit": "nit",
    "bug": "critical", "missing": "warning", "blocker": "critical",
    "high": "critical", "major": "warning", "medium": "warning",
    "minor": "nit", "low": "nit",
}
_SEVERITIES = "|".join(_SEVERITY_LEVELS)
_LABEL = re.compile(
    rf"\*\*\s*(?:[A-Z]\d+\s*[-—–]\s*)?({_SEVERITIES})\b[^*\n]*\*\*"
    rf"|\[\s*(?:severity\s*:\s*)?({_SEVERITIES})\b[^\]\n]*\](?!\()"
    rf"|\bseverity\s*:\s*({_SEVERITIES})\b(?![^\[\]\n]*\]\()", re.I)


def parse_findings(text: str | None) -> dict | None:
    """Count canonical and legacy findings, preserving unreadable reviews."""
    if text is None:
        return None
    counts = dict.fromkeys(("critical", "warning", "nit"), 0)
    severity_columns = []
    lines = text.splitlines()
    for line in lines:
        # Exclude format placeholders from all label forms, including severity:.
        finding_line = re.sub(r"\[[^\]\n]*\|[^\]\n]*\]", "", line)
        labels = {next(group for group in match.groups() if group).lower()
                  for match in _LABEL.finditer(finding_line)}
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [cell.strip().lower() for cell in line.strip()[1:-1].split("|")]
            if "severity" in cells:
                severity_columns = [index for index, cell in enumerate(cells)
                                    if cell == "severity"]
            else:
                labels.update(cells[index] for index in severity_columns
                              if index < len(cells) and cells[index] in _SEVERITY_LEVELS)
        else:
            severity_columns = []
        for label in labels:
            counts[_SEVERITY_LEVELS[label]] += 1
    verdict = None
    for line in reversed(lines):
        match = re.match(r"^\W*VERDICT[\s*`_]*:[\s*`_]*(READY|NOT READY)(?![A-Za-z])", line, re.I)
        if match:
            verdict = match[1].upper()
            break
    if verdict is None:
        for line in reversed(lines):
            if re.search(r"\bNOT READY\b", line, re.I):
                verdict = "NOT READY"
                break
            if re.search(r"\bREADY TO MERGE\b", line, re.I):
                verdict = "READY"
                break
    no_findings = any(re.fullmatch(r"[\s*`_># +\-]*no findings[\s*`_]*\.?[\s*`_]*", line, re.I)
                      for line in lines)
    return {**counts, "verdict": verdict,
            "unparsed": not any(counts.values()) and not no_findings and verdict is None}


_RAN = re.compile(r"^(.*?)\s*->\s*exit\s+(-?\d+):\s*(.*)$")
_FIELDS = ("RESULT", "RAN", "DEVIATIONS", "FOLLOW-UPS")
_NONE_RAN = re.compile(r"(?:none\.?|n/a|none\s*\(.*\))", re.IGNORECASE)


def _unmark(value: str) -> str:
    """Remove Markdown marks at the edges without touching marks within a value."""
    value = value.strip()
    while value.startswith(("**", "`")) or value.endswith(("**", "`")):
        if value.startswith("**"):
            value = value[2:]
        if value.endswith("**"):
            value = value[:-2]
        if value.startswith("`"):
            value = value[1:]
        if value.endswith("`"):
            value = value[:-1]
        value = value.strip()
    return value


def parse(text: str | None) -> dict | None:
    """Parse the final report, ignoring common Markdown line wrappers."""
    if text is None:
        return None
    fields: dict[str, str | list[str]] = {}
    completed: dict[str, str | list[str]] | None = None
    current_label = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            continue
        bullet = line.startswith(("- ", "* "))
        if bullet:
            line = line[2:].strip()
        line = re.sub(r"^\*\*([A-Za-z-]+):\*\*", r"\1:", line)
        line = re.sub(r"^\*\*([A-Za-z-]+)\*\*:", r"\1:", line)
        label, separator, value = line.partition(":")
        label = label.strip(" `*").upper()
        if not separator or label not in _FIELDS:
            if bullet and current_label == "RAN" and "RESULT" in fields:
                assert isinstance(fields["RAN"], list)
                fields["RAN"].append(_unmark(line))
            else:
                current_label = None
            continue
        current_label = label
        value = _unmark(value)
        if label == "RESULT":
            if fields.get("RESULT") in {"PASS", "ISSUES", "BLOCKED"} and all(
                    name in fields for name in _FIELDS):
                completed = fields
            fields = {"RESULT": value}
        elif "RESULT" in fields:
            if label == "RAN":
                fields.setdefault("RAN", [])
                assert isinstance(fields["RAN"], list)
                fields["RAN"].append(value)
            else:
                fields[label] = value
    if fields.get("RESULT") not in {"PASS", "ISSUES", "BLOCKED"} or not all(
            name in fields for name in _FIELDS):
        if completed is None:
            return None
        fields = completed
    ran = []
    for claim in fields["RAN"]:
        if not claim or _NONE_RAN.fullmatch(claim):
            continue
        match = _RAN.fullmatch(claim)
        if not match or not match.group(1).strip():
            ran.append({"command": _unmark(claim), "exit_code": None, "output": ""})
            continue
        ran.append({"command": _unmark(match.group(1)),
                    "exit_code": int(match.group(2)), "output": match.group(3)})
    return {"result": fields["RESULT"], "ran": ran,
            "deviations": fields["DEVIATIONS"], "follow_ups": fields["FOLLOW-UPS"]}
