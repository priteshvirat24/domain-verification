"""Local result, evidence, review, and quality-summary exports."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .models import STATUSES

APPENDED_COLUMNS = [
    "input_row_id", "classification", "confidence", "original_domain", "normalized_domain", "registered_domain", "requested_url",
    "final_url", "final_registered_domain", "http_status", "dns_status", "https_available",
    "redirect_chain", "domain_active", "website_title", "homepage_h1", "website_h1", "website_description", "business_activity",
    "website_identified_entity", "website_identified_brand", "website_identified_group", "website_country",
    "website_address", "website_phone", "website_email_domain", "organization_names_found", "legal_names_found",
    "registration_numbers", "canonical_domain", "redirect_destination",
    "search_association", "organization_name_match", "brand_relationship", "country_consistency",
    "address_consistency", "phone_consistency", "email_consistency", "business_activity_consistency",
    "corporate_relationship", "contradiction_found", "contradiction_type",
    "detected_company_name", "detected_legal_entity", "detected_parent_company", "detected_brand",
    "detected_country", "exact_entity_match", "corporate_group_match", "evidence_url_1",
    "evidence_url_2", "evidence_type", "evidence_text", "evidence_json", "evidence_reason", "verification_status",
    "destination_verification_status", "verification_reason", "decision_reason", "fetch_method",
    "fetch_attempts", "blocked_reason", "checked_at",
]
REVIEW_PRIORITY = {"MISMATCH": 0, "REVIEW": 1, "PROBABLE": 2, "UNVERIFIED": 3, "BLOCKED": 4, "REDIRECT": 5}


def _csv_value(value: object) -> object:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value


def write_outputs(rows: list[dict], output: str | Path, review: str | Path,
                  evidence_path: str | Path, summary_path: str | Path,
                  unique_domains: int) -> dict:
    original_columns = [k for k in rows[0] if k not in APPENDED_COLUMNS] if rows else []
    columns = original_columns + [k for k in APPENDED_COLUMNS if k not in original_columns]
    for path in (output, review):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _csv_value(r.get(k)) for k in columns} for r in rows)
    review_rows = [r for r in rows if (r.get("classification") or r.get("verification_status")) in REVIEW_PRIORITY]
    review_rows.sort(key=lambda r: (REVIEW_PRIORITY.get(r.get("classification") or r.get("verification_status"), 99), r.get("input_row_id", 0)))
    with open(review, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns + ["unresolved_issue"], extrasaction="ignore")
        writer.writeheader()
        for row in review_rows:
            issue = row.get("decision_reason") or row.get("verification_reason") or "Confirm the legal/corporate relationship with an authoritative source."
            writer.writerow({**{k: _csv_value(row.get(k)) for k in columns}, "unresolved_issue": issue})
    with open(evidence_path, "w", encoding="utf-8") as file:
        for row in rows:
            for index, item in enumerate(row.get("evidence_json", [])):
                record = {
                    "input_row_id": row.get("input_row_id"),
                    "organization": row.get("Organization Name", ""),
                    "organization_id": row.get("Organization ID", ""),
                    "original_domain": row.get("original_domain", ""),
                    "normalized_domain": row.get("normalized_domain", ""),
                    "final_url": row.get("final_url", ""),
                    "verification_status": row.get("verification_status", ""),
                    "confidence": row.get("confidence", ""),
                    "reason": row.get("verification_reason", ""),
                    "evidence_url": item.get("url", ""),
                    "evidence_type": item.get("evidence_type", ""),
                    "evidence_text": item.get("text", ""),
                    "relationship": item.get("relationship", ""),
                    "strength": item.get("strength", ""),
                    "source_title": item.get("title", ""),
                    "source_type": item.get("source_type", "website"),
                    "fetch_method": row.get("fetch_method", ""),
                    "checked_timestamp": row.get("checked_at", ""),
                    "evidence_index": index,
                }
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
    status_keys = [r.get("classification") or r.get("verification_status") for r in rows]
    counts = Counter(k for k in status_keys if k)
    total = len(rows)
    def _dom(r: dict) -> str:
        return r.get("normalized_domain") or r.get("canonical_domain") or ""

    all_domains = [_dom(r) for r in rows if _dom(r)]
    domain_freq = Counter(all_domains)
    duplicate_domain_count = sum(1 for d in all_domains if domain_freq[d] > 1)

    fetched_domains = {_dom(r) for r in rows if r.get("http_status") and _dom(r)}
    failed_domains = {_dom(r) for r in rows if _dom(r) and
                      not r.get("http_status") and (r.get("fetch_attempts") or r.get("dns_status") in ("FAILED", "UNSAFE_ADDRESS"))}
    not_attempted = {_dom(r) for r in rows if _dom(r) and
                     not r.get("http_status") and not r.get("fetch_attempts") and
                     r.get("dns_status") not in ("FAILED", "UNSAFE_ADDRESS")}
    verified = (
        counts.get("VALID", 0)
        + counts.get("VALID_GROUP", 0)
        + counts.get("VERIFIED_EXACT", 0)
        + counts.get("VERIFIED_ENTITY", 0)
        + counts.get("VERIFIED_GROUP", 0)
        + counts.get("STRONG_MATCH", 0)
    )

    rows_with_authoritative = sum(
        any(item.get("strength") in ("STRONG", "VERY_STRONG") for item in r.get("evidence_json", []))
        for r in rows
    )
    rows_with_external = sum(
        any(item.get("source_type") == "external" for item in r.get("evidence_json", []))
        for r in rows
    )
    rows_with_website_only = sum(
        bool(r.get("evidence_json")) and not any(item.get("source_type") == "external" for item in r.get("evidence_json", []))
        for r in rows
    )

    summary = {
        "total_rows": total,
        "unique_domains": unique_domains,
        "fetched_domains": len(fetched_domains),
        "failed_domains": len(failed_domains),
        "domains_successfully_fetched": len(fetched_domains),
        "domains_failed": len(failed_domains),
        "domains_not_attempted": len(not_attempted),
        **{status.lower(): counts[status] for status in STATUSES},
        "duplicate_domain_count": duplicate_domain_count,
        "fetch_success_rate": len(fetched_domains) / (len(fetched_domains) + len(failed_domains)) if fetched_domains or failed_domains else None,
        "verification_rate": verified / total if total else 0.0,
        "percentage_requiring_manual_review": round((len(review_rows) / total * 100), 2) if total else 0.0,
        "percentage_with_authoritative_evidence": round((rows_with_authoritative / total * 100), 2) if total else 0.0,
        "percentage_classified_only_from_website_evidence": round((rows_with_website_only / total * 100), 2) if total else 0.0,
        "percentage_classified_using_external_evidence": round((rows_with_external / total * 100), 2) if total else 0.0,
        "high_confidence_rate": sum(r["confidence"] == "HIGH" for r in rows) / total if total else 0.0,
        "ground_truth_accuracy": None,
    }
    Path(summary_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
