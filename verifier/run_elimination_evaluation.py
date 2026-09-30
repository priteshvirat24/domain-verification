"""Run Elimination-First Architecture evaluation on the 4,000-row validation dataset.

Produces:
- results_elimination.csv
- evidence_elimination.jsonl
- review_elimination.csv
- summary_elimination.json
- comparison_elimination.csv
"""
import csv
import json
import random
import sqlite3
from collections import Counter
from pathlib import Path

from verifier.elimination_engine import evaluate_elimination_decision
from verifier.models import DomainRecord
from verifier.normalization import normalize_domain

CACHE_PATH = "verifier/live_validation_4000_cache.sqlite"
OLD_RESULTS_PATH = "old_results.csv"

OUTPUT_RESULTS = "results_elimination.csv"
OUTPUT_EVIDENCE = "evidence_elimination.jsonl"
OUTPUT_REVIEW = "review_elimination.csv"
OUTPUT_SUMMARY = "summary_elimination.json"
OUTPUT_COMPARISON = "comparison_elimination.csv"


def load_cached_domain(cursor, domain: str) -> DomainRecord | None:
    row = cursor.execute("SELECT payload FROM domains WHERE domain = ?", (domain,)).fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    return DomainRecord(
        domain=data.get("domain", domain),
        registered_domain=data.get("registered_domain", ""),
        requested_url=data.get("requested_url", f"https://{domain}/"),
        final_url=data.get("final_url", ""),
        final_registered_domain=data.get("final_registered_domain", ""),
        http_status=data.get("http_status", 0),
        dns_status=data.get("dns_status", "UNKNOWN"),
        https_available=data.get("https_available"),
        redirect_chain=data.get("redirect_chain", []),
        domain_active=data.get("domain_active"),
        parked=data.get("parked", False),
        blocked_reason=data.get("blocked_reason", ""),
        fetch_method=data.get("fetch_method", "NONE"),
        fetch_attempts=data.get("fetch_attempts", 0),
        checked_at=data.get("checked_at", ""),
        pages=data.get("pages", []),
        fetch_error=data.get("fetch_error", ""),
        fetch_error_type=data.get("fetch_error_type", ""),
    )


def serialize_cell(val: object) -> object:
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    return val


def main():
    print(f"Loading 4,000 rows from {OLD_RESULTS_PATH}...")
    with open(OLD_RESULTS_PATH, "r", encoding="utf-8") as f:
        old_rows = list(csv.DictReader(f))

    print(f"Loaded {len(old_rows)} rows. Connecting to cache: {CACHE_PATH}...")
    conn = sqlite3.connect(CACHE_PATH)
    cursor = conn.cursor()

    elimination_results = []
    comparison_rows = []
    evidence_entries = []
    review_rows = []

    old_counts = Counter()
    new_counts = Counter()
    transition_counts = Counter()
    transition_samples = {}

    for idx, r in enumerate(old_rows):
        org_name = r.get("Organization Name") or ""
        dom_val = r.get("Domain Name") or r.get("normalized_domain") or ""
        norm = normalize_domain(dom_val)
        domain_str = norm.normalized_domain or norm.original_domain

        old_status = r.get("verification_status") or "UNKNOWN"
        old_counts[old_status] += 1

        # Fetch domain record from sqlite
        dom_rec = load_cached_domain(cursor, domain_str)

        # Run elimination-first evaluation
        res = evaluate_elimination_decision(r, norm, dom_rec, network_healthy=True)
        new_status = res["classification"]
        new_counts[new_status] += 1

        trans_key = f"{old_status} -> {new_status}"
        transition_counts[trans_key] += 1
        transition_samples.setdefault(trans_key, []).append({
            "row_id": r.get("input_row_id") or str(idx + 1),
            "organization": org_name,
            "domain": dom_val,
            "old_status": old_status,
            "new_status": new_status,
            "reason": res["decision_reason"],
            "operator": res.get("website_identified_entity", ""),
            "brand": res.get("website_identified_brand", ""),
        })

        elimination_results.append(res)

        # Build comparison row
        comparison_rows.append({
            "input_row_id": r.get("input_row_id") or str(idx + 1),
            "Organization Name": org_name,
            "Country": r.get("Country", ""),
            "Domain Name": dom_val,
            "old_verification_status": old_status,
            "new_classification": new_status,
            "confidence": res.get("confidence", ""),
            "corporate_relationship": res.get("corporate_relationship", ""),
            "contradiction_found": res.get("contradiction_found", False),
            "contradiction_type": res.get("contradiction_type", "NONE"),
            "website_identified_entity": res.get("website_identified_entity", ""),
            "decision_reason": res.get("decision_reason", ""),
        })

        # Filter rows requiring review
        if new_status in ("REVIEW", "MISMATCH", "BLOCKED"):
            review_rows.append(res)

        # Collect evidence jsonl entries
        evidence_entries.append({
            "input_row_id": r.get("input_row_id") or str(idx + 1),
            "organization": org_name,
            "domain": dom_val,
            "classification": new_status,
            "confidence": res.get("confidence", ""),
            "website_title": res.get("website_title", ""),
            "website_identified_entity": res.get("website_identified_entity", ""),
            "website_country": res.get("website_country", ""),
            "organization_name_match": res.get("organization_name_match", ""),
            "brand_relationship": res.get("brand_relationship", ""),
            "corporate_relationship": res.get("corporate_relationship", ""),
            "contradiction_found": res.get("contradiction_found", False),
            "contradiction_type": res.get("contradiction_type", "NONE"),
            "evidence_url": res.get("evidence_url", ""),
            "evidence_text": res.get("evidence_text", ""),
            "decision_reason": res.get("decision_reason", ""),
        })

    conn.close()

    # 1. Write results_elimination.csv
    print(f"Writing {OUTPUT_RESULTS}...")
    if elimination_results:
        fieldnames = list(elimination_results[0].keys())
        with open(OUTPUT_RESULTS, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in elimination_results:
                writer.writerow({k: serialize_cell(row.get(k)) for k in fieldnames})

    # 2. Write review_elimination.csv
    print(f"Writing {OUTPUT_REVIEW}...")
    if review_rows:
        fieldnames = list(review_rows[0].keys())
        with open(OUTPUT_REVIEW, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in review_rows:
                writer.writerow({k: serialize_cell(row.get(k)) for k in fieldnames})

    # 3. Write comparison_elimination.csv
    print(f"Writing {OUTPUT_COMPARISON}...")
    if comparison_rows:
        fieldnames = list(comparison_rows[0].keys())
        with open(OUTPUT_COMPARISON, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(comparison_rows)

    # 4. Write evidence_elimination.jsonl
    print(f"Writing {OUTPUT_EVIDENCE}...")
    with open(OUTPUT_EVIDENCE, "w", encoding="utf-8") as f:
        for entry in evidence_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 5. Build summary_elimination.json
    total = len(elimination_results)
    valid_count = new_counts["VALID"] + new_counts["VALID_GROUP"]
    summary = {
        "architecture": "ELIMINATION_FIRST",
        "total_rows": total,
        "classifications": dict(new_counts),
        "old_statuses": dict(old_counts),
        "validation_rate": round(valid_count / total, 4) if total else 0.0,
        "elimination_mismatch_rate": round(new_counts["MISMATCH"] / total, 4) if total else 0.0,
        "inactive_rate": round(new_counts["INACTIVE"] / total, 4) if total else 0.0,
        "review_rate": round(new_counts["REVIEW"] / total, 4) if total else 0.0,
        "transitions": dict(transition_counts.most_common(30)),
    }
    with open(OUTPUT_SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n================ ELIMINATION ARCHITECTURE SUMMARY ================")
    print(json.dumps(summary, indent=2))

    # Print Key Transition Analysis
    key_transitions = [
        "UNVERIFIED -> VALID",
        "UNVERIFIED -> VALID_GROUP",
        "PROBABLE -> VALID",
        "BLOCKED -> VALID",
        "BLOCKED -> VALID_GROUP",
        "INACTIVE -> INACTIVE",
        "MISMATCH -> MISMATCH",
        "MISMATCH -> VALID",
    ]
    print("\n================ KEY TRANSITION SAMPLES ================")
    for kt in key_transitions:
        samples = transition_samples.get(kt, [])
        print(f"\n--- {kt} (Total: {len(samples)}) ---")
        rng = random.Random(42)
        sample_pick = rng.sample(samples, min(3, len(samples)))
        for s in sample_pick:
            print(f"  Row {s['row_id']}: Org='{s['organization']}' | Domain='{s['domain']}'")
            print(f"    Reason: {s['reason']}")


if __name__ == "__main__":
    main()
