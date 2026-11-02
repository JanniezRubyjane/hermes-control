"""Validated repository diagrams and their persisted review records."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env

from .runlog import run_log_dir

MAX_JSON_BYTES = 200 * 1024
KINDS = ("module", "flow", "sequence")
ITEM_ID = re.compile(r"[a-z0-9_-]{1,40}\Z")
DIAGRAM_ID = re.compile(r"d-[0-9]{8}-[0-9a-f]{6}\Z")
VERDICTS = ("confirmed", "wrong", "unverifiable", "unreviewed")
ITEM_FIELDS = {
    "nodes": ("id", "label", "group", "type", "evidence", "note"),
    "edges": ("id", "from", "to", "label", "type", "order", "evidence", "note"),
    "groups": ("id", "label"),
}
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:9119"

_SPEC_FORMAT = '''Return the FULL spec as JSON with this shape:
{"version":1,"title":"...","summary":"...","views":[{"kind":"module|flow|sequence",
"title":"...","groups":[{"id":"core","label":"Core"}],"nodes":[{"id":"entry",
"label":"Entry","group":"core","type":"module|function|file|store|external|actor",
"evidence":[{"path":"relative/file.py","lines":[1,2]}],"note":"optional"}],
"edges":[{"id":"edge1","from":"entry","to":"entry","label":"calls",
"type":"calls|imports|data|event|step","order":1,
"evidence":[{"path":"relative/file.py","lines":[1,2]}]}]}],"unknowns":["..."]}.
One view per requested kind; read the repo once for all views. At most 6 views,
30 nodes and 50 edges per view. IDs match [a-z0-9_-]{1,40} and are unique across
all groups, nodes and edges within each view. Labels are at most
40 characters, summary at most 600, node and edge notes at most 200. The
summary must not repeat the commit hash or repository path; they are shown separately.
Every edge references
nodes of that view; order is an integer required on flow and sequence edges.
Provide at most 5 path:line evidence entries for EVERY node and edge, using relative paths without
.. and valid line ranges at the stamped commit. List what you could not settle in
unknowns. The JSON must be at most 200 KB.
Fix specs may include an optional top-level "resolves" list:
[{"view":"module|flow|sequence","missing":"missing-1","by":["item-id"]}]
or [{"view":"module|flow|sequence","missing":"missing-1","dismiss":"reason"}].
Use a nonempty list of item ids for by, or a dismissal reason <= 200 characters.
Reference the missing ids in the findings; unknown ids are ignored.
End your final message with exactly one fenced block with info string hermes-diagram
containing the JSON.'''
_FIX_CAPS = "\nStay within the caps by merging or dropping minor items rather than adding past them.\n"
_READ_ONLY = "Read the repository read-only. Do not edit files or create commits.\n"
SPEC_REQUEST = _READ_ONLY + "Describe the requested repository design and workflow.\n" + _SPEC_FORMAT
FIX_REQUEST = _READ_ONLY + "Check the review findings against the repo and fix wrong and missing items.\n" + _SPEC_FORMAT + _FIX_CAPS
CLAUDE_FIX_REQUEST = _READ_ONLY + '''Write the final corrected spec from the remaining review findings.
Bash is not available in this read-only task; open every path:line you cite with the Read tool
before adding or changing an item; edits whose evidence you did not open are discarded.
''' + _SPEC_FORMAT + _FIX_CAPS
REVIEW_REQUEST = _READ_ONLY + '''Check the stored spec against the code at the stamped commit.
Review EVERY requested node and edge independently; open the cited lines with Read.
Use confirmed only when evidence supports
it, wrong when contradicted and unverifiable when you cannot settle it. Include
path:line evidence for every reviewed node and edge, plus notes explaining findings.
List in missing only behaviour that no node or edge covers, never something the spec
already shows. List important omissions in missing (each label <= 80 characters, details in its note) and explain anything you could not settle in
notes (the spec's unknowns must remain explicit). End your final message with exactly
one fenced block with info string hermes-diagram-review holding JSON of this shape:
{"views":[{"kind":"module|flow|sequence","items":[{"id":"entry",
"verdict":"confirmed|wrong|unverifiable","note":"...",
"evidence":[{"path":"relative/file.py","lines":[1,2]}]}],
"missing":[{"label":"...","note":"optional",
"evidence":[{"path":"relative/file.py","lines":[1,2]}]}]}]}.
'''


class DiagramError(ValueError):
    """Malformed spec/review, or too much evidence that cannot be verified."""


def _parse(text: str, info: str) -> dict | None:
    if not isinstance(text, str):
        return None
    # Walk whole fences so a target-looking line inside another block is ignored.
    fences = re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n", text)
    last = None
    consumed = 0
    for fence in fences:
        if fence.start() < consumed:
            continue
        marker, header = fence.group(1), fence.group(2).strip()
        end = re.search(r"(?m)^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker))
                        + r",}[ \t]*\r?$", text[fence.end():])
        if end is None:
            break
        body_end = fence.end() + end.start()
        consumed = fence.end() + end.end()
        if header == info:
            last = text[fence.end():body_end]
    if last is None:
        return None
    try:
        if len(last.encode("utf-8")) > MAX_JSON_BYTES:
            return None
        value = json.loads(last, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _invalid_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def parse_spec(text: str) -> dict | None:
    return _parse(text, "hermes-diagram")


def parse_review(text: str) -> dict | None:
    return _parse(text, "hermes-diagram-review")


def _string(value, where: str, errors: list[str], limit: int | None = None) -> None:
    if not isinstance(value, str) or (limit is not None and len(value) > limit):
        errors.append(f"{where}: expected string" + (f" <= {limit} characters" if limit else ""))


def _evidence_shape(evidence, where: str, errors: list[str]) -> None:
    if not isinstance(evidence, list):
        errors.append(f"{where}: evidence must be a list")
        return
    if len(evidence) > 5:
        errors.append(f"{where}: evidence must have at most 5 entries")
        return
    for entry in evidence:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("lines"), list) or len(entry["lines"]) != 2
                or any(type(n) is not int for n in entry["lines"])):
            errors.append(f"{where}: evidence needs a path string and two integer lines")


def _check_structure(spec) -> None:
    errors: list[str] = []
    if not isinstance(spec, dict):
        raise DiagramError("spec must be an object")
    if type(spec.get("version")) is not int or spec["version"] != 1:
        errors.append("version must be 1")
    _string(spec.get("title"), "title", errors)
    _string(spec.get("summary"), "summary", errors, 600)
    if not isinstance(spec.get("unknowns"), list) or any(
            not isinstance(x, str) for x in spec.get("unknowns", [])):
        errors.append("unknowns must be a list of strings")
    views = spec.get("views")
    if not isinstance(views, list) or not 1 <= len(views) <= 6:
        errors.append("views must be a list of 1 to 6 views")
        raise DiagramError("; ".join(errors))
    kinds = set()
    for index, view in enumerate(views):
        where = f"view {index}"
        if not isinstance(view, dict):
            errors.append(f"{where}: must be an object")
            continue
        kind = view.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            errors.append(f"{where}: invalid kind")
        elif kind in kinds:
            errors.append(f"{where}: duplicate kind")
        else:
            kinds.add(kind)
        _string(view.get("title"), f"{where} title", errors)
        collections = {}
        for name, limit in (("groups", None), ("nodes", 30), ("edges", 50)):
            items = view.get(name)
            if not isinstance(items, list) or (limit is not None and len(items) > limit):
                errors.append(f"{where}: invalid {name}" + (f" (limit {limit})" if limit else ""))
                items = []
            collections[name] = items
        ids = set()
        node_ids, group_ids = set(), set()
        for name, items in collections.items():
            for item in items:
                if not isinstance(item, dict):
                    errors.append(f"{where} {name}: item must be an object")
                    continue
                ident = item.get("id")
                if not isinstance(ident, str) or not ITEM_ID.fullmatch(ident):
                    errors.append(f"{where} {name}: invalid id")
                elif ident in ids:
                    errors.append(f"{where}: duplicate id {ident}")
                else:
                    ids.add(ident)
                if isinstance(ident, str):
                    if name == "nodes":
                        node_ids.add(ident)
                    if name == "groups":
                        group_ids.add(ident)
                _string(item.get("label"), f"{where} {name} label", errors, 40)
                if name == "groups":
                    continue
                allowed = ("module", "function", "file", "store", "external", "actor") \
                    if name == "nodes" else ("calls", "imports", "data", "event", "step")
                if item.get("type") not in allowed:
                    errors.append(f"{where} {ident}: invalid type")
                _evidence_shape(item.get("evidence"), f"{where} {ident}", errors)
                if "note" in item:
                    _string(item["note"], f"{where} {ident} note", errors, 200)
                if name == "edges" and ("order" in item or kind in ("flow", "sequence")):
                    if type(item.get("order")) is not int:
                        errors.append(f"{where} {ident}: order must be an integer")
        for node in collections["nodes"]:
            if isinstance(node, dict) and "group" in node:
                if not isinstance(node["group"], str) or node["group"] not in group_ids:
                    errors.append(f"{where}: unknown node group")
        for edge in collections["edges"]:
            if isinstance(edge, dict):
                for endpoint in ("from", "to"):
                    if not isinstance(edge.get(endpoint), str) or edge[endpoint] not in node_ids:
                        errors.append(f"{where}: dangling edge {endpoint}")
    if "resolves" in spec:
        resolves = spec["resolves"]
        if not isinstance(resolves, list):
            errors.append("resolves must be a list")
        else:
            for entry in resolves:
                if (not isinstance(entry, dict) or entry.get("view") not in KINDS
                        or not isinstance(entry.get("missing"), str)
                        or not ITEM_ID.fullmatch(entry["missing"])
                        or set(entry) not in ({"view", "missing", "by"}, {"view", "missing", "dismiss"})):
                    errors.append("resolves entry needs view, missing and either by or dismiss")
                    continue
                if "by" in entry:
                    if (not isinstance(entry["by"], list) or not entry["by"]
                            or any(not isinstance(i, str) or not ITEM_ID.fullmatch(i) for i in entry["by"])):
                        errors.append("resolves by must be a nonempty list of item ids")
                else:
                    _string(entry["dismiss"], "resolves dismiss", errors, 200)
    if errors:
        raise DiagramError("; ".join(errors))


def _file_lines(repo, commit: str, path: str) -> int | None:
    if (not path or PurePosixPath(path).is_absolute() or ".." in path.split("/")
            or "\\" in path or "\x00" in path):
        return None
    target = f"{commit}:{path}"
    try:
        content = subprocess.run(
            ["git", *harden_git_argv(["-C", str(repo), "cat-file", "blob", target])],
            capture_output=True, timeout=10, env=noninteractive_git_env())
        if content.returncode or b"\x00" in content.stdout:
            return None
        return content.stdout.count(b"\n") + bool(content.stdout and not content.stdout.endswith(b"\n"))
    except (OSError, subprocess.TimeoutExpired):
        return None


def validate_spec(spec: dict, repo, commit: str, *, line_counts: dict | None = None) -> tuple[dict, list[str]]:
    """Reject structural errors; retain up to 20% items with unverifiable evidence.

    A pipeline shares line_counts across stages at the same stamped commit.
    Input and worker-supplied annotations are never trusted or modified.
    """
    _check_structure(spec)
    try:
        size = len(json.dumps(spec, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise DiagramError(f"spec is not valid JSON: {exc}") from exc
    if size > MAX_JSON_BYTES:
        raise DiagramError("spec JSON exceeds 200 KB")
    resolves = copy.deepcopy(spec.get("resolves"))
    spec = spec_payload(spec)
    if resolves is not None:
        spec["resolves"] = resolves
    problems: list[str] = []
    cache = {} if line_counts is None else line_counts
    bad, total = 0, 0
    for view in spec["views"]:
        for item in view["nodes"] + view["edges"]:
            total += 1
            invalid = not item["evidence"]
            for evidence in item["evidence"]:
                path = evidence["path"]
                key = (str(repo), commit, path)
                if key not in cache:
                    cache[key] = _file_lines(repo, commit, path)
                count = cache[key]
                start, end = evidence["lines"]
                if count is None or not 1 <= start <= end <= count:
                    invalid = True
            item["evidence_invalid"] = invalid
            if invalid:
                bad += 1
                problems.append(f"{view['kind']} {item['id']}: invalid evidence")
    if total and bad * 5 > total:
        raise DiagramError("More than 20% of items have invalid evidence: " + "; ".join(problems))
    return spec, problems


def spec_payload(record: dict) -> dict:
    """Only schema content, for workers and comparisons, without review annotations."""
    return {"version": 1, "title": record["title"], "summary": record["summary"],
            "unknowns": copy.deepcopy(record["unknowns"]), "views": [
                {"kind": view["kind"], "title": view["title"],
                 **{name: [{k: copy.deepcopy(value) for k, value in item.items() if k in ITEM_FIELDS[name]}
                           for item in view[name]] for name in ("groups", "nodes", "edges")}}
                for view in record["views"]]}


def update_counts(record: dict) -> None:
    counts = dict.fromkeys((*VERDICTS, "missing", "missing_settled", "evidence_invalid", "claude_edited"), 0)
    for view in record.get("views", []):
        for missing in view.get("missing", []):
            counts["missing_settled" if missing.get("status") in ("resolved", "dismissed") else "missing"] += 1
        for item in view["nodes"] + view["edges"]:
            counts[item.get("verdict", "unreviewed")] += 1
            counts["evidence_invalid"] += bool(item.get("evidence_invalid"))
            counts["claude_edited"] += bool(item.get("claude_edited"))
    record["counts"] = counts


def merge_review(record: dict, review: dict, *, append: bool = False) -> None:
    errors: list[str] = []
    if not isinstance(review, dict) or not isinstance(review.get("views"), list):
        raise DiagramError("review views must be a list")
    seen = set()
    for view in review["views"]:
        if not isinstance(view, dict) or not isinstance(view.get("kind"), str):
            errors.append("review view needs a kind string")
            continue
        if view["kind"] in seen:
            errors.append("duplicate review view kind")
        seen.add(view["kind"])
        for name in ("items", "missing"):
            if not isinstance(view.get(name), list):
                errors.append(f"review {name} must be a list")
                continue
            for item in view[name]:
                if not isinstance(item, dict):
                    errors.append("review item must be an object")
                    continue
                if name == "items":
                    _string(item.get("id"), "review id", errors)
                    if item.get("verdict") not in VERDICTS[:3]:
                        errors.append("invalid review verdict")
                    _string(item.get("note"), "review note", errors)
                else:
                    _string(item.get("label"), "missing label", errors)
                    if "note" in item:
                        _string(item["note"], "missing note", errors)
                _evidence_shape(item.get("evidence"), "review", errors)
    if errors:
        raise DiagramError("; ".join(errors))
    views = {v["kind"]: v for v in record["views"]}
    if not append:
        record["review_unknown_ids"] = []
    for view in views.values():
        if not append:
            view["missing"] = []
        for item in view["nodes"] + view["edges"]:
            if not append:
                item["verdict"], item["review_note"] = "unreviewed", ""
            item.setdefault("evidence_invalid", False)
            item.setdefault("claude_edited", False)
    for checked in review["views"]:
        view = views.get(checked["kind"])
        items = {i["id"]: i for i in view["nodes"] + view["edges"]} if view else {}
        for finding in checked["items"]:
            item = items.get(finding["id"])
            if item is None:
                record["review_unknown_ids"].append(finding["id"])
                continue
            if append and item.get("verdict", "unreviewed") != "unreviewed":
                continue
            item["verdict"] = finding["verdict"]
            item["review_note"] = finding["note"]
        if view is not None:
            labels = {m["label"] for m in view["missing"]}
            ids = {i["id"] for name in ("groups", "nodes", "edges") for i in view[name]}
            ids.update(m["id"] for m in view["missing"])
            for finding in checked["missing"]:
                missing = _fit_missing(copy.deepcopy(finding))
                if missing["label"] in labels:
                    continue
                index = 1
                while f"missing-{index}" in ids:
                    index += 1
                ident = f"missing-{index}"
                view["missing"].append({"id": ident, "label": missing["label"],
                                        "note": missing.get("note", ""),
                                        "evidence": missing["evidence"], "status": "open"})
                ids.add(ident)
                labels.add(missing["label"])
    update_counts(record)


def _fit_missing(item: dict) -> dict:
    """Cut an over-long omission label to 80 characters, keeping the full text in its note.

    Reviewers write omissions as sentences; one long label must not throw away the
    whole review's verdicts.
    """
    label = item["label"].strip()
    if len(label) > 80:
        note = item.get("note", "").strip()
        item["note"] = label + (f" ({note})" if note else "")
        item["label"] = label[:79].rstrip() + "…"
    return item


def is_clean(record: dict) -> bool:
    return all(not any(m.get("status") not in ("resolved", "dismissed") for m in view.get("missing", []))
               and all(item.get("verdict") != "wrong"
               for item in view["nodes"] + view["edges"]) for view in record["views"])


def save_diagram(record: dict) -> None:
    ident = record.get("id")
    if not isinstance(ident, str) or not DIAGRAM_ID.fullmatch(ident):
        raise DiagramError("invalid diagram id")
    directory = run_log_dir() / "diagrams"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".diagram-", suffix=".tmp", delete=False) as file:
            temporary = file.name
            json.dump(record, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, directory / f"{ident}.json")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_diagram(ident: str) -> dict | None:
    if not isinstance(ident, str) or not DIAGRAM_ID.fullmatch(ident):
        return None
    try:
        with (run_log_dir() / "diagrams" / f"{ident}.json").open(encoding="utf-8") as file:
            record = json.load(file)
        return record if isinstance(record, dict) and record.get("id") == ident else None
    except (OSError, ValueError, RecursionError):
        return None


def list_diagrams() -> list[dict]:
    records = []
    for path in (run_log_dir() / "diagrams").glob("*.json"):
        record = load_diagram(path.stem)
        if record is not None and isinstance(record.get("created_at"), (float, int)):
            records.append({key: value for key, value in record.items() if key not in ("views", "stages")})
    return sorted(records, key=lambda record: record["created_at"], reverse=True)


def diagram_url(ident: str) -> str:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    try:
        entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
        settings = entry.get("settings")
        value = settings.get("dashboard_url") if isinstance(settings, dict) else None
    except Exception:
        value = None
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                value = None
        except ValueError:
            value = None
    base = value.rstrip("/") if isinstance(value, str) else DEFAULT_DASHBOARD_URL
    return f"{base}/engines?diagram={ident}"


def is_stale(record: dict, *, head_cache: dict | None = None) -> bool | None:
    """Compare the stamp with HEAD, optionally reading each repo once per request."""
    cache = {} if head_cache is None else head_cache
    try:
        repo, commit = record["repo"], record["commit"]
        if repo not in cache:
            cache[repo] = None
            result = subprocess.run(
                ["git", *harden_git_argv(["-C", repo, "rev-parse", "HEAD"])],
                capture_output=True, text=True, timeout=10, env=noninteractive_git_env())
            if not result.returncode:
                cache[repo] = result.stdout.strip()
        head = cache[repo]
        return head != commit if head is not None else None
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError):
        return None
