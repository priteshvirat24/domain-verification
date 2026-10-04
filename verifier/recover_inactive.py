"""
Automated Inactive Domain Discovery & Recovery Pipeline:
1. Searches for official websites of organizations with INACTIVE domains (Apify Google Search).
2. Persistently caches search results in SQLite with WAL mode to avoid redundant API calls.
3. Crawls discovered candidate domains concurrently with Scrapling (curl-impersonate / TLS).
4. Verifies candidate domains with the Elimination-First Engine.
5. Replaces verified inactive domains with active domains in the dataset.
6. Runs pilot batch (500 rows) first, logs metrics, and then continues for the full inactive dataset.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from verifier.cache import SQLiteCache
from verifier.config import Config
from verifier.elimination_engine import evaluate_elimination_decision
from verifier.fetcher import TieredFetcher
from verifier.normalization import normalize_domain, NormalizedDomain
from verifier.models import DomainRecord
from verifier.pipeline import investigate_domain
from verifier.apify_escalation import get_apify_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("recover_inactive")

EXCLUDE_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "wikimedia.org", "bloomberg.com", "reuters.com",
    "dnb.com", "zoominfo.com", "yellowpages.com", "yellowpages.com.au",
    "emis.com", "crunchbase.com", "kompass.com", "pitchbook.com",
    "google.com", "glassdoor.com", "indeed.com", "seek.com.au",
    "jobstreet.com", "apple.com", "play.google.com", "pinterest.com",
    "ditchcarbon.com", "cbinsights.com", "owler.com", "craft.co",
    "ebsco.com", "fda.gov", "nih.gov", "sec.gov", "wipo.int",
    "sciencedirect.com", "researchgate.net", "tandfonline.com", "springer.com",
    "jstor.org", "yahoo.com", "msn.com", "bing.com", "baidu.com",
    "mapquest.com", "tripadvisor.com", "yelp.com", "trustpilot.com",
    "bbb.org", "opencorporates.com", "transparency.gov.au",
    "envalith.com", "accessdata.fda.gov", "britannica.com", "gov.au/abn",
}

COUNTRY_MAP = {
    "AU": "Australia", "MY": "Malaysia", "SG": "Singapore",
    "JP": "Japan", "TH": "Thailand", "ID": "Indonesia",
    "PH": "Philippines", "VN": "Vietnam", "NZ": "New Zealand",
    "KR": "South Korea", "IN": "India", "HK": "Hong Kong",
}


def clean_org_for_search(name: str) -> str:
    cleaned = re.sub(r"\s*-\s*[A-Z]{2}$", "", name)
    cleaned = re.sub(r"\bCO[\.,\s]+(?:LTD|LIMITED)\b\.?", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:PTE|PTY|SDN|BHD|LTD|LIMITED|INC|CORP|CORPORATION|LLC|GMBH|PLC|BV|AG|KK|K\.K\.)\b\.?", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bPT\b\.?", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[,\(\)\.\"]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or name


def extract_candidate_domain(organic_results: list[dict]) -> str | None:
    for res in organic_results:
        url = res.get("url") or ""
        if not url:
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if not host:
                continue
            if any(host == exc or host.endswith("." + exc) for exc in EXCLUDE_DOMAINS):
                continue
            if url.lower().endswith(".pdf") or "/docs/" in url.lower():
                continue
            return host
        except Exception:
            continue
    return None


class SearchCache:
    def __init__(self, db_path: str = "verifier/search_cache.sqlite"):
        self.conn = sqlite3.connect(db_path, timeout=60.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS searches (query TEXT PRIMARY KEY, results_json TEXT, created_at TEXT)"
        )
        self.conn.commit()

    def get(self, query: str) -> list[dict] | None:
        cur = self.conn.cursor()
        row = cur.execute("SELECT results_json, created_at FROM searches WHERE query = ?", (query,)).fetchone()
        if row:
            results = json.loads(row[0])
            created = row[1] if row[1] else None
            # Gap 17: Expire empty results after 24h, non-empty after 30 days
            if created:
                try:
                    from datetime import datetime, timezone, timedelta
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age = datetime.now(timezone.utc) - ts
                    if not results and age > timedelta(hours=24):
                        # Empty results have expired — purge and retry
                        self.conn.execute("DELETE FROM searches WHERE query = ?", (query,))
                        self.conn.commit()
                        return None
                    if age > timedelta(days=30):
                        # Stale results — purge and allow refresh
                        self.conn.execute("DELETE FROM searches WHERE query = ?", (query,))
                        self.conn.commit()
                        return None
                except Exception:
                    pass
            return results
        return None

    def put(self, query: str, results: list[dict]):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO searches (query, results_json, created_at) VALUES (?, ?, ?)",
            (query, json.dumps(results), now),
        )
        self.conn.commit()


def run_batch_apify_search_with_retries(client, queries: list[str], max_retries: int = 3) -> dict[str, list[dict]]:
    """Runs a batch of search queries on Apify Google Search Scraper with exponential backoff retries."""
    if not queries:
        return {}
    query_text = "\n".join(queries)

    for attempt in range(1, max_retries + 1):
        try:
            run = client.actor("apify/google-search-scraper").call(
                run_input={
                    "queries": query_text,
                    "maxPagesPerQuery": 1,
                    "resultsPerPage": 4,
                }
            )
            dataset_id = getattr(run, "default_dataset_id", None) or (run.get("defaultDatasetId") if hasattr(run, "get") else None)
            if not dataset_id:
                LOG.warning("No default dataset ID in Apify run on attempt %d", attempt)
                time.sleep(3 * attempt)
                continue
            items = client.dataset(dataset_id).list_items(limit=10000).items
            out = {}
            for item in items:
                q = (item.get("searchQuery") or {}).get("term") or ""
                out[q] = item.get("organicResults", [])
            return out
        except Exception as e:
            LOG.warning("Apify search batch attempt %d failed: %s", attempt, e)
            if "limit exceeded" in str(e).lower() or "usage limit" in str(e).lower():
                LOG.error("Apify usage hard limit reached. Aborting search retries.")
                return {}
            if attempt < max_retries:
                time.sleep(4 * attempt)
            else:
                LOG.error("Apify search batch permanently failed for %d queries", len(queries))
                return {}
    return {}


async def process_rows_batch(
    rows: list[dict],
    batch_name: str,
    output_dir: Path,
    config: Config,
    cache: SQLiteCache,
    search_cache: SearchCache,
    fetcher: TieredFetcher,
    apify_client,
) -> tuple[dict, list[dict]]:
    LOG.info("=== Starting %s: %d rows ===", batch_name, len(rows))

    # 1. Prepare search queries and map each query to its corresponding rows (preserving all duplicate org rows)
    query_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        org = (r.get("organization_for_verification") or r.get("Organization Name") or "").strip()
        country_code = (r.get("Country") or r.get("\ufeffCountry") or "").strip()
        country_name = COUNTRY_MAP.get(country_code, country_code)
        clean_org = clean_org_for_search(org)
        q = f'"{clean_org}" {country_name} official website'.strip()
        query_to_rows[q].append(r)

    queries = list(query_to_rows.keys())
    needed_queries = []
    search_results_by_query = {}

    for q in queries:
        cached_res = search_cache.get(q)
        if cached_res is not None:
            search_results_by_query[q] = cached_res
        else:
            needed_queries.append(q)

    LOG.info("[%s] %d queries cached, %d queries need fetching via Apify", batch_name, len(search_results_by_query), len(needed_queries))

    # Run missing queries in batches of 50 with concurrency of 24
    batch_size = 50
    sem_apify = asyncio.Semaphore(24)

    async def fetch_chunk(chunk: list[str], chunk_idx: int, total_chunks: int):
        async with sem_apify:
            LOG.info("[%s] Apify Search chunk %d/%d (%d queries)...", batch_name, chunk_idx, total_chunks, len(chunk))
            batch_res = await asyncio.to_thread(run_batch_apify_search_with_retries, apify_client, chunk)
            for q, org_res in batch_res.items():
                search_cache.put(q, org_res)
                search_results_by_query[q] = org_res
            # Missing results may indicate a transient actor failure; retry them later.
            for q in chunk:
                if q not in search_results_by_query:
                    LOG.warning("Search returned no response for query %r; left uncached", q)

    chunks = [needed_queries[i:i + batch_size] for i in range(0, len(needed_queries), batch_size)]
    if chunks:
        await asyncio.gather(*(fetch_chunk(chunk, idx + 1, len(chunks)) for idx, chunk in enumerate(chunks)))

    # 2. Extract candidate domains for every row
    row_candidates = []
    for q, row_list in query_to_rows.items():
        organic = search_results_by_query.get(q, [])
        cand_domain = extract_candidate_domain(organic)
        for r in row_list:
            row_candidates.append((r, cand_domain, q))

    # 3. Concurrent crawling of discovered candidate domains
    unique_candidate_domains = {c for _, c, _ in row_candidates if c}
    LOG.info("[%s] Unique candidate domains discovered: %d. Crawling concurrently...", batch_name, len(unique_candidate_domains))

    sem = asyncio.Semaphore(config.concurrency)

    async def crawl_domain(dom: str):
        async with sem:
            try:
                norm = normalize_domain(dom)
                host = norm.normalized_domain
                if not host:
                    return
                rec = await cache.get(host)
                if not rec:
                    rec = await investigate_domain(host, config, fetcher)
                    await cache.put(rec)
            except Exception as e:
                LOG.warning("Error crawling candidate domain %s: %s", dom, e)

    await asyncio.gather(*(crawl_domain(d) for d in unique_candidate_domains), return_exceptions=True)
    LOG.info("[%s] Completed crawling all candidate domains.", batch_name)

    # 4. Evaluate elimination decision for all rows
    results = []
    recovered_valid = 0
    recovered_valid_group = 0
    mismatch_count = 0
    not_found_count = 0

    for idx, (r, cand_domain, q) in enumerate(row_candidates, 1):
        old_domain = r.get("Domain Name") or r.get("original_domain") or ""

        if not cand_domain:
            not_found_count += 1
            res_entry = dict(r)
            res_entry["recovery_status"] = "NOT_FOUND"
            res_entry["original_domain"] = old_domain
            res_entry["discovered_domain"] = ""
            res_entry["recovery_reason"] = "Search did not return an official website candidate"
            results.append(res_entry)
            continue

        norm = normalize_domain(cand_domain)
        host = norm.normalized_domain
        dom_rec = await cache.get(host)

        decision = evaluate_elimination_decision(r, norm, dom_rec, network_healthy=True)
        decision_class = decision.get("classification")
        reason = decision.get("decision_reason")

        res_entry = dict(decision)
        res_entry["original_domain"] = old_domain
        res_entry["original_classification"] = "INACTIVE"
        res_entry["discovered_domain"] = host
        res_entry["recovery_reason"] = reason

        if decision_class == "VALID":
            recovered_valid += 1
            res_entry["recovery_status"] = "RECOVERED_VALID"
            res_entry["classification"] = "VALID"
            res_entry["confidence"] = decision.get("confidence", "HIGH")
            res_entry["Domain Name"] = host
            res_entry["normalized_domain"] = host
        elif decision_class == "VALID_GROUP":
            recovered_valid_group += 1
            res_entry["recovery_status"] = "RECOVERED_VALID_GROUP"
            res_entry["classification"] = "VALID_GROUP"
            res_entry["confidence"] = decision.get("confidence", "HIGH")
            res_entry["Domain Name"] = host
            res_entry["normalized_domain"] = host
        else:
            mismatch_count += 1
            res_entry["recovery_status"] = f"UNVERIFIED_{decision_class}"
            res_entry["classification"] = "INACTIVE"
            res_entry["Domain Name"] = old_domain
            res_entry["normalized_domain"] = old_domain

        results.append(res_entry)

    # 5. Export results
    out_csv = output_dir / f"{batch_name.lower()}_results.csv"
    if results:
        fieldnames = list(results[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    total_recovered = recovered_valid + recovered_valid_group
    total_rows = len(rows)
    recovery_rate = round((total_recovered / total_rows) * 100, 2) if total_rows else 0.0

    summary = {
        "batch_name": batch_name,
        "total_attempted": total_rows,
        "recovered_valid": recovered_valid,
        "recovered_valid_group": recovered_valid_group,
        "total_recovered": total_recovered,
        "recovery_rate_percent": recovery_rate,
        "unverified_candidates": mismatch_count,
        "no_candidate_found": not_found_count,
    }

    out_summary = output_dir / f"{batch_name.lower()}_summary.json"
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOG.info("=== %s COMPLETE ===", batch_name.upper())
    LOG.info("Total attempted: %d | Recovered: %d (%.2f%%) [VALID: %d, VALID_GROUP: %d]",
             total_rows, total_recovered, recovery_rate, recovered_valid, recovered_valid_group)
    LOG.info("Results saved to: %s", out_csv)

    return summary, results


async def main():
    output_dir = Path("recovery_output")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = "verifier/live_validation_4000_cache.sqlite"

    config = Config(
        concurrency=150,
        max_evidence_pages=3,
        use_dynamic=False,
        use_stealth=False,
        use_proxy=False,
        use_apify=True,
    )
    cache = SQLiteCache(cache_path, config.cache_ttl_seconds)
    search_cache = SearchCache("verifier/search_cache.sqlite")
    fetcher = TieredFetcher(config, proxy_url=None)
    apify_client = get_apify_client(config.apify_token)

    # Load all rows from results.csv
    results_path = "run_full_output/results.csv"
    with open(results_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    inactive_rows = [r for r in all_rows if r.get("classification") == "INACTIVE"]
    LOG.info("Total rows in dataset: %d | Total INACTIVE rows: %d", len(all_rows), len(inactive_rows))

    # Directly proceed with ALL inactive rows
    LOG.info("Proceeding directly with ALL %d inactive rows in the dataset...", len(inactive_rows))
    full_summary, full_results = await process_rows_batch(
        inactive_rows, "full_inactive_recovered", output_dir, config, cache, search_cache, fetcher, apify_client
    )

    # Phase 3: Replace verified inactive domains in the full master dataset
    LOG.info("Phase 3: Replacing verified inactive domains in master dataset...")
    recovered_map = {
        r["input_row_id"]: r
        for r in full_results
        if r.get("recovery_status") in ("RECOVERED_VALID", "RECOVERED_VALID_GROUP")
    }

    updated_full_rows = []
    replaced_count = 0
    for r in all_rows:
        row_id = r.get("input_row_id")
        if row_id in recovered_map:
            rec = recovered_map[row_id]
            updated_row = dict(r)
            updated_row.update(rec)
            updated_full_rows.append(updated_row)
            replaced_count += 1
        else:
            updated_full_rows.append(r)

    out_master = output_dir / "results_full_active_replaced.csv"
    if updated_full_rows:
        keys_set = set()
        for row in updated_full_rows:
            keys_set.update(row.keys())
        fieldnames = list(all_rows[0].keys())
        for k in sorted(keys_set):
            if k not in fieldnames:
                fieldnames.append(k)
        with open(out_master, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(updated_full_rows)

    # Backup original run_full_output/results.csv and overwrite with updated results
    backup_path = Path("run_full_output/results_backup_pre_recovery.csv")
    if not backup_path.exists():
        shutil.copyfile("run_full_output/results.csv", backup_path)
        LOG.info("Backed up original run_full_output/results.csv to %s", backup_path)

    with open("run_full_output/results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(updated_full_rows)

    # Compute updated global summary
    valid_count = sum(1 for r in updated_full_rows if r.get("classification") == "VALID")
    valid_group_count = sum(1 for r in updated_full_rows if r.get("classification") == "VALID_GROUP")
    inactive_count = sum(1 for r in updated_full_rows if r.get("classification") == "INACTIVE")
    review_count = sum(1 for r in updated_full_rows if r.get("classification") == "REVIEW")
    mismatch_count = sum(1 for r in updated_full_rows if r.get("classification") == "MISMATCH")
    total_rows = len(updated_full_rows)
    total_verified = valid_count + valid_group_count
    verification_rate = round((total_verified / total_rows) * 100, 2) if total_rows else 0.0

    global_summary = {
        "total_rows": total_rows,
        "replaced_inactive_domains": replaced_count,
        "valid": valid_count,
        "valid_group": valid_group_count,
        "total_verified": total_verified,
        "verification_rate_percent": verification_rate,
        "remaining_inactive": inactive_count,
        "review": review_count,
        "mismatch": mismatch_count,
    }

    with open(output_dir / "global_recovery_summary.json", "w", encoding="utf-8") as f:
        json.dump(global_summary, f, indent=2)

    with open("run_full_output/summary.json", "w", encoding="utf-8") as f:
        json.dump(global_summary, f, indent=2)

    LOG.info("=== REPLACEMENT PIPELINE COMPLETE ===")
    LOG.info("Total rows: %d | Replaced inactive domains: %d", total_rows, replaced_count)
    LOG.info("New Verified count: %d (%.2f%%) | Remaining inactive: %d",
             total_verified, verification_rate, inactive_count)
    LOG.info("Master updated results saved to: %s and run_full_output/results.csv", out_master)


if __name__ == "__main__":
    asyncio.run(main())
