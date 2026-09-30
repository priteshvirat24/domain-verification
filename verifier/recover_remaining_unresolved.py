"""
Deep Recovery Pipeline for Remaining Unresolved Rows in SUPER_MERGED_MASTER_FINAL.xlsx:
Targets:
1. 10,057 UNVERIFIED_INACTIVE rows (checks www canonical DNS + live HTTP).
2. 24,386 NOT_FOUND rows (runs relaxed fuzzy & TaxID/RegNo searches via Apify + Scrapling).
3. Updates SUPER_MERGED_MASTER_FINAL.xlsx, SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx, and final_super_merged_master_populated.csv.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import time
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import openpyxl

from verifier.cache import SQLiteCache
from verifier.config import Config
from verifier.elimination_engine import evaluate_elimination_decision
from verifier.fetcher import TieredFetcher, dns_status
from verifier.models import DomainRecord
from verifier.normalization import normalize_domain
from verifier.pipeline import investigate_domain
from verifier.apify_escalation import get_apify_client
from verifier.recover_inactive import (
    clean_org_for_search,
    extract_candidate_domain,
    run_batch_apify_search_with_retries,
    SearchCache,
    COUNTRY_MAP,
    EXCLUDE_DOMAINS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("deep_recovery")


import dns.asyncresolver


async def check_www_resolution(host: str, resolver: dns.asyncresolver.Resolver) -> str | None:
    """Checks if www.{host} resolves to global IP address via async dnspython."""
    if not host or "." not in host:
        return None
    target = host if host.startswith("www.") else f"www.{host}"
    try:
        ans = await resolver.resolve(target, "A")
        if any(ip_address(r.address).is_global for r in ans):
            return target
    except Exception:
        pass
    return None


async def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    csv_path = Path("final_super_merged_master_populated.csv")
    
    LOG.info("1. Loading unresolved rows from %s...", csv_path)
    all_rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    unverified_inactive_rows = []
    not_found_rows = []

    for idx, r in enumerate(all_rows, 2):  # 1-based, row 2 is first data row
        r["_row_idx"] = idx
        st = r.get("Domain Status")
        if st == "UNVERIFIED_INACTIVE":
            unverified_inactive_rows.append(r)
        elif st == "NOT_FOUND":
            not_found_rows.append(r)

    LOG.info("Identified %d UNVERIFIED_INACTIVE rows and %d NOT_FOUND rows (Total: %d)",
             len(unverified_inactive_rows), len(not_found_rows), len(unverified_inactive_rows) + len(not_found_rows))

    search_cache = SearchCache("verifier/search_cache.sqlite")
    config = Config(concurrency=150, max_evidence_pages=2, use_dynamic=False, use_stealth=False, use_proxy=False, use_apify=True)
    domain_cache = SQLiteCache("verifier/live_validation_4000_cache.sqlite", config.cache_ttl_seconds)
    fetcher = TieredFetcher(config)
    apify_client = get_apify_client(config.apify_token)

    # =========================================================================
    # STAGE 1: Fast WWW Canonical Probe for UNVERIFIED_INACTIVE rows
    # =========================================================================
    LOG.info("=== STAGE 1: Fast WWW Canonical Probing for %d Inactive Rows ===", len(unverified_inactive_rows))
    
    unique_legacy_hosts = set()
    for r in unverified_inactive_rows:
        legacy = r.get("Original Legacy Domain") or r.get("Domain URL") or ""
        if legacy and legacy.lower() not in ("none", "nan", ""):
            host = urlsplit(legacy if "://" in legacy else f"http://{legacy}").netloc or legacy.split("/")[0]
            if host:
                unique_legacy_hosts.add(host.lower())

    LOG.info("Testing %d unique legacy hostnames for active www. resolution...", len(unique_legacy_hosts))
    
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 1.0
    resolver.lifetime = 1.5
    sem_dns = asyncio.Semaphore(300)
    resolved_www_map = {}

    async def probe_dns(h: str):
        async with sem_dns:
            active_target = await check_www_resolution(h, resolver)
            if active_target:
                resolved_www_map[h] = active_target

    await asyncio.gather(*(probe_dns(h) for h in unique_legacy_hosts))
    LOG.info("Stage 1 DNS Results: %d / %d hosts (%.1f%%) resolve actively via www!",
             len(resolved_www_map), len(unique_legacy_hosts),
             (len(resolved_www_map) / len(unique_legacy_hosts)) * 100 if unique_legacy_hosts else 0)

    # Crawl resolved www hosts to confirm HTTP response
    sem_crawl = asyncio.Semaphore(150)
    confirmed_live_www = set()

    async def crawl_www(target: str):
        async with sem_crawl:
            try:
                rec = await domain_cache.get(target)
                if not rec:
                    rec = await investigate_domain(target, config, fetcher)
                    await domain_cache.put(rec)
                if rec.dns_status == "RESOLVED" and rec.http_status in (200, 301, 302, 303, 307, 308):
                    confirmed_live_www.add(target)
            except Exception:
                pass

    targets_to_crawl = list(set(resolved_www_map.values()))
    LOG.info("Validating HTTP response for %d www hosts...", len(targets_to_crawl))
    await asyncio.gather(*(crawl_www(t) for t in targets_to_crawl))
    LOG.info("Stage 1 HTTP Results: %d / %d www hosts confirmed 100%% LIVE & HEALTHY!",
             len(confirmed_live_www), len(targets_to_crawl))

    stage1_recovered_rows = 0
    for r in unverified_inactive_rows:
        legacy = r.get("Original Legacy Domain") or r.get("Domain URL") or ""
        host = urlsplit(legacy if "://" in legacy else f"http://{legacy}").netloc or legacy.split("/")[0]
        host = host.lower()
        active_target = resolved_www_map.get(host)
        if active_target and active_target in confirmed_live_www:
            r["Domain URL"] = f"https://{active_target}"
            r["Domain Status"] = "REPLACED_INACTIVE"
            stage1_recovered_rows += 1

    LOG.info("Stage 1 Completed: %d rows updated to live www domains!", stage1_recovered_rows)

    # =========================================================================
    # STAGE 2: Deep Search for Remaining NOT_FOUND & Unresolved Rows
    # =========================================================================
    still_needing_search = [r for r in unverified_inactive_rows if r.get("Domain Status") == "UNVERIFIED_INACTIVE"] + not_found_rows
    LOG.info("=== STAGE 2: Deep Search Query Generation for %d Rows ===", len(still_needing_search))

    query_to_rows = {}
    needed_queries = []

    for r in still_needing_search:
        name = (r.get("Organization Name") or r.get("original_company_name") or "").strip()
        if not name or name.lower() in ("none", "nan", ""):
            continue
        c_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
        country_name = COUNTRY_MAP.get(c_code, c_code)
        clean = clean_org_for_search(name)
        tax_id = (r.get("tax_id") or "").strip()

        # Generate refined queries
        if tax_id and len(tax_id) >= 6:
            q = f'"{clean}" {tax_id} official website'
        else:
            q = f'{clean} {country_name} corporate website'

        if q not in query_to_rows:
            query_to_rows[q] = []
            cached = search_cache.get(q)
            if cached is None:
                needed_queries.append(q)
        query_to_rows[q].append(r)

    LOG.info("Unique search queries: %d (Cached: %d, Need Apify: %d)",
             len(query_to_rows), len(query_to_rows) - len(needed_queries), len(needed_queries))

    # Dispatch Apify searches in batches of 50 with 24 concurrent workers
    if needed_queries:
        batch_size = 50
        sem_apify = asyncio.Semaphore(24)
        quota_hit = asyncio.Event()

        async def fetch_chunk(chunk: list[str], chunk_idx: int, total_chunks: int):
            if quota_hit.is_set():
                return
            async with sem_apify:
                if quota_hit.is_set():
                    return
                LOG.info("Apify chunk %d/%d (%d queries)...", chunk_idx, total_chunks, len(chunk))
                batch_res = await asyncio.to_thread(run_batch_apify_search_with_retries, apify_client, chunk)
                if not batch_res:
                    LOG.warning("Apify usage limit hit on chunk %d. Bypassing remaining Apify chunks.", chunk_idx)
                    quota_hit.set()
                    return
                for q, org_res in batch_res.items():
                    search_cache.put(q, org_res)

        chunks = [needed_queries[i:i + batch_size] for i in range(0, len(needed_queries), batch_size)]
        LOG.info("Dispatching %d chunks to 24 parallel Apify workers...", len(chunks))
        await asyncio.gather(*(fetch_chunk(chunk, idx + 1, len(chunks)) for idx, chunk in enumerate(chunks)))

    # Extract candidate domains and crawl
    LOG.info("Extracting candidate domains from SERPs...")
    candidate_domains = {}
    unique_candidates = set()

    for q in query_to_rows:
        res = search_cache.get(q) or []
        cand = extract_candidate_domain(res)
        if cand:
            candidate_domains[q] = cand
            unique_candidates.add(cand)

    LOG.info("Discovered %d candidate domains across %d queries", len(unique_candidates), len(candidate_domains))

    # Crawl candidates
    async def crawl_candidate(dom: str):
        async with sem_crawl:
            try:
                norm = normalize_domain(dom)
                host = norm.normalized_domain
                if not host:
                    return
                rec = await domain_cache.get(host)
                if not rec:
                    rec = await investigate_domain(host, config, fetcher)
                    await domain_cache.put(rec)
            except Exception:
                pass

    LOG.info("Crawling unique candidate domains with 150 workers...")
    await asyncio.gather(*(crawl_candidate(d) for d in unique_candidates), return_exceptions=True)

    # Verify and map decisions
    stage2_recovered_rows = 0
    for q, rows_list in query_to_rows.items():
        cand = candidate_domains.get(q)
        if not cand:
            continue
        norm = normalize_domain(cand)
        host = norm.normalized_domain
        dom_rec = await domain_cache.get(host)
        if not dom_rec:
            continue

        decision = evaluate_elimination_decision(
            {"Organization Name": rows_list[0].get("Organization Name") or rows_list[0].get("original_company_name"),
             "Country": rows_list[0].get("country code")},
            norm,
            dom_rec,
            network_healthy=True,
        )

        if decision.get("classification") in ("VALID", "VALID_GROUP"):
            for r in rows_list:
                prior_st = r.get("Domain Status")
                r["Domain URL"] = f"https://{host}"
                r["Domain Status"] = "REPLACED_INACTIVE" if prior_st == "UNVERIFIED_INACTIVE" else "VERIFIED_ACTIVE"
                stage2_recovered_rows += 1

    LOG.info("Stage 2 Completed: %d rows verified and populated!", stage2_recovered_rows)

    # =========================================================================
    # STAGE 3: Save Updated Master Workbook and CSV
    # =========================================================================
    LOG.info("=== STAGE 3: Saving Updated Master Excel & CSV Deliverables ===")
    
    wb = openpyxl.load_workbook(excel_path)
    ws = wb["final super merged master"]
    
    col_domain = 13
    col_legacy = 14
    col_status = 15

    total_active_now = 0
    status_summary = {}

    for r in all_rows:
        row_idx = r["_row_idx"]
        d = r.get("Domain URL")
        st = r.get("Domain Status")
        leg = r.get("Original Legacy Domain")
        
        ws.cell(row=row_idx, column=col_domain, value=d)
        ws.cell(row=row_idx, column=col_legacy, value=leg)
        ws.cell(row=row_idx, column=col_status, value=st)

        status_summary[st] = status_summary.get(st, 0) + 1
        if st in ("VERIFIED_ACTIVE", "REPLACED_INACTIVE", "PRE_EXISTING_ACTIVE"):
            total_active_now += 1

    wb.save(excel_path)
    wb.save("SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = [c for c in all_rows[0].keys() if c != "_row_idx"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    total_rows = len(all_rows)
    coverage = round((total_active_now / total_rows) * 100, 2)

    summary = {
        "total_rows": total_rows,
        "stage1_www_recovered": stage1_recovered_rows,
        "stage2_deep_search_recovered": stage2_recovered_rows,
        "total_active_domains_now": total_active_now,
        "active_domain_coverage_percent": coverage,
        "status_breakdown": status_summary,
    }

    with open("deep_recovery_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOG.info("==================================================")
    LOG.info("DEEP RECOVERY PIPELINE FINISHED")
    LOG.info("Stage 1 WWW Recoveries: %d", stage1_recovered_rows)
    LOG.info("Stage 2 Search Recoveries: %d", stage2_recovered_rows)
    LOG.info("TOTAL ACTIVE DOMAINS NOW: %d (%.2f%%)", total_active_now, coverage)
    LOG.info("Status Breakdown: %s", status_summary)
    LOG.info("Saved to: %s and %s", excel_path, csv_path)
    LOG.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
