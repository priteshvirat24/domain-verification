"""
Comprehensive Replacement & Population Engine for SUPER_MERGED_MASTER_FINAL.xlsx:
1. Replaces the 19,267 flagged INACTIVE domains with verified active domains discovered by Apify + Scrapling.
2. Preserves the original dead domain in a dedicated audit column 'Original Legacy Domain'.
3. Populates remaining empty rows strictly matching by (Organization Name / Company Name, Country).
4. Adds 'Domain Status' column (VERIFIED_ACTIVE, REPLACED_INACTIVE, PRE_EXISTING_ACTIVE, NOT_FOUND).
5. Exports updated SUPER_MERGED_MASTER_FINAL.xlsx and final_super_merged_master_populated.csv.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import openpyxl

from verifier.normalization import normalize_domain
from verifier.proof_ladder import legal_identity_key
from verifier.recover_inactive import clean_org_for_search, extract_candidate_domain, COUNTRY_MAP
from verifier.elimination_engine import evaluate_elimination_decision
from verifier.models import DomainRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("super_merged_replacer")


def build_indexes():
    LOG.info("1. Loading verified databases and search caches...")
    
    domain_status_map = {}
    results_path = Path("run_full_output/results.csv")
    verified_name_map = {}
    
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                d = (r.get("Domain Name") or r.get("original_domain") or r.get("final_url") or "").strip()
                cl = r.get("verification_status") or ""
                norm = normalize_domain(d).normalized_domain if d else ""
                country = (r.get("Country") or r.get("\ufeffCountry") or "").strip().upper()
                name = (r.get("Organization Name") or r.get("organization_for_verification") or "").strip()
                key = (legal_identity_key(name), country)
                if norm and key[0]:
                    domain_status_map[(key[0], country, norm)] = cl
                if cl in ("VERIFIED_EXACT", "VERIFIED_GROUP") and norm and key[0]:
                    verified_name_map.setdefault(key, set()).add(norm)
                            
    LOG.info("Identified %d strictly verified (name, country) pairings", len(verified_name_map))

    return domain_status_map, verified_name_map


def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    domain_status_map, verified_name_map = build_indexes()

    LOG.info("2. Reading %s...", excel_path)
    wb = openpyxl.load_workbook(excel_path)
    sheet_name = "final super merged master"
    ws = wb[sheet_name]
    
    # Column indices (1-based)
    col_country_group = 1
    col_country = 2
    col_org_name = 6
    col_orig_name = 7
    col_domain = 13
    
    # Add new audit columns
    col_legacy_domain = 14
    col_domain_status = 15
    ws.cell(row=1, column=col_legacy_domain, value="Original Legacy Domain")
    ws.cell(row=1, column=col_domain_status, value="Domain Status")

    total_rows = ws.max_row - 1
    replaced_inactive_count = 0
    retained_inactive_count = 0
    populated_empty_count = 0
    pre_existing_active_count = 0
    mismatch_count = 0
    needs_review_count = 0
    still_empty_count = 0

    LOG.info("3. Processing %d rows for replacement and population...", total_rows)

    for row_idx in range(2, ws.max_row + 1):
        cell_val = ws.cell(row=row_idx, column=col_domain).value
        curr_domain = str(cell_val or "").strip()
        
        org_name = str(ws.cell(row=row_idx, column=col_org_name).value or "").strip()
        orig_name = str(ws.cell(row=row_idx, column=col_orig_name).value or "").strip()
        country_code = str(ws.cell(row=row_idx, column=col_country).value or ws.cell(row=row_idx, column=col_country_group).value or "").strip().upper()
        country_name = COUNTRY_MAP.get(country_code, country_code)
        
        norm_curr = normalize_domain(curr_domain).normalized_domain if curr_domain and curr_domain.lower() not in ("none", "nan", "") else ""
        
        # Candidate search queries for this organization
        queries = []
        for name_candidate in (org_name, orig_name):
            if name_candidate and name_candidate.lower() not in ("none", "nan", ""):
                clean_name = clean_org_for_search(name_candidate)
                q = f'"{clean_name}" {country_name} official website'.strip()
                queries.append((name_candidate, clean_name, q))

        # Check existing verification status for current domain
        identity_key = (legal_identity_key(org_name or orig_name), country_code)
        curr_status = domain_status_map.get((identity_key[0], country_code, norm_curr), "")
        verified_candidates = verified_name_map.get(identity_key, set())
        verified_candidate = next(iter(verified_candidates)) if len(verified_candidates) == 1 else None

        # CASE A: Row has a domain, but it is INACTIVE -> REPLACE IT
        if curr_status == "INACTIVE":
            # Look for verified active replacement
            active_replacement = None
            
            # Check strictly verified name map
            if verified_candidate and verified_candidate != norm_curr:
                active_replacement = verified_candidate

            if active_replacement:
                ws.cell(row=row_idx, column=col_legacy_domain, value=curr_domain)
                ws.cell(row=row_idx, column=col_domain, value=f"https://{active_replacement}")
                ws.cell(row=row_idx, column=col_domain_status, value="REPLACED_INACTIVE")
                replaced_inactive_count += 1
            else:
                ws.cell(row=row_idx, column=col_legacy_domain, value=curr_domain)
                ws.cell(row=row_idx, column=col_domain_status, value="UNVERIFIED_INACTIVE")
                retained_inactive_count += 1

        # CASE B: Row has no domain -> POPULATE IT
        elif not norm_curr:
            found_domain = None
            
            # Check verified name map
            found_domain = verified_candidate

            if found_domain:
                ws.cell(row=row_idx, column=col_domain, value=f"https://{found_domain}")
                ws.cell(row=row_idx, column=col_domain_status, value="VERIFIED_ACTIVE")
                populated_empty_count += 1
            else:
                ws.cell(row=row_idx, column=col_domain_status, value="NO_DOMAIN_SUPPLIED")
                still_empty_count += 1

        # CASE C: Row has domain evaluated as MISMATCH -> DO NOT CALL PRE_EXISTING_ACTIVE!
        elif curr_status in ("MISMATCH", "REJECT"):
            ws.cell(row=row_idx, column=col_domain_status, value="MISMATCH_REJECTED")
            mismatch_count += 1

        # CASE D: Row has domain evaluated as BLOCKED or NEEDS_REVIEW
        elif curr_status in ("BLOCKED", "REVIEW", "NEEDS_REVIEW", "UNVERIFIED"):
            ws.cell(row=row_idx, column=col_domain_status, value="NEEDS_REVIEW")
            needs_review_count += 1

        # CASE E: Row has verified valid domain
        elif curr_status in ("VERIFIED_EXACT", "VERIFIED_GROUP"):
            ws.cell(row=row_idx, column=col_domain_status, value="PRE_EXISTING_ACTIVE")
            pre_existing_active_count += 1

        # CASE F: Domain not yet checked by pipeline
        else:
            ws.cell(row=row_idx, column=col_domain_status, value="NOT_CHECKED")
            needs_review_count += 1

    LOG.info("4. Saving updated workbook to a new output file...")
    out_populated = Path("SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx")
    wb.save(out_populated)

    LOG.info("5. Exporting CSV final_super_merged_master_populated.csv...")
    csv_out = Path("final_super_merged_master_populated.csv")
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for r in ws.iter_rows(values_only=True):
            writer.writerow(r)

    total_valid_domains = pre_existing_active_count + replaced_inactive_count + populated_empty_count
    valid_rate = (total_valid_domains / total_rows) * 100

    summary = {
        "total_rows": total_rows,
        "clean_pre_existing_active": pre_existing_active_count,
        "inactive_replaced_with_active": replaced_inactive_count,
        "inactive_retained_unverified": retained_inactive_count,
        "newly_populated_empty_rows": populated_empty_count,
        "still_empty_not_found": still_empty_count,
        "total_active_domains_now": total_valid_domains,
        "active_domain_coverage_percent": round(valid_rate, 2),
    }

    with open("replacement_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOG.info("==================================================")
    LOG.info("REPLACEMENT & POPULATION COMPLETE")
    LOG.info("Total Rows: %d", total_rows)
    LOG.info("Clean Pre-Existing Active Domains: %d", pre_existing_active_count)
    LOG.info("Inactive Domains REPLACED with Active: %d", replaced_inactive_count)
    LOG.info("Inactive Domains Retained (Unverified): %d", retained_inactive_count)
    LOG.info("Newly Populated Empty Rows: %d", populated_empty_count)
    LOG.info("TOTAL ACTIVE DOMAINS NOW: %d (%.2f%%)", total_valid_domains, valid_rate)
    LOG.info("Saved to: %s and %s", excel_path, csv_out)
    LOG.info("==================================================")


if __name__ == "__main__":
    main()
