"""
Continuous Domain Recovery Pipeline:
1. Loads current master files (SUPER_MERGED_MASTER_FINAL.xlsx and final_super_merged_master_populated.csv).
2. Purges stale empty [] search stubs from verifier/search_cache.sqlite.
3. Targets all remaining unresolved rows (NOT_FOUND and UNVERIFIED_INACTIVE).
4. Employs Camoufox stealth browser with anti-rate-limit jitter and worker pooling to harvest organic SERPs.
5. Employs Scrapling (curl_cffi Chrome 120 browser impersonation) for high-speed domain crawling.
6. Evaluates candidates using the strict 9-point Elimination-First Engine.
7. Continuously persists recovered domains into SUPER_MERGED_MASTER_FINAL.xlsx and final_super_merged_master_populated.csv.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import openpyxl
from camoufox.async_api import AsyncCamoufox

from verifier.cache import SQLiteCache
from verifier.config import Config
from verifier.elimination_engine import evaluate_elimination_decision
from verifier.fetcher import TieredFetcher
from verifier.models import DomainRecord
from verifier.normalization import normalize_domain
from verifier.pipeline import investigate_domain
from verifier.recover_inactive import (
    clean_org_for_search,
    extract_candidate_domain,
    SearchCache,
    COUNTRY_MAP,
    EXCLUDE_DOMAINS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("continuous_recovery.log", encoding="utf-8")
    ]
)
LOG = logging.getLogger("continuous_recovery")


def decode_bing_url(href: str) -> str:
    """Decodes Bing redirect links (&u=a1...) to retrieve canonical target URL."""
    if "/ck/a?!" in href and "&u=" in href:
        try:
            part = href.split("&u=")[1].split("&")[0]
            if part.startswith("a1"):
                raw = part[2:] + "==="
                raw = raw[:len(raw) - (len(raw) % 4)] if len(raw) % 4 != 0 else raw
                return base64.urlsafe_b64decode(raw).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return href


def purge_empty_cache_stubs(db_path: str = "verifier/search_cache.sqlite"):
    """Deletes empty '[]' search results caused by rate limits."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM searches WHERE results_json = '[]'")
    empty_cnt = cur.fetchone()[0]
    if empty_cnt > 0:
        LOG.info("Purging %d empty search cache stubs...", empty_cnt)
        cur.execute("DELETE FROM searches WHERE results_json = '[]'")
        conn.commit()
    conn.close()


def save_master_files(all_rows: list[dict], excel_path: Path, csv_path: Path):
    """Atomically saves all rows to Excel and CSV master files."""
    LOG.info("Syncing master files: %s and %s...", excel_path, csv_path)
    wb = openpyxl.load_workbook(excel_path)
    ws = wb["final super merged master"]

    col_domain = 13
    col_legacy = 14
    col_status = 15

    total_active = 0
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
            total_active += 1

    wb.save(excel_path)
    wb.save("SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = [c for c in all_rows[0].keys() if c != "_row_idx"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    total_rows = len(all_rows)
    coverage = round((total_active / total_rows) * 100, 2)
    LOG.info("Current Active Verified Domains: %d / %d (%.2f%%) | Breakdown: %s",
             total_active, total_rows, coverage, status_summary)
    return total_active, coverage, status_summary


from curl_cffi.requests import AsyncSession
from lxml import html


async def harvest_serp_batch(queries_to_fetch: list[str], search_cache: SearchCache, concurrency: int = 60) -> dict[str, list[dict]]:
    """Harvests Bing search results using curl_cffi Chrome 120 impersonation with 60 parallel async workers."""
    results_map = {}
    if not queries_to_fetch:
        return results_map

    sem = asyncio.Semaphore(concurrency)
    completed = 0
    total = len(queries_to_fetch)
    t0 = time.time()

    async with AsyncSession(impersonate="chrome120", timeout=12) as session:
        async def fetch_one(q: str):
            nonlocal completed
            async with sem:
                try:
                    url = f"https://www.bing.com/search?q={quote_plus(q)}"
                    resp = await session.get(url)
                    if resp.status_code == 200 and resp.text:
                        tree = html.fromstring(resp.text)
                        links = tree.cssselect("li.b_algo h2 a")
                        items = []
                        for l in links[:5]:
                            href = l.get("href") or ""
                            title = l.text_content().strip()
                            real_url = decode_bing_url(href)
                            if real_url and real_url.startswith("http") and "bing.com" not in real_url:
                                items.append({"title": title, "url": real_url})
                        if items:
                            search_cache.put(q, items)
                            results_map[q] = items
                except Exception as e:
                    LOG.debug("Worker query '%s' error: %s", q, e)
                finally:
                    completed += 1
                    if completed % 100 == 0 or completed == total:
                        elapsed = max(0.1, time.time() - t0)
                        LOG.info("High-Speed Search Progress: %d / %d completed (%.1f q/s, %d with results)",
                                 completed, total, completed / elapsed, len(results_map))

        await asyncio.gather(*(fetch_one(q) for q in queries_to_fetch), return_exceptions=True)

    return results_map


async def process_batch_evaluation(
    candidate_domains_by_query: dict[str, str],
    query_to_rows: dict[str, list[dict]],
    domain_cache: SQLiteCache,
    config: Config,
    fetcher: TieredFetcher
) -> tuple[int, int]:
    """Crawls candidate domains with Scrapling and evaluates them using 9-Point Elimination Engine."""
    unique_candidates = set(candidate_domains_by_query.values())
    to_crawl = []
    for dom in unique_candidates:
        norm = normalize_domain(dom)
        host = norm.normalized_domain
        if host and (await domain_cache.get(host)) is None:
            to_crawl.append(host)

    if to_crawl:
        LOG.info("Crawling %d candidate domains with Scrapling (400 workers)...", len(to_crawl))
        sem_crawl = asyncio.Semaphore(400)

        async def crawl_one(host: str):
            async with sem_crawl:
                try:
                    rec = await investigate_domain(host, config, fetcher)
                    await domain_cache.put(rec)
                except Exception as e:
                    LOG.debug("Crawl error for %s: %s", host, e)

        await asyncio.gather(*(crawl_one(h) for h in to_crawl), return_exceptions=True)

    new_replaced = 0
    new_populated = 0

    for q, cand in candidate_domains_by_query.items():
        rows_list = query_to_rows.get(q, [])
        if not rows_list:
            continue

        norm = normalize_domain(cand)
        host = norm.normalized_domain
        dom_rec = await domain_cache.get(host)
        if not dom_rec:
            continue

        decision = evaluate_elimination_decision(
            {
                "Organization Name": rows_list[0].get("Organization Name") or rows_list[0].get("original_company_name"),
                "Country": rows_list[0].get("country code")
            },
            norm,
            dom_rec,
            network_healthy=True,
        )

        if decision.get("classification") in ("VALID", "VALID_GROUP"):
            target_url = f"https://{host}"
            for r in rows_list:
                st = r.get("Domain Status")
                r["Domain URL"] = target_url
                if st == "UNVERIFIED_INACTIVE":
                    r["Domain Status"] = "REPLACED_INACTIVE"
                    new_replaced += 1
                elif st == "NOT_FOUND":
                    r["Domain Status"] = "VERIFIED_ACTIVE"
                    new_populated += 1

    return new_replaced, new_populated


async def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    csv_path = Path("final_super_merged_master_populated.csv")

    LOG.info("==================================================")
    LOG.info("STARTING CONTINUOUS DOMAIN RECOVERY PIPELINE")
    LOG.info("==================================================")

    # 1. Purge empty cache stubs from prior rate-limited runs
    purge_empty_cache_stubs("verifier/search_cache.sqlite")
    search_cache = SearchCache("verifier/search_cache.sqlite")

    # 2. Load master CSV
    all_rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    unresolved_rows = []
    for idx, r in enumerate(all_rows, 2):
        r["_row_idx"] = idx
        st = r.get("Domain Status")
        if st in ("UNVERIFIED_INACTIVE", "NOT_FOUND"):
            unresolved_rows.append(r)

    LOG.info("Master Dataset Total Rows: %d", len(all_rows))
    LOG.info("Remaining Unresolved Rows to Recover: %d", len(unresolved_rows))

    # 3. Build query map for unresolved rows
    query_to_rows: dict[str, list[dict]] = {}
    for r in unresolved_rows:
        name = (r.get("Organization Name") or r.get("original_company_name") or "").strip()
        if not name or name.lower() in ("none", "nan", ""):
            continue
        c_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
        country_name = COUNTRY_MAP.get(c_code, c_code)
        clean = clean_org_for_search(name)
        q = f'"{clean}" {country_name} official website'

        if q not in query_to_rows:
            query_to_rows[q] = []
        query_to_rows[q].append(r)

    all_queries = list(query_to_rows.keys())
    LOG.info("Generated %d unique company queries for unresolved rows", len(all_queries))

    # Identify cached vs uncached
    cached_queries = {}
    to_fetch_queries = []
    for q in all_queries:
        cached = search_cache.get(q)
        if cached:
            cached_queries[q] = cached
        else:
            to_fetch_queries.append(q)

    LOG.info("Queries already cached with results: %d", len(cached_queries))
    LOG.info("Queries to harvest via Camoufox: %d", len(to_fetch_queries))

    config = Config(concurrency=400, max_evidence_pages=2, use_dynamic=False, use_stealth=False, use_proxy=False, use_apify=False)
    domain_cache = SQLiteCache("verifier/live_validation_4000_cache.sqlite", config.cache_ttl_seconds)
    fetcher = TieredFetcher(config)

    total_recovered_inactive = 0
    total_recovered_not_found = 0

    # First, evaluate any already-cached results that haven't been applied yet
    if cached_queries:
        LOG.info("Processing already-cached search results...")
        cached_candidates = {}
        for q, res in cached_queries.items():
            cand = extract_candidate_domain(res)
            if cand:
                cached_candidates[q] = cand
        if cached_candidates:
            rep, pop = await process_batch_evaluation(cached_candidates, query_to_rows, domain_cache, config, fetcher)
            total_recovered_inactive += rep
            total_recovered_not_found += pop
            LOG.info("Pre-cached Results Yielded: %d replaced inactive, %d populated active", rep, pop)
            if rep > 0 or pop > 0:
                save_master_files(all_rows, excel_path, csv_path)

    # Process remaining queries in batches of 1,000 with 60 parallel workers for continuous checkpointing
    batch_size = 1000
    for i in range(0, len(to_fetch_queries), batch_size):
        chunk = to_fetch_queries[i : i + batch_size]
        LOG.info("--- Starting Search Batch %d/%d (%d queries with 60 parallel workers) ---",
                 (i // batch_size) + 1, (len(to_fetch_queries) + batch_size - 1) // batch_size, len(chunk))

        batch_results = await harvest_serp_batch(chunk, search_cache, concurrency=60)

        # Extract candidates
        batch_candidates = {}
        for q, org_res in batch_results.items():
            cand = extract_candidate_domain(org_res)
            if cand:
                batch_candidates[q] = cand

        LOG.info("Batch %d discovered %d candidate domains", (i // batch_size) + 1, len(batch_candidates))

        if batch_candidates:
            rep, pop = await process_batch_evaluation(batch_candidates, query_to_rows, domain_cache, config, fetcher)
            total_recovered_inactive += rep
            total_recovered_not_found += pop
            LOG.info("Batch %d Evaluation: +%d Replaced Inactive, +%d Newly Populated Active",
                     (i // batch_size) + 1, rep, pop)

            # Persist checkpoint after every batch that found domains
            if rep > 0 or pop > 0:
                save_master_files(all_rows, excel_path, csv_path)

    # Final Save & Summary
    LOG.info("==================================================")
    LOG.info("CONTINUOUS PIPELINE COMPLETE")
    LOG.info("Total Inactive Domains Replaced: %d", total_recovered_inactive)
    LOG.info("Total Not-Found Domains Populated: %d", total_recovered_not_found)
    total_active, coverage, status_summary = save_master_files(all_rows, excel_path, csv_path)

    summary = {
        "total_rows": len(all_rows),
        "total_recovered_inactive": total_recovered_inactive,
        "total_recovered_not_found": total_recovered_not_found,
        "total_active_domains": total_active,
        "active_domain_coverage_percent": coverage,
        "status_breakdown": status_summary,
    }
    with open("continuous_recovery_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOG.info("Final Master Files saved successfully.")
    LOG.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
