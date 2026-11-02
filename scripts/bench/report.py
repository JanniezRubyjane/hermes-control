"""Outcome summaries and paired regression comparisons (stdlib only)."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.results import read_results

GROUP = ("arm", "engine", "model", "effort")


def wilson_ci(solved: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    if not 0 <= solved <= n:
        raise ValueError("solved: must be between zero and n")
    z = 1.959963984540054
    p = solved / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial test of the discordant paired outcomes."""
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be nonnegative")
    n = b + c
    if not n:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n)


def _numbers(rows, key):
    return [r[key] for r in rows if isinstance(r.get(key), (int, float))
            and not isinstance(r[key], bool) and math.isfinite(r[key])]


def _median(values):
    return statistics.median(values) if values else None


def _p90(values):
    # Nearest-rank percentile, including for small samples.
    return sorted(values)[math.ceil(.9 * len(values)) - 1] if values else None


def _solved(row):
    return bool(row.get("solved")) and row.get("status") == "graded"


def _token_total(row):
    tokens = row.get("tokens") or {}
    if "total_tokens" in tokens:
        return tokens["total_tokens"]
    values = [v for k, v in tokens.items() if k in {"input_tokens", "output_tokens"}
              and isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(values) if values else None


def summarize(results) -> list[dict]:
    groups = defaultdict(list)
    for row in results:
        groups[tuple(row.get(k) for k in GROUP)].append(row)
    summaries = []
    for key, rows in sorted(groups.items(), key=lambda item: tuple(str(k) for k in item[0])):
        graded = [r for r in rows if r.get("status") == "graded"]
        n = len(graded)
        solved = sum(_solved(r) for r in graded)
        false_green = sum(bool(r.get("verify_green")) and not _solved(r) for r in graded)
        tokens = [total for r in rows if isinstance(total := _token_total(r), (int, float))]
        summaries.append({
            **dict(zip(GROUP, key)), "n": n, "solved": solved,
            "solved_pct": 100 * solved / n if n else None,
            "solved_rate": solved / n if n else None,
            "wilson_ci": wilson_ci(solved, n),
            "false_green": false_green, "false_green_rate": false_green / n if n else None,
            "median_wall_s": _median(_numbers(rows, "wall_s")),
            "p90_wall_s": _p90(_numbers(rows, "wall_s")),
            "median_worker_s": _median(_numbers(rows, "worker_s")),
            "median_tokens": _median(tokens),
            "gate_violations": sum(len(r.get("gate_violations") or []) for r in rows),
            "void": sum(r.get("status") == "void" for r in rows),
            "not_graded": sum(r.get("status") == "not-graded" for r in rows),
            "engine_error": sum(r.get("status") == "engine-error" for r in rows),
        })
    return summaries


def _comparison(baseline, current):
    def majority(rows):
        grouped = defaultdict(list)
        for row in rows:
            if row.get("status") != "graded":
                continue
            grouped[tuple(row.get(k) for k in ("task_id", *GROUP))].append(_solved(row))
        # A tie does not demonstrate a solved task.
        return {key: sum(values) > len(values) / 2 for key, values in grouped.items()}

    before, after = majority(baseline), majority(current)
    paired = before.keys() & after.keys()
    lost = sum(before[k] and not after[k] for k in paired)
    gained = sum(after[k] and not before[k] for k in paired)
    n = len(paired)
    before_rate = sum(before[k] for k in paired) / n if n else None
    after_rate = sum(after[k] for k in paired) / n if n else None
    p = mcnemar_exact(lost, gained)
    before_wall = _median(_numbers(baseline, "wall_s"))
    after_wall = _median(_numbers(current, "wall_s"))
    slower = (before_wall is not None and after_wall is not None
              and after_wall > before_wall * 1.25)
    gates = sum(len(r.get("gate_violations") or []) for r in current)
    ungraded = bool(current) and all(r.get("status") in {"not-graded", "engine-error"}
                                     for r in current)
    return {"paired": n, "baseline_solved_rate": before_rate,
            "current_solved_rate": after_rate, "lost": lost, "gained": gained,
            "p_value": p, "gate_violations": gates,
            "baseline_median_wall_s": before_wall, "current_median_wall_s": after_wall,
            "wall_regression": slower, "ungraded": ungraded,
            "regression": bool((n and after_rate < before_rate and p < .1)
                               or gates or slower or ungraded)}


def compare(baseline, current) -> dict:
    baseline, current = list(baseline), list(current)
    result = _comparison(baseline, current)
    by_group = []
    for key in sorted({tuple(r.get(k) for k in GROUP) for r in current}, key=str):
        select = lambda rows: [r for r in rows if tuple(r.get(k) for k in GROUP) == key]
        by_group.append({**dict(zip(GROUP, key)), **_comparison(select(baseline), select(current))})
    result["groups"] = by_group
    result["regression"] |= any(g["regression"] for g in by_group)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = read_results(args.results)
    output = {"groups": summarize(rows), "malformed_lines": rows.malformed_lines}
    if args.baseline:
        baseline = read_results(args.baseline)
        output["comparison"] = compare(baseline, rows)
        output["baseline_malformed_lines"] = baseline.malformed_lines
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("arm engine model effort | n solved % [95% CI] | false-green rate | wall median/p90 | worker | tokens | gates void not-graded engine-error")
        def fmt(value):
            return "?" if value is None else f"{value:.2f}"
        for g in output["groups"]:
            lo, hi = g["wilson_ci"]
            print(f"{' '.join(str(g[k]) for k in GROUP)} | {g['n']} {g['solved']} "
                  f"{fmt(g['solved_pct'])}% [{100*lo:.1f},{100*hi:.1f}] | "
                  f"{g['false_green']} {fmt(100*g['false_green_rate'] if g['n'] else None)}% | "
                  f"{fmt(g['median_wall_s'])}/{fmt(g['p90_wall_s'])} | "
                  f"{fmt(g['median_worker_s'])} | {fmt(g['median_tokens'])} | "
                  f"{g['gate_violations']} {g['void']} {g['not_graded']} {g['engine_error']}")
        if "comparison" in output:
            c = output["comparison"]
            print(f"paired={c['paired']} lost={c['lost']} gained={c['gained']} "
                  f"McNemar p={c['p_value']:.6g} regression={c['regression']}")
        print(f"malformed lines skipped: {output['malformed_lines']}" +
              (f" (baseline: {output['baseline_malformed_lines']})" if args.baseline else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
