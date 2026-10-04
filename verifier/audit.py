"""Score a hand-labeled verification audit without treating missing labels as correct."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

VALID_LABELS = {"ACCEPT", "REJECT", "NEEDS_REVIEW"}


def score_audit(path: str | Path) -> dict:
    buckets: dict[str, list[bool]] = defaultdict(list)
    labeled = 0
    unlabeled = 0
    with Path(path).open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            human = str(row.get("human_decision") or "").strip().upper()
            if not human:
                unlabeled += 1
                continue
            if human not in VALID_LABELS:
                raise ValueError(f"Invalid human_decision {human!r} on input row {row.get('input_row_id')}")
            predicted = str(row.get("decision") or "").strip().upper()
            if predicted not in VALID_LABELS:
                raise ValueError(f"Invalid predicted decision {predicted!r} on input row {row.get('input_row_id')}")
            correct = human == predicted
            labeled += 1
            buckets["overall"].append(correct)
            buckets[f"status:{row.get('verification_status') or ''}"].append(correct)
            buckets[f"proof:{row.get('proof_level') or ''}"].append(correct)
    return {
        "labeled_rows": labeled,
        "unlabeled_rows": unlabeled,
        "accuracy": sum(buckets["overall"]) / labeled if labeled else None,
        "by_rule": {name: {"n": len(values), "accuracy": sum(values) / len(values),
                            "meets_95_percent": len(values) >= 20 and sum(values) / len(values) >= 0.95}
                    for name, values in sorted(buckets.items()) if name != "overall"},
        "note": "Rule gates require at least 20 labeled rows; this is sample accuracy, not a population guarantee.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score completed human verification audit")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = score_audit(args.input)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
