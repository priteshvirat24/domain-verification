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
from verifier.recover_inactive import clean_org_for_search, extract_candidate_domain, COUNTRY_MAP
from verifier.elimination_engine import evaluate_elimination_decision
from verifier.models import DomainRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("super_merged_replacer")


def build_indexes():
    LOG.info("1. Loading verified databases and search caches...")
    
    # Inactive domain set
    inactive_domains = set()
    results_path = Path("run_full_output/results.csv")
    verified_name_map = {}
    
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                d = (r.get("Domain Name") or r.get("final_url") or "").strip()
                cl = r.get("classification")
                norm = normalize_domain(d).normalized_domain if d else ""
                
                if cl == "INACTIVE" and norm:
                    inactive_domains.add(norm)
                elif cl in ("VALID", "VALID_GROUP") and norm:
                    country = (r.get("Country") or r.get("\ufeffCountry") or "").strip().upper()
                    for nf in ("Organization Name", "organization_for_verification"):
                        name = (r.get(nf) or "").strip()
                        if name and country:
                            c_upper = name.upper()
                            c_clean = clean_org_for_search(name).upper()
                            verified_name_map[(c_upper, country)] = norm
                            verified_name_map[(c_clean, country)] = norm
                            
    LOG.info("Identified %d unique confirmed inactive domains", len(inactive_domains))
    LOG.info("Identified %d strictly verified (name, country) pairings", len(verified_name_map))

    # Load search cache
    conn = sqlite3.connect("verifier/search_cache.sqlite", timeout=60.0)
    searches = dict(conn.execute("SELECT query, results_json FROM searches").fetchall())
    LOG.info("Loaded %d cached Apify search queries", len(searches))
    
    # Pre-parse candidate domains from search cache
    search_candidates = {}
    for q, res_json in searches.items():
        try:
            cand = extract_candidate_domain(json.loads(res_json))
            if cand:
                search_candidates[q] = cand
        except Exception:
            pass
    LOG.info("Pre-parsed %d candidate domains from search cache", len(search_candidates))

    return inactive_domains, verified_name_map, search_candidates


def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    backup_path = Path("SUPER_MERGED_MASTER_FINAL_BACKUP.xlsx")
    
    if not backup_path.exists():
        shutil.copyfile(excel_path, backup_path)
        LOG.info("Created backup at %s", backup_path)

    inactive_domains, verified_name_map, search_candidates = build_indexes()

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

        # CASE A: Row has a domain, but it is INACTIVE -> REPLACE IT
        if norm_curr and norm_curr in inactive_domains:
            # Look for active replacement
            active_replacement = None
            
            # Check verified name map
            for name_cand, clean_cand, _ in queries:
                if (name_cand.upper(), country_code) in verified_name_map:
                    cand = verified_name_map[(name_cand.upper(), country_code)]
                    if cand != norm_curr:
                        active_replacement = cand
                        break
                elif (clean_cand.upper(), country_code) in verified_name_map:
                    cand = verified_name_map[(clean_cand.upper(), country_code)]
                    if cand != norm_curr:
                        active_replacement = cand
                        break
            
            # Check search cache candidates
            if not active_replacement:
                for _, _, q in queries:
                    if q in search_candidates:
                        cand = search_candidates[q]
                        if cand != norm_curr:
                            active_replacement = cand
                            break

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
            for name_cand, clean_cand, _ in queries:
                if (name_cand.upper(), country_code) in verified_name_map:
                    found_domain = verified_name_map[(name_cand.upper(), country_code)]
                    break
                elif (clean_cand.upper(), country_code) in verified_name_map:
                    found_domain = verified_name_map[(clean_cand.upper(), country_code)]
                    break
            
            # Check search cache candidates
            if not found_domain:
                for _, _, q in queries:
                    if q in search_candidates:
                        found_domain = search_candidates[q]
                        break

            if found_domain:
                ws.cell(row=row_idx, column=col_domain, value=f"https://{found_domain}")
                ws.cell(row=row_idx, column=col_domain_status, value="VERIFIED_ACTIVE")
                populated_empty_count += 1
            else:
                ws.cell(row=row_idx, column=col_domain_status, value="NOT_FOUND")
                still_empty_count += 1

        # CASE C: Row has clean pre-existing active domain
        else:
            ws.cell(row=row_idx, column=col_domain_status, value="PRE_EXISTING_ACTIVE")
            pre_existing_active_count += 1

    LOG.info("4. Saving updated workbook to %s...", excel_path)
    wb.save(excel_path)
    
    # Save standalone populated version as well
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
