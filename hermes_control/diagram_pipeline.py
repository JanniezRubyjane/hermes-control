"""Repository diagram stages over the existing read-only dispatcher."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from typing import Any

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env

from . import diagram, model_policy, runlog
from .engines import registry

AUTHOR_ENGINES = ("codex", "gemini")
_STATUS = {"spec": "writing", "review": "reviewing", "fix": "fixing",
           "review-rest": "reviewing", "re-review-rest": "re-reviewing",
           "re-review": "re-reviewing", "claude-fix": "claude-fixing"}


def parse_author(raw: Any) -> str | None:
    """The configured diagram spec/fix author, or None when unset; malformed settings fail closed."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise diagram.DiagramError("diagram setting must be a mapping")
    unknown = set(raw) - {"author"}
    if unknown:
        raise diagram.DiagramError(
            f"diagram setting has unknown key(s) {', '.join(sorted(map(str, unknown)))}; "
            "only diagram.author is allowed")
    if "author" not in raw:
        return None
    author = raw["author"]
    if author == "claude":
        raise diagram.DiagramError(
            "diagram.author cannot be 'claude' because claude reviews the author's spec; "
            "choose 'codex' or 'gemini'")
    if not isinstance(author, str) or author not in AUTHOR_ENGINES:
        raise diagram.DiagramError(
            f"diagram.author must be 'codex' or 'gemini', got {author!r}")
    return author


def read_author() -> str:
    """The author engine for this run.

    An explicit diagram.author must be enabled. Unset, it is the first enabled of
    codex, gemini, so turning codex off does not stop diagrams."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
    settings = entry.get("settings")
    if settings is not None and not isinstance(settings, Mapping):
        raise diagram.DiagramError("hermes_control settings must be a mapping")
    if isinstance(settings, Mapping) and "diagram" in settings and settings["diagram"] is None:
        raise diagram.DiagramError("diagram setting must be a mapping")
    author = parse_author(settings.get("diagram") if isinstance(settings, Mapping) else None)
    enabled = registry.ENGINE_NAMES
    if author is None:
        author = next((name for name in AUTHOR_ENGINES if name in enabled), None)
        if author is None:
            raise diagram.DiagramError(
                "diagram.author: no author engine is enabled; enable codex or gemini "
                "(engines.<name>.enabled)")
    elif author not in enabled:
        raise diagram.DiagramError(
            f"diagram.author engine {author!r} is not enabled (enable engines.{author}.enabled first)")
    return author


def _author_default_model(author: str) -> str | None:
    """The author engine's default model from settings.models, for the stage record."""
    try:
        policy = model_policy.load_policy()
    except ValueError as exc:
        raise diagram.DiagramError(str(exc)) from exc
    rules = policy.get(author)
    return rules.default if rules is not None else None


def repo_commit(repo) -> tuple[str, str]:
    """Validate a working tree and stamp its HEAD before launching any workers."""
    repo = str(Path(repo).resolve())
    try:
        worktree = subprocess.run(
            ["git", *harden_git_argv(["-C", repo, "rev-parse", "--is-inside-work-tree"])],
            capture_output=True, text=True, timeout=10, env=noninteractive_git_env())
        head = subprocess.run(
            ["git", *harden_git_argv(["-C", repo, "rev-parse", "--verify", "HEAD^{commit}"])],
            capture_output=True, text=True, timeout=10, env=noninteractive_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise diagram.DiagramError(f"repo must be inside a git work tree with a commit: {exc}") from exc
    if worktree.returncode or worktree.stdout.strip() != "true" or head.returncode:
        raise diagram.DiagramError("repo must be inside a git work tree with a commit")
    return repo, head.stdout.strip()


def _install_spec(record: dict, spec: dict, *, claude_fix: bool = False) -> bool:
    previous = {view["kind"]: view for view in record["views"]}
    content = {view["kind"]: view for view in diagram.spec_payload(record)["views"]}
    new_views = copy.deepcopy(spec["views"])
    changed = False
    for view in new_views:
        old_view = previous.get(view["kind"], {})
        old_items = {i["id"]: i for i in old_view.get("nodes", []) + old_view.get("edges", [])}
        old_content = content.get(view["kind"], {})
        originals = {i["id"]: i for i in old_content.get("nodes", []) + old_content.get("edges", [])}
        if claude_fix:
            changed = changed or bool(originals.keys() - {i["id"] for i in view["nodes"] + view["edges"]})
        for name in ("nodes", "edges"):
            for item in view[name]:
                plain = {key: value for key, value in item.items() if key in diagram.ITEM_FIELDS[name]}
                edited = claude_fix and originals.get(item["id"]) != plain
                changed = changed or edited
                old = old_items.get(item["id"], {}) if claude_fix else {}
                item["claude_edited"] = edited
                item["verdict"] = "unreviewed" if edited else old.get("verdict", "unreviewed")
                item["review_note"] = "Corrected by claude in the final fix." if edited else old.get("review_note", "")
        view["missing"] = copy.deepcopy(old_view.get("missing", [])) if claude_fix else []
    for key in ("title", "summary", "unknowns"):
        record[key] = copy.deepcopy(spec[key])
    record["views"] = new_views
    if claude_fix and changed:
        record["spec_author"] = "claude"
    diagram.update_counts(record)
    return changed


def _findings(record: dict) -> dict:
    return {"views": [{"kind": view["kind"],
                       "wrong": [{"id": item["id"], "note": item["review_note"]}
                                 for item in view["nodes"] + view["edges"] if item["verdict"] == "wrong"],
                       "missing": view["missing"]} for view in record["views"]]}


def _read_events(run_id: str) -> list[dict]:
    with runlog.run_log_path(run_id).open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _read_ranges(events, task_id: str) -> list[tuple[str, int, int]]:
    events = [event for event in events if isinstance(event, dict)
              and event.get("task_id", task_id) == task_id]
    failed = {payload["tool_id"] for event in events
              if event.get("kind") == "tool_end"
              and isinstance(payload := event.get("payload"), dict)
              and isinstance(payload.get("tool_id"), str) and payload.get("is_error") is True}
    reads = []
    for event in events:
        if event.get("kind") != "tool_start":
            continue
        payload = event.get("payload")
        if (not isinstance(payload, dict) or payload.get("name") != "Read"
                or (isinstance(payload.get("tool_id"), str) and payload["tool_id"] in failed)):
            continue
        inputs = payload.get("input")
        if not isinstance(inputs, dict):
            continue
        path, offset, limit = inputs.get("file_path"), inputs.get("offset", 1), inputs.get("limit", 2000)
        if type(offset) is int:
            offset = max(1, offset)
        if (isinstance(path, str) and Path(path).is_absolute()
                and type(offset) is int and offset >= 1 and type(limit) is int and limit > 0):
            reads.append((path, offset, offset + limit - 1))
    return reads


def _covered(evidence: dict, reads: list, worktree: str | None) -> bool:
    path, (start, end) = evidence["path"], evidence["lines"]
    if not path or start < 1 or end < start or ".." in path.split("/") or path.startswith("/"):
        return False
    ranges = []
    prefix = os.path.normpath(worktree).rstrip("/") + "/" if worktree else None
    for opened, first, last in reads:
        opened = os.path.normpath(opened)
        matches = (opened.startswith(prefix) and opened[len(prefix):] == path
                   if prefix else opened.endswith("/" + path))
        if matches:
            ranges.append((first, last))
    next_line = start
    for first, last in sorted(ranges):
        if first > next_line:
            break
        next_line = max(next_line, last + 1)
        if next_line > end:
            return True
    return False


def _check_confirmations(record: dict, review: dict, reads: list, worktree, *, append: bool) -> int:
    """Amend the review before merging; an extra review cannot change earlier verdicts."""
    views = {v["kind"]: v for v in record["views"]}
    downgraded = 0
    for checked in review["views"]:
        view = views.get(checked["kind"], {})
        items = {i["id"]: i for i in view.get("nodes", []) + view.get("edges", [])}
        for finding in checked["items"]:
            item = items.get(finding["id"])
            if (item is None or finding["verdict"] != "confirmed"
                    or (append and item.get("verdict", "unreviewed") != "unreviewed")):
                continue
            if not any(_covered(e, reads, worktree) for e in item["evidence"] + finding["evidence"]):
                finding["verdict"] = "unverifiable"
                finding["note"] = "Downgraded: the reviewer did not open the cited lines. " + finding["note"]
                downgraded += 1
    return downgraded


def _drop_unchanged_deleted_edges(record: dict, spec: dict) -> dict:
    """Allow initial validation when a wrong node's unchanged edges were left behind."""
    spec = copy.deepcopy(spec)
    previous = {v["kind"]: v for v in record["views"]}
    originals = {v["kind"]: v for v in diagram.spec_payload(record)["views"]}
    views = spec.get("views")
    if not isinstance(views, list):
        return spec
    for view in views:
        if not isinstance(view, dict) or not isinstance(view.get("kind"), str):
            continue
        old_view = previous.get(view["kind"])
        nodes, edges, groups = view.get("nodes"), view.get("edges"), view.get("groups")
        # Leave malformed and over-cap payloads intact for the usual validation.
        if (old_view is None or not isinstance(nodes, list) or not isinstance(edges, list)
                or not isinstance(groups, list)
                or len(nodes) > 30 or len(edges) > 50
                or any(not isinstance(i, dict) or not isinstance(i.get("id"), str)
                       for i in nodes + edges + groups)):
            continue
        if len({i["id"] for i in nodes + edges + groups}) != len(nodes + edges + groups):
            continue
        node_ids = {n["id"] for n in nodes}
        deleted = {n["id"] for n in old_view["nodes"]
                   if n.get("verdict") == "wrong" and n["id"] not in node_ids}
        old_edges = {e["id"]: e for e in originals[view["kind"]]["edges"]}
        view["edges"] = [e for e in edges if not (
            any(e.get(endpoint) in deleted for endpoint in ("from", "to")
                if isinstance(e.get(endpoint), str))
            and old_edges.get(e["id"]) == {k: v for k, v in e.items() if k in diagram.ITEM_FIELDS["edges"]})]
    return spec


def _guard_claude_edits(record: dict, spec: dict, reads: list, worktree) -> tuple[dict, set]:
    """Restore unsupported changes and non-wrong deletions before validating again."""
    previous = {v["kind"]: v for v in record["views"]}
    originals = {v["kind"]: v for v in diagram.spec_payload(record)["views"]}
    spec = copy.deepcopy(spec)
    reverted = set()
    for view in spec["views"]:
        kind = view["kind"]
        old_view = previous[kind]
        for name in ("nodes", "edges"):
            old = {i["id"]: i for i in originals[kind][name]}
            verdicts = {i["id"]: i.get("verdict") for i in old_view[name]}
            proposed = {i["id"]: i for i in view[name]}
            kept = []
            for item in view[name]:
                ident = item["id"]
                plain = {k: v for k, v in item.items() if k in diagram.ITEM_FIELDS[name]}
                if (old.get(ident) != plain and (not item["evidence"] or not all(
                        _covered(e, reads, worktree) for e in item["evidence"]))):
                    reverted.add((kind, ident))
                    if ident in old:
                        kept.append(copy.deepcopy(old[ident]))
                else:
                    kept.append(item)
            for ident, item in old.items():
                if ident not in proposed and verdicts[ident] != "wrong":
                    kept.append(copy.deepcopy(item))
                    reverted.add((kind, ident))
            view[name] = kept
        node_ids = {n["id"] for n in view["nodes"]}
        deleted = {n["id"] for n in old_view["nodes"]} - node_ids
        old_edges = {e["id"]: e for e in originals[kind]["edges"]}
        kept_edges = []
        for edge in view["edges"]:
            if edge["from"] not in node_ids or edge["to"] not in node_ids:
                reverted.add((kind, edge["id"]))
                if edge["id"] in old_edges:
                    edge = copy.deepcopy(old_edges[edge["id"]])
                else:
                    continue
            if edge["from"] in deleted or edge["to"] in deleted:
                reverted.discard((kind, edge["id"]))
                continue
            kept_edges.append(edge)
        view["edges"] = kept_edges
    return spec, reverted


def _settle_missing(record: dict, resolves: list, reverted: set, reads, worktree) -> None:
    """Only settle claims supported by this fix's Reads, including unchanged items."""
    reads = reads or []
    views = {v["kind"]: v for v in record["views"]}
    for resolution in resolves:
        view = views.get(resolution["view"])
        if view is None:
            continue
        missing = next((m for m in view["missing"] if m.get("id") == resolution["missing"]), None)
        if missing is None or missing.get("status", "open") != "open":
            continue
        if "dismiss" in resolution:
            if (not missing["evidence"]
                    or any(_covered(e, reads, worktree) for e in missing["evidence"])):
                missing.update(status="dismissed", reason=resolution["dismiss"])
        else:
            items = {i["id"]: i for i in view["nodes"] + view["edges"]}
            supported = True
            for ident in resolution["by"]:
                item = items.get(ident)
                if item is None or (view["kind"], ident) in reverted:
                    supported = False
                    break
                check = all if item.get("claude_edited") else any
                if not item["evidence"] or not check(_covered(e, reads, worktree) for e in item["evidence"]):
                    supported = False
                    break
            if supported:
                missing.update(status="resolved", by=copy.deepcopy(resolution["by"]))
    diagram.update_counts(record)


def run(repo, ask, kinds, *, author_model=None, codex_model=None, claude_model=None,
        lang="en", dispatch_fn=None, event_reader=None, now=time.time) -> dict:
    """Run at most seven workers; worker and validation failures become records.

    event_reader(run_id) yields decoded JSONL events; failures skip review/edit
    read checks, while omission settlements still require proof of Reads.
    """
    repo, commit = repo_commit(repo)
    kinds = list(dict.fromkeys(kinds or ["module", "flow"]))
    if not isinstance(ask, str) or not ask.strip():
        raise diagram.DiagramError("ask must be a nonempty string")
    if any(kind not in diagram.KINDS for kind in kinds):
        raise diagram.DiagramError("kinds must be module, flow or sequence")
    if not isinstance(lang, str) or not lang.strip():
        raise diagram.DiagramError("lang must be a nonempty string")
    if author_model is not None and codex_model is not None and author_model != codex_model:
        raise diagram.DiagramError("pass only one of author_model or codex_model")
    author = read_author()
    author_model = author_model if author_model is not None else codex_model
    if author_model is None:
        author_model = _author_default_model(author)
    event_reader = event_reader or _read_events
    if dispatch_fn is None:
        from .dispatch import dispatch
        dispatch_fn = dispatch
    created = now()
    prefix = "d-" + datetime.fromtimestamp(created, timezone.utc).strftime("%Y%m%d") + "-"
    while True:
        ident = prefix + secrets.token_hex(3)
        if not (diagram.run_log_dir() / "diagrams" / f"{ident}.json").exists():
            break
    record = {"id": ident, "version": 1, "repo": repo, "commit": commit, "ask": ask,
              "kinds": kinds, "lang": lang, "status": "writing", "reason": "", "created_at": created,
              "updated_at": created, "title": "", "summary": "", "unknowns": [],
              "spec_author": author, "views": [], "stages": [], "counts": {},
              "url": diagram.diagram_url(ident)}
    line_counts = {}
    fix_resolves = {}
    diagram.update_counts(record)
    diagram.save_diagram(record)

    def save():
        record["updated_at"] = now()
        diagram.save_diagram(record)

    def stage(name: str, engine: str, goal: str, *, review: bool = False) -> bool:
        nonlocal fix_resolves
        if lang != "en":
            goal += (f"\nWrite summary, notes, missing notes and unknowns in {lang}; "
                     "ids, labels, code names and paths stay English.\n")
        task_id = f"{ident}-{name}"
        task = {"id": task_id, "engine": engine, "policy": "read-only",
                "goal": f"Your current directory is a checkout of the stamped commit {commit}.\n"
                        "All evidence paths are relative to it. You must not read the original "
                        "repository checkout.\n\n" + goal,
                "why": f"diagram pipeline: {name}"}
        model = author_model if engine == author else claude_model
        if model is not None:
            task["model"] = model
        entry = {"stage": name, "run_id": "", "task_id": task_id, "engine": engine,
                 "model": model, "ok": None, "reason": "", "started_at": now(), "finished_at": None}
        record["status"] = _STATUS[name]
        record["stages"].append(entry)
        save()
        reasons = []
        try:
            result = dispatch_fn([task], parent_cwd=repo)
            if not isinstance(result, dict):
                raise diagram.DiagramError("dispatch returned no result")
            entry["run_id"] = result.get("run_id", "")
            tasks = result.get("tasks")
            reported = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else {}
            for key in ("task_id", "engine", "model"):
                if key in reported:
                    entry[key] = reported[key]
            if "base_commit" in reported and reported["base_commit"] != commit:
                raise diagram.DiagramError(
                    f"repo HEAD moved from {commit[:7]} to {str(reported['base_commit'])[:7]} "
                    "during the run; re-run the diagram")
            if result.get("error"):
                raise diagram.DiagramError(str(result["error"]))
            if reported.get("ok") is not True:
                raise diagram.DiagramError(str(reported.get("text") or reported.get("error") or "worker task failed"))
            text = reported.get("text", "")
            reads = None
            if review or name == "claude-fix":
                try:
                    reads = _read_ranges(event_reader(entry["run_id"]), entry["task_id"])
                except Exception as exc:
                    reasons.append(f"read checks skipped: run log could not be read ({exc})")
            worktree = reported.get("worktree")
            if review:
                parsed = diagram.parse_review(text)
                if parsed is None:
                    raise diagram.DiagramError("missing or invalid hermes-diagram-review JSON block")
                append = name.endswith("-rest")
                # Validate before examining findings; invalid reviews leave the record intact.
                candidate = copy.deepcopy(record)
                diagram.merge_review(candidate, parsed, append=append)
                if reads is not None:
                    downgraded = _check_confirmations(record, parsed, reads, worktree, append=append)
                    if downgraded:
                        reasons.append(f"{downgraded} confirmations downgraded: evidence not opened")
                diagram.merge_review(record, parsed, append=append)
                if append and record["counts"]["unreviewed"]:
                    reasons.append(f"{record['counts']['unreviewed']} items still unreviewed")
            else:
                parsed = diagram.parse_spec(text)
                if parsed is None:
                    raise diagram.DiagramError("missing or invalid hermes-diagram JSON block")
                if name == "claude-fix":
                    parsed = _drop_unchanged_deleted_edges(record, parsed)
                spec, _problems = diagram.validate_spec(parsed, repo, commit, line_counts=line_counts)
                actual = {view["kind"] for view in spec["views"]}
                missing, extra = set(kinds) - actual, actual - set(kinds)
                if missing or extra:
                    kind_errors = []
                    if missing:
                        kind_errors.append("missing kinds: " + ", ".join(sorted(missing)))
                    if extra:
                        kind_errors.append("extra kinds: " + ", ".join(sorted(extra)))
                    raise diagram.DiagramError("spec does not match requested kinds; " + "; ".join(kind_errors))
                install = True
                reverted = set()
                if name == "claude-fix" and reads is not None:
                    if not reads:
                        install = False
                        reasons.append("claude-fix read no code; kept the reviewed spec")
                    else:
                        spec, reverted = _guard_claude_edits(record, spec, reads, worktree)
                        if reverted:
                            reasons.append(f"{len(reverted)} edits reverted: evidence not opened")
                        try:
                            spec, _ = diagram.validate_spec(spec, repo, commit, line_counts=line_counts)
                        except diagram.DiagramError as exc:
                            install = False
                            reasons.append(f"filtered claude-fix invalid; kept the reviewed spec: {exc}")
                if install:
                    if name == "fix":
                        # Installing a codex spec clears missing; retain the claims' context first.
                        missing = {v["kind"]: {m["id"]: m for m in v["missing"]} for v in record["views"]}
                        fix_resolves = {}
                        for resolution in spec.get("resolves", []):
                            omission = missing.get(resolution["view"], {}).get(resolution["missing"])
                            if omission is not None:
                                claim = {k: copy.deepcopy(v) for k, v in resolution.items() if k != "view"}
                                claim.update(label=omission["label"], note=omission.get("note", ""))
                                fix_resolves.setdefault(resolution["view"], []).append(claim)
                    changed = _install_spec(record, spec, claude_fix=name == "claude-fix")
                    if name == "claude-fix":
                        _settle_missing(record, spec.get("resolves", []), reverted, reads, worktree)
                        if not changed:
                            reasons.append("claude made no changes")
            entry["reason"] = "; ".join(reasons)
            entry["ok"] = True
        except Exception as exc:
            entry["ok"] = False
            entry["reason"] = "; ".join([*reasons, str(exc) or type(exc).__name__])
            record["status"], record["reason"] = "failed", entry["reason"]
        entry["finished_at"] = now()
        save()
        return entry["ok"]

    def spec_json():
        return "Stored spec JSON:\n" + json.dumps(diagram.spec_payload(record), ensure_ascii=False)

    def fix_goal(request):
        return request + "\n" + spec_json() + "\nReview findings JSON:\n" + json.dumps(_findings(record), ensure_ascii=False)

    def review_goal(*, rest=False):
        ids = {view["kind"]: [i["id"] for i in view["nodes"] + view["edges"]
                              if not rest or i.get("verdict", "unreviewed") == "unreviewed"]
               for view in record["views"]}
        if rest:
            ids = {kind: items for kind, items in ids.items() if items}
        request = diagram.REVIEW_REQUEST + "\nReview only the exact ids listed below.\n"
        request += "\n".join(f"return exactly {len(items)} items for view {kind}"
                             for kind, items in ids.items())
        request += "\nItems to review JSON:\n" + json.dumps(ids) + "\n" + spec_json()
        if fix_resolves:
            request += ("\nthe fix says these omissions were addressed by (verify these claims):\n"
                        + json.dumps(fix_resolves, ensure_ascii=False))
        if rest:
            request += "\nExisting review findings JSON:\n" + json.dumps(_findings(record), ensure_ascii=False)
        return request

    def review_round(name):
        if not stage(name, "claude", review_goal(), review=True):
            return False
        if record["counts"]["unreviewed"]:
            return stage(name + "-rest", "claude", review_goal(rest=True), review=True)
        return True

    if not stage("spec", author, ask + "\nKinds wanted: " + ", ".join(kinds) + "\n" + diagram.SPEC_REQUEST):
        return record
    if not review_round("review"):
        return record
    if not diagram.is_clean(record):
        if not stage("fix", author, fix_goal(diagram.FIX_REQUEST)):
            return record
        if not review_round("re-review"):
            return record
        if not diagram.is_clean(record):
            if not stage("claude-fix", "claude", fix_goal(diagram.CLAUDE_FIX_REQUEST)):
                return record
    record["status"], record["reason"] = "done", ""
    save()
    return record
