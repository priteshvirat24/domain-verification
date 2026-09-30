"""
Pilot Domain Recovery Pipeline:
Recovers and replaces INACTIVE domains with verified active domains using
Apify Google Search + Scrapling Content Crawler + Elimination-First Verification Engine.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import urllib.parse
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
    "youtube.com", "wikipedia.org", "bloomberg.com", "reuters.com",
    "dnb.com", "zoominfo.com", "yellowpages.com", "yellowpages.com.au",
    "emis.com", "crunchbase.com", "kompass.com", "pitchbook.com",
    "google.com", "glassdoor.com", "indeed.com", "seek.com.au",
    "jobstreet.com", "apple.com", "play.google.com", "pinterest.com",
    "ditchcarbon.com", "cbinsights.com", "owler.com", "craft.co",
}

COUNTRY_MAP = {
    "AU": "Australia", "MY": "Malaysia", "SG": "Singapore",
    "JP": "Japan", "TH": "Thailand", "ID": "Indonesia",
    "PH": "Philippines", "VN": "Vietnam", "NZ": "New Zealand",
    "KR": "South Korea", "IN": "India", "HK": "Hong Kong",
}


def clean_org_for_search(name: str) -> str:
    cleaned = re.sub(r"\s*-\s*[A-Z]{2}$", "", name)
    cleaned = re.sub(r"\b(PT|SDN BHD|PTE LTD|LTD|CO\., LTD\.|INC|CORP|HOLDINGS?)\b\.?", "", cleaned, flags=re.I)
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
            
            # Check exclusions
            parts = host.split(".")
            root_domain = ".".join(parts[-2:]) if len(parts) >= 2 else host
            if any(exc in host for exc in EXCLUDE_DOMAINS):
                continue
            if url.lower().endswith(".pdf") or "/docs/" in url.lower():
                continue
            return host
        except Exception:
            continue
    return None


def run_batch_apify_search(client, queries: list[str]) -> dict[str, list[dict]]:
    """Runs a batch of search queries on Apify Google Search Scraper."""
    if not queries:
        return {}
    query_text = "\n".join(queries)
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
            return {}
        items = client.dataset(dataset_id).list_items().items
        out = {}
        for item in items:
            q = (item.get("searchQuery") or {}).get("term") or ""
            out[q] = item.get("organicResults", [])
        return out
    except Exception as e:
        LOG.error("Apify search batch error: %s", e)
        return {}


async def main():
    pilot_size = 500
    output_dir = Path("pilot_recovery_output")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = "verifier/recovery_cache.sqlite"

    config = Config(
        concurrency=100,
        max_evidence_pages=3,
        use_dynamic=False,
        use_stealth=False,
        use_proxy=False,
        use_apify=True,
    )
    cache = SQLiteCache(cache_path, config.cache_ttl_seconds)
    fetcher = TieredFetcher(config, proxy_url=None)
    apify_client = get_apify_client(config.apify_token)

    # 1. Load inactive rows
    results_path = "run_full_output/results.csv"
    with open(results_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    inactive_rows = [r for r in all_rows if r.get("classification") == "INACTIVE"]
    LOG.info("Total INACTIVE rows in dataset: %d", len(inactive_rows))

    # Pick unique organizations
    seen_orgs = set()
    sample = []
    for r in inactive_rows:
        org = (r.get("organization_for_verification") or r.get("Organization Name") or "").strip()
        if org and org not in seen_orgs:
            seen_orgs.add(org)
            sample.append(r)
            if len(sample) == pilot_size:
                break

    LOG.info("Selected %d unique organizations for pilot recovery", len(sample))

    # 2. Build search queries
    query_to_row = {}
    for r in sample:
        org = (r.get("organization_for_verification") or r.get("Organization Name") or "").strip()
        country_code = (r.get("Country") or "").strip()
        country_name = COUNTRY_MAP.get(country_code, country_code)
        clean_org = clean_org_for_search(org)
        q = f'"{clean_org}" {country_name} official website'.strip()
        query_to_row[q] = r

    queries = list(query_to_row.keys())
    batch_size = 25
    search_results_by_query = {}

    LOG.info("Starting Apify Google Search for %d queries in batches of %d...", len(queries), batch_size)
    for i in range(0, len(queries), batch_size):
        chunk = queries[i:i + batch_size]
        LOG.info("Searching queries %d..%d / %d...", i + 1, min(i + batch_size, len(queries)), len(queries))
        batch_res = await asyncio.to_thread(run_batch_apify_search, apify_client, chunk)
        search_results_by_query.update(batch_res)

    LOG.info("Completed search queries. Parsing top candidate domains...")

    # 3. Match candidate domains
    row_candidates = []
    for q, r in query_to_row.items():
        organic = search_results_by_query.get(q, [])
        cand_domain = extract_candidate_domain(organic)
        row_candidates.append((r, cand_domain, q))

    # 4. Crawl & Verify candidates
    LOG.info("Crawling and verifying candidate domains via Scrapling + Elimination Engine...")
    results = []
    recovered_count = 0
    mismatch_count = 0
    not_found_count = 0

    for idx, (r, cand_domain, q) in enumerate(row_candidates, 1):
        org = (r.get("organization_for_verification") or r.get("Organization Name") or "").strip()
        old_domain = r.get("Domain Name") or r.get("original_domain") or ""
        country = r.get("Country") or ""

        if not cand_domain:
            not_found_count += 1
            res_entry = dict(r)
            res_entry["recovery_status"] = "NOT_FOUND"
            res_entry["discovered_domain"] = ""
            res_entry["recovery_reason"] = "Search did not return a valid candidate official website"
            results.append(res_entry)
            continue

        # Inspect candidate domain
        norm = normalize_domain(cand_domain)
        dom_rec = await cache.get(norm.host)
        if not dom_rec:
            dom_rec = await investigate_domain(norm.host, config, fetcher)
            await cache.put(dom_rec)

        # Run elimination decision
        decision = evaluate_elimination_decision(r, norm, dom_rec, network_healthy=True)
        decision_class = decision.get("classification")
        reason = decision.get("decision_reason")

        res_entry = dict(r)
        res_entry["original_domain"] = old_domain
        res_entry["original_classification"] = "INACTIVE"
        res_entry["discovered_domain"] = norm.host
        res_entry["recovery_reason"] = reason

        if decision_class in ("VALID", "VALID_GROUP"):
            recovered_count += 1
            res_entry["recovery_status"] = f"RECOVERED_{decision_class}"
            res_entry["classification"] = decision_class
            res_entry["Domain Name"] = norm.host
            res_entry["normalized_domain"] = norm.host
            LOG.info("[%d/%d] RECOVERED: '%s' -> %s (%s)", idx, pilot_size, org, norm.host, decision_class)
        else:
            mismatch_count += 1
            res_entry["recovery_status"] = f"UNVERIFIED_{decision_class}"
            LOG.info("[%d/%d] UNVERIFIED: '%s' candidate %s yielded %s", idx, pilot_size, org, norm.host, decision_class)

        results.append(res_entry)

    # 5. Export results
    out_csv = output_dir / "recovered_results.csv"
    if results:
        fieldnames = list(results[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    summary = {
        "total_attempted": pilot_size,
        "recovered_valid_or_group": recovered_count,
        "recovery_rate_percent": round((recovered_count / pilot_size) * 100, 2),
        "unverified_candidates": mismatch_count,
        "no_candidate_found": not_found_count,
    }
    with open(output_dir / "recovery_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOG.info("=== PILOT RECOVERY COMPLETE ===")
    LOG.info("Total attempted: %d", pilot_size)
    LOG.info("Successfully recovered (VALID / VALID_GROUP): %d (%.2f%%)", recovered_count, summary["recovery_rate_percent"])
    LOG.info("Unverified / Mismatched: %d", mismatch_count)
    LOG.info("No candidates: %d", not_found_count)
    LOG.info("Output written to %s", out_csv)


if __name__ == "__main__":
    asyncio.run(main())
