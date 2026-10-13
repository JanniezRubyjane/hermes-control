"""Conservative, deterministic checks against facts in the shop fixture."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def check(prompt_id: str, answer: str) -> dict[str, object]:
    text = answer.lower().replace("`", "")
    failures: list[str] = []
    if prompt_id == "summary":
        for module in ("pricing", "discount", "cart"):
            if module not in text:
                failures.append(f"missing {module}")
        dependency = (r"\bpricing(?:\.py)?.{0,80}\bdiscount(?:\.py)?\b"
                      r"|\bdiscount(?:\.py)?.{0,80}\bpricing(?:\.py)?\b")
        if not re.search(dependency, text):
            failures.append("missing nearby pricing and discount identifiers")
    elif prompt_id == "check":
        for name in ("valid_rate", "total", "round_money", "checkout"):
            if name not in text:
                failures.append(f"missing untested {name}")
    elif prompt_id in ("routine", "hard"):
        failures.append("requires controller summary, verify exit, and committed code")
    elif prompt_id in ("review", "pinned"):
        if "round" not in text or "0.005" not in text:
            failures.append("missing round and 0.005")
    return {"ok": not failures, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--home", type=Path, help="override recorded evaluation home")
    args = parser.parse_args()
    from analyze import analyze
    for row in analyze(args.outdir, home=args.home):
        print(json.dumps({"id": row["id"], **row["answer"]}))


if __name__ == "__main__":
    main()
