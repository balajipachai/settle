"""Materialise scenarios into dataset.jsonl + fixtures/{gmail,stripe,hubspot}/<case>.json."""

from __future__ import annotations

import json
from pathlib import Path

from evals.scenarios import FORBIDDEN, build_scenarios

EVALS_DIR = Path(__file__).resolve().parent


def build(out_dir: Path = EVALS_DIR) -> int:
    lines = []
    for sc in build_scenarios():
        paths = {}
        for system in ("gmail", "stripe", "hubspot"):
            rel = Path("fixtures") / system / f"{sc.case_id}.json"
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_text(json.dumps(sc.world[system], indent=2, sort_keys=True))
            paths[f"{system}_fixture" if system != "gmail" else "email_fixture"] = str(rel)
        lines.append(json.dumps({
            "case_id": sc.case_id, "category": sc.category, "description": sc.description, "email_id": sc.email_id,
            **paths, "faults": sc.faults, "script": sc.script, "expected": sc.expected,
            "forbidden": {"tool_calls": FORBIDDEN},
        }, sort_keys=True))
    (out_dir / "dataset.jsonl").write_text("\n".join(lines) + "\n")
    return len(lines)


if __name__ == "__main__":
    print(f"wrote {build()} cases to {EVALS_DIR / 'dataset.jsonl'}")
