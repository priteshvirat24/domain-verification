"""Re-evaluate the 4,000-row validation dataset using the V2 Human-Researcher decision engine."""
import csv
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from verifier.models import DomainRecord
from verifier.normalization import normalize_domain
from verifier.v2_human_decision import evaluate_human_researcher_decision

CACHE_PATH = "verifier/live_validation_4000_cache.sqlite"
OLD_RESULTS_PATH = "old_results.csv"


def load_cached_domain(cursor, domain: str) -> DomainRecord | None:
    row = cursor.execute("SELECT payload FROM domains WHERE domain = ?", (domain,)).fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    rec = DomainRecord(
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
    return rec


def main():
    print("Loading 4,000 sample rows from old_results.csv...")
    with open(OLD_RESULTS_PATH, "r", encoding="utf-8") as f:
        old_rows = list(csv.DictReader(f))

    print(f"Loaded {len(old_rows)} rows. Connecting to SQLite cache...")
    conn = sqlite3.connect(CACHE_PATH)
    cursor = conn.cursor()

    new_results = []
    transitions = []
    old_status_counter = Counter()
    new_status_counter = Counter()
    transition_counter = Counter()

    for r in old_rows:
        org_name = r.get("Organization Name") or ""
        dom_val = r.get("Domain Name") or r.get("normalized_domain") or ""
        norm = normalize_domain(dom_val)
        domain_str = norm.normalized_domain or norm.original_domain

        old_status = r.get("verification_status") or "UNKNOWN"
        old_conf = r.get("confidence") or "LOW"
        old_status_counter[old_status] += 1

        # Fetch domain record from cache
        dom_rec = load_cached_domain(cursor, domain_str)

        # Evaluate V2 Human-Researcher decision
        res = evaluate_human_researcher_decision(r, norm, dom_rec, network_healthy=True)
        new_status = res["verification_status"]
        new_conf = res["confidence"]
        new_status_counter[new_status] += 1

        transition_key = f"{old_status} -> {new_status}"
        transition_counter[transition_key] += 1

        # Record transition
        reason_change = ""
        if old_status != new_status:
            reason_change = f"Upgraded from {old_status} ({old_conf}) to {new_status} ({new_conf}) via multi-signal human researcher evaluation: {res['verification_reason']}"
        else:
            reason_change = "Status maintained"

        transitions.append({
            "organization": org_name,
            "domain": domain_str,
            "old_status": old_status,
            "new_status": new_status,
            "old_confidence": old_conf,
            "new_confidence": new_conf,
            "confidence_score": res["confidence_score"],
            "verification_level": res["verification_level"],
            "relationship_type": res["entity_relationship_type"],
            "evidence_count": res["evidence_count"],
            "reason_for_change": reason_change
        })

        new_results.append(res)

    conn.close()

    print("\n--- CLASSIFICATION COMPARISON ---")
    print(f"Total Rows: {len(new_results)}")
    print(f"Old Statuses: {dict(old_status_counter)}")
    print(f"New Statuses: {dict(new_status_counter)}")

    # Write results_v2.csv and results.csv
    fieldnames = list(new_results[0].keys())
    for filename in ("results_v2.csv", "results.csv"):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(new_results)
        print(f"Wrote {filename}")

    # Write review_v2.csv and review.csv
    review_rows = [r for r in new_results if r["verification_status"] in ("BLOCKED", "PROBABLE", "UNVERIFIED", "REDIRECT")]
    for filename in ("review_v2.csv", "review.csv"):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(review_rows)
        print(f"Wrote {filename} ({len(review_rows)} rows)")

    # Write comparison.csv and status_transition.csv
    trans_fieldnames = list(transitions[0].keys())
    for filename in ("comparison.csv", "status_transition.csv"):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=trans_fieldnames)
            writer.writeheader()
            writer.writerows(transitions)
        print(f"Wrote {filename}")

    # Write evidence_v2.jsonl and evidence.jsonl
    for filename in ("evidence_v2.jsonl", "evidence.jsonl"):
        with open(filename, "w", encoding="utf-8") as f:
            for r in new_results:
                if r.get("evidence_json"):
                    entry = {
                        "input_row_id": r.get("input_row_id"),
                        "organization": r.get("Organization Name"),
                        "domain": r.get("normalized_domain"),
                        "status": r.get("verification_status"),
                        "confidence_score": r.get("confidence_score"),
                        "level": r.get("verification_level"),
                        "evidence": r.get("evidence_json"),
                    }
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"Wrote {filename}")

    # Summary statistics
    verified_total = sum(new_status_counter[s] for s in ("VERIFIED_EXACT", "VERIFIED_ENTITY", "VERIFIED_GROUP", "STRONG_MATCH"))
    summary_v2 = {
        "total_rows": len(new_results),
        "verified_exact": new_status_counter["VERIFIED_EXACT"],
        "verified_entity": new_status_counter["VERIFIED_ENTITY"],
        "verified_group": new_status_counter["VERIFIED_GROUP"],
        "strong_match": new_status_counter["STRONG_MATCH"],
        "probable": new_status_counter["PROBABLE"],
        "unverified": new_status_counter["UNVERIFIED"],
        "mismatch": new_status_counter["MISMATCH"],
        "inactive": new_status_counter["INACTIVE"],
        "redirect": new_status_counter["REDIRECT"],
        "blocked": new_status_counter["BLOCKED"],
        "total_verified_or_strong": verified_total,
        "verified_or_strong_rate": round(verified_total / len(new_results), 4),
        "review_required_rate": round(len(review_rows) / len(new_results), 4),
        "old_distribution": dict(old_status_counter),
        "new_distribution": dict(new_status_counter),
        "transition_matrix": dict(transition_counter.most_common(30))
    }

    for filename in ("summary_v2.json", "summary.json"):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(summary_v2, f, indent=2)
        print(f"Wrote {filename}")

    with open("classification_comparison.json", "w", encoding="utf-8") as f:
        json.dump({
            "old_counts": dict(old_status_counter),
            "new_counts": dict(new_status_counter),
            "transitions": dict(transition_counter)
        }, f, indent=2)
    print("Wrote classification_comparison.json")


if __name__ == "__main__":
    main()
