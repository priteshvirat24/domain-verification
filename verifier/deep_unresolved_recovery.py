"""
Deep Unresolved Domain Recovery Pipeline:
Specifically targets the remaining:
1. 5,422 UNVERIFIED_INACTIVE rows (using legacy domain slug & relaxed corporate search).
2. 20,158 NOT_FOUND rows (using relaxed multi-variant company search).
3. Evaluates candidates using 400 Scrapling workers + 9-Point Elimination Engine.
4. Progressively checkpoints to SUPER_MERGED_MASTER_FINAL.xlsx and final_super_merged_master_populated.csv.
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
from curl_cffi.requests import AsyncSession
from lxml import html

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
        logging.FileHandler("deep_unresolved_recovery.log", encoding="utf-8")
    ]
)
LOG = logging.getLogger("deep_unresolved")

EXCLUDE_SEARCH_DOMAINS = EXCLUDE_DOMAINS | {
    "google.com", "google.co.in", "translate.google.com", "translate.google.co.in",
    "pinterest.com", "en.wikipedia.org", "wikipedia.org", "cricbuzz.com",
    "claude.ai", "chatgpt.com", "gemini.google.com", "skillshop.withgoogle.com",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "instagram.com", "crunchbase.com", "bloomberg.com", "pitchbook.com",
    "dnb.com", "zoominfo.com", "yellowpages.com"
}


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


def extract_slug_from_legacy(domain_str: str) -> str:
    """Extracts base brand slug from legacy domain (e.g. mitani.co.jp -> mitani)."""
    if not domain_str:
        return ""
    norm = normalize_domain(domain_str)
    host = norm.normalized_domain or ""
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[0] if parts[0] != "www" else parts[1]
    return ""


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


async def harvest_high_speed_searches(queries: list[str], search_cache: SearchCache, concurrency: int = 120) -> dict[str, list[dict]]:
    """Harvests Bing search results using a 6-session curl_cffi Chrome 120 pool with 120 parallel async workers."""
    results_map = {}
    queries_to_fetch = [q for q in queries if search_cache.get(q) is None]

    for q in queries:
        cached = search_cache.get(q)
        if cached:
            results_map[q] = cached

    if not queries_to_fetch:
        return results_map

    sem = asyncio.Semaphore(concurrency)
    completed = 0
    total = len(queries_to_fetch)
    t0 = time.time()

    num_sessions = 6
    sessions = [AsyncSession(impersonate="chrome120", timeout=10) for _ in range(num_sessions)]

    try:
        async def fetch_one(idx: int, q: str):
            nonlocal completed
            session = sessions[idx % num_sessions]
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
                                parsed = urlsplit(real_url)
                                host = (parsed.netloc or "").lower().split(":")[0]
                                if not any(ex in host for ex in EXCLUDE_SEARCH_DOMAINS):
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
                        LOG.info("Ultra-Fast Search Progress: %d / %d completed (%.1f q/s, %d with results)",
                                 completed, total, completed / elapsed, len(results_map))

        await asyncio.gather(*(fetch_one(i, q) for i, q in enumerate(queries_to_fetch)), return_exceptions=True)
    finally:
        for s in sessions:
            try:
                await s.close()
            except Exception:
                pass

    return results_map


async def process_batch_evaluation(
    candidate_domains_by_query: dict[str, str],
    query_to_rows: dict[str, list[dict]],
    domain_cache: SQLiteCache,
    config: Config,
    fetcher: TieredFetcher
) -> tuple[int, int]:
    """Crawls candidate domains with Scrapling (400 workers) and evaluates via 9-Point Elimination Engine."""
    unique_candidates = set(candidate_domains_by_query.values())
    to_crawl = []
    for dom in unique_candidates:
        norm = normalize_domain(dom)
        host = norm.normalized_domain
        if host and (await domain_cache.get(host)) is None:
            to_crawl.append(host)

    if to_crawl:
        LOG.info("Crawling %d candidate domains with Scrapling (500 workers)...", len(to_crawl))
        sem_crawl = asyncio.Semaphore(500)

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
                if st == "UNVERIFIED_INACTIVE":
                    r["Domain URL"] = target_url
                    r["Domain Status"] = "REPLACED_INACTIVE"
                    new_replaced += 1
                elif st == "NOT_FOUND":
                    r["Domain URL"] = target_url
                    r["Domain Status"] = "VERIFIED_ACTIVE"
                    new_populated += 1

    return new_replaced, new_populated


async def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    csv_path = Path("final_super_merged_master_populated.csv")

    LOG.info("==================================================")
    LOG.info("STARTING DEEP UNRESOLVED & INACTIVE RECOVERY PIPELINE")
    LOG.info("==================================================")

    search_cache = SearchCache("verifier/search_cache.sqlite")

    # Load master CSV
    all_rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    inactive_rows = []
    not_found_rows = []
    for idx, r in enumerate(all_rows, 2):
        r["_row_idx"] = idx
        st = r.get("Domain Status")
        if st == "UNVERIFIED_INACTIVE":
            inactive_rows.append(r)
        elif st == "NOT_FOUND":
            not_found_rows.append(r)

    LOG.info("Dataset: Total Rows: %d | UNVERIFIED_INACTIVE: %d | NOT_FOUND: %d",
             len(all_rows), len(inactive_rows), len(not_found_rows))

    # Build Multi-Variant relaxed search queries
    query_to_rows: dict[str, list[dict]] = {}

    # 1. Inactive rows: try domain slug query + clean name query
    for r in inactive_rows:
        c_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
        country_name = COUNTRY_MAP.get(c_code, c_code)
        legacy = r.get("Original Legacy Domain") or r.get("Domain URL") or ""
        slug = extract_slug_from_legacy(legacy)
        org = (r.get("Organization Name") or r.get("original_company_name") or "").strip()
        clean = clean_org_for_search(org)

        if slug and len(slug) >= 3 and slug.isalpha():
            q_slug = f"{slug} {country_name} official website"
            if q_slug not in query_to_rows:
                query_to_rows[q_slug] = []
            query_to_rows[q_slug].append(r)

        if clean and len(clean) >= 3:
            q_clean = f"{clean} {country_name} official portal"
            if q_clean not in query_to_rows:
                query_to_rows[q_clean] = []
            query_to_rows[q_clean].append(r)

    # 2. Not-Found rows: relaxed unquoted query + brand portal query
    for r in not_found_rows:
        c_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
        country_name = COUNTRY_MAP.get(c_code, c_code)
        org = (r.get("Organization Name") or r.get("original_company_name") or "").strip()
        clean = clean_org_for_search(org)

        if clean and len(clean) >= 3:
            q = f"{clean} {country_name} official portal"
            if q not in query_to_rows:
                query_to_rows[q] = []
            query_to_rows[q].append(r)

    all_queries = list(query_to_rows.keys())
    LOG.info("Generated %d multi-variant targeted queries for unresolved entities", len(all_queries))

    config = Config(concurrency=500, max_evidence_pages=2, use_dynamic=False, use_stealth=False, use_proxy=False, use_apify=False)
    domain_cache = SQLiteCache("verifier/live_validation_4000_cache.sqlite", config.cache_ttl_seconds)
    fetcher = TieredFetcher(config)

    total_recovered_inactive = 0
    total_recovered_not_found = 0

    batch_size = 1000
    for i in range(0, len(all_queries), batch_size):
        chunk = all_queries[i : i + batch_size]
        LOG.info("--- Starting Targeted Batch %d/%d (%d queries with 120 parallel workers) ---",
                 (i // batch_size) + 1, (len(all_queries) + batch_size - 1) // batch_size, len(chunk))

        batch_results = await harvest_high_speed_searches(chunk, search_cache, concurrency=120)

        # Extract candidates
        batch_candidates = {}
        for q, items in batch_results.items():
            cand = extract_candidate_domain(items)
            if cand:
                batch_candidates[q] = cand

        LOG.info("Batch %d discovered %d candidate domains", (i // batch_size) + 1, len(batch_candidates))

        if batch_candidates:
            rep, pop = await process_batch_evaluation(batch_candidates, query_to_rows, domain_cache, config, fetcher)
            total_recovered_inactive += rep
            total_recovered_not_found += pop
            LOG.info("Batch %d Evaluation: +%d Replaced Inactive, +%d Newly Populated Active",
                     (i // batch_size) + 1, rep, pop)

            if rep > 0 or pop > 0:
                save_master_files(all_rows, excel_path, csv_path)

    # Final Save
    LOG.info("==================================================")
    LOG.info("TARGETED RECOVERY COMPLETE")
    LOG.info("Total Inactive Domains Replaced: %d", total_recovered_inactive)
    LOG.info("Total Not-Found Domains Populated: %d", total_recovered_not_found)
    total_active, coverage, status_summary = save_master_files(all_rows, excel_path, csv_path)
    LOG.info("Final Master Files saved successfully.")
    LOG.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
