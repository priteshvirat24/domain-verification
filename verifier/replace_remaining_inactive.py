"""
Production Inactive Replacement Pipeline
1. Cleans known false positives (e.g. timesofindia/indiatimes).
2. Evaluates remaining UNVERIFIED_INACTIVE rows using pre-cached search candidates.
3. Uses 120 parallel async workers with Scrapling TieredFetcher.
4. Strict validation: VALID_EXACT, VALID_GROUP, VALID_ENTITY, STRONG_MATCH or core domain slug match.
5. Checkpoints to SUPER_MERGED_MASTER_FINAL.xlsx and final_super_merged_master_populated.csv.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import openpyxl

from verifier.config import Config
from verifier.elimination_engine import CCTLD_MAP, evaluate_elimination_decision
from verifier.fetcher import TieredFetcher, dns_status
from verifier.normalization import normalize_domain, registered_domain
from verifier.pipeline import investigate_domain
from verifier.recover_inactive import (
    clean_org_for_search,
    COUNTRY_MAP,
    EXCLUDE_DOMAINS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("inactive_replacement.log", encoding="utf-8"),
    ],
)
LOG = logging.getLogger("inactive_pipeline")

EXTRA_EXCLUDE = EXCLUDE_DOMAINS | {
    "databasesets.com", "bizdirlib.com", "cybo.com", "findglocal.com",
    "companiesdb.net", "dataforthai.com", "companieshouse.co.th",
    "vietnam-visa.com", "openbriefing.com", "kordamentha.com", "chess.com",
    "worldplaces.me", "fandom.com", "auscompanies.com", "acnc.gov.au",
    "donatehq.com.au", "bizly.com.au", "verif.com", "aubiz.net", "vgccc.vic.gov.au",
    "6sense.com", "cricket.or.jp", "mnetplus.world", "straykidsjapan.com",
    "automotiveworld.com", "ftc.gov", "japan.go.jp", "timesofindia.com",
    "indiatimes.com", "economictimes.indiatimes.com", "kamikochi.org", "wikipedia.org",
    "reddit.com", "zhihu.com", "quora.com", "alicejapan.co.jp", "ndangira.net",
    "steampowered.com", "espn.com", "virginaustralia.com", "justdial.com",
    "mofa.go.kr", "korea.net", "52pojie.cn", "ilifehacks.com", "techpilipinas.com",
    "govtjobguru.in", "ontheworldmap.com", "newzealand.com", "laodong.vn", "vietnamnet.vn",
    "autocarindia.com", "educationquizzes.com", "jagranjosh.com", "shiksha.com",
    "carwale.com", "bikewale.com", "moneycontrol.com", "ndtv.com", "hindustantimes.com"
}

GENERIC_SLUGS = {
    "auto", "air", "sports", "tech", "med", "news", "group", "holdings",
    "services", "online", "shop", "global", "japan", "thai", "malaysia",
    "china", "korea", "school", "college", "bank", "credit", "energy", "trade", "india"
}


def extract_slug_from_legacy(domain_str: str) -> str:
    """Extracts distinctive brand slug from legacy domain."""
    if not domain_str:
        return ""
    norm = normalize_domain(domain_str)
    host = norm.normalized_domain or ""
    parts = host.split(".")
    if not parts:
        return ""
    if parts[0] == "www" and len(parts) > 1:
        parts = parts[1:]
    if parts[0] not in ("com", "org", "net", "gov", "edu", "co", "or"):
        slug = parts[0]
        slug = re.sub(r"[^a-zA-Z0-9\-]", "", slug).lower()
        if len(slug) >= 4 and slug not in GENERIC_SLUGS:
            return slug
    return ""


def clean_known_false_positives(all_rows: list[dict]) -> int:
    """Cleans known false positives like timesofindia/indiatimes from previous runs."""
    cleaned = 0
    bad_patterns = ("timesofindia", "indiatimes", "autocarindia", "educationquizzes", "jagranjosh")
    for r in all_rows:
        dom = (r.get("Domain URL") or "").lower()
        if any(bp in dom for bp in bad_patterns):
            leg = r.get("Original Legacy Domain") or ""
            if leg:
                r["Domain URL"] = leg
                r["Domain Status"] = "UNVERIFIED_INACTIVE"
            else:
                r["Domain URL"] = ""
                r["Domain Status"] = "NOT_FOUND"
            r["reason"] = "Reverted false positive generic media domain"
            cleaned += 1
    if cleaned > 0:
        LOG.info("Cleaned %d false positive media domains from master records.", cleaned)
    return cleaned


def save_csv_checkpoint(all_rows: list[dict], csv_path: Path):
    """Fast checkpoint writing CSV in ~1 second."""
    fieldnames = [c for c in all_rows[0].keys() if c != "_row_idx"]
    temp_csv = csv_path.with_suffix(".tmp")
    with open(temp_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    safe_csv = csv_path.with_name(csv_path.stem + "_REMAINING_REVIEWED.csv")
    temp_csv.replace(safe_csv)
    LOG.info("Checkpoint saved to CSV (%s).", safe_csv)


def save_master_files(all_rows: list[dict], excel_path: Path, csv_path: Path):
    """Atomically saves all rows to Excel and CSV master files."""
    save_csv_checkpoint(all_rows, csv_path)

    LOG.info("Syncing Excel master file: %s (113,488 rows)...", excel_path)
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
        leg = r.get("Original Legacy Domain")
        st = r.get("Domain Status")

        ws.cell(row=row_idx, column=col_domain, value=d)
        ws.cell(row=row_idx, column=col_legacy, value=leg)
        ws.cell(row=row_idx, column=col_status, value=st)

        status_summary[st] = status_summary.get(st, 0) + 1
        if st in ("VERIFIED_ACTIVE", "REPLACED_INACTIVE", "PRE_EXISTING_ACTIVE"):
            total_active += 1

    temp_excel = excel_path.with_name(f"{excel_path.stem}.tmp.xlsx")
    wb.save(temp_excel)
    temp_excel.replace(excel_path.with_name(excel_path.stem + "_REMAINING_REVIEWED.xlsx"))

    total_rows = len(all_rows)
    coverage = round((total_active / total_rows) * 100, 2)
    LOG.info("Master Files Synced! Current Active Verified Domains: %d / %d (%.2f%%) | Breakdown: %s",
             total_active, total_rows, coverage, status_summary)
    return total_active, coverage, status_summary


async def verify_candidate(cand: str, row: dict, config: Config, fetcher: TieredFetcher) -> str | None:
    """Verifies candidate domain via live crawl and 9-point elimination engine / slug matching."""
    norm = normalize_domain(cand)
    host = norm.normalized_domain
    if not host or any(ex in host for ex in EXTRA_EXCLUDE):
        return None

    # Exclude spam / non-enterprise TLDs
    INVALID_TLDS = (".wtf", ".xyz", ".day", ".top", ".vip", ".stream", ".online", ".pro", ".click", ".rest", ".game", ".icu", ".wang", ".monster", ".club")
    if host.endswith(INVALID_TLDS):
        return None

    legacy = row.get("Original Legacy Domain") or row.get("Domain URL") or ""
    norm_leg = normalize_domain(legacy).normalized_domain
    if host == norm_leg:
        return None

    c_code = (row.get("country code") or row.get("Country (Group)") or "").strip().upper()
    cand_tld = host.split(".")[-1].lower()
    if cand_tld in CCTLD_MAP and c_code and CCTLD_MAP[cand_tld] != c_code:
        return None
    if cand_tld in ("in", "za", "ru", "br", "de", "cn") and c_code not in ("IN", "ZA", "RU", "BR", "DE", "CN"):
        return None

    try:
        dom_rec = await investigate_domain(host, config, fetcher)
        if not dom_rec.domain_active or dom_rec.http_status not in (200, 301, 302, 307, 308):
            return None

        # Distinctive slug match (exact only)
        legacy_slug = extract_slug_from_legacy(legacy)
        cand_slug = extract_slug_from_legacy(host)
        is_exact_slug = bool(legacy_slug and cand_slug and legacy_slug == cand_slug and len(legacy_slug) >= 5)

        # 9-point Elimination Engine
        row_for_engine = dict(row)
        row_for_engine["Country"] = c_code
        row_for_engine["Organization Name"] = (
            row.get("Organization Name")
            or row.get("original_company_name")
            or ""
        )
        row_for_engine["Sales Territory Name"] = ""
        dec = evaluate_elimination_decision(row_for_engine, norm, dom_rec)
        cls = dec.get("classification")
        check_b = dec.get("checks", {}).get("check_b_org_name")

        # Require valid classification from verification engine
        if cls in ("VALID", "VALID_GROUP"):
            if check_b in ("YES", "PARTIAL") or is_exact_slug:
                return host
    except Exception as e:
        LOG.debug("Verification error for %s: %s", host, e)

    return None


async def main():
    excel_path = Path("SUPER_MERGED_MASTER_FINAL.xlsx")
    csv_path = Path("final_super_merged_master_populated.csv")
    db_path = Path("verifier/search_cache.sqlite")

    LOG.info("==================================================")
    LOG.info("STARTING PRODUCTION INACTIVE REPLACEMENT PIPELINE")
    LOG.info("Targeting remaining UNVERIFIED_INACTIVE rows")
    LOG.info("==================================================")

    conn = sqlite3.connect(str(db_path), timeout=60.0)
    cursor = conn.cursor()

    # Load master CSV
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    for idx, r in enumerate(all_rows, 2):
        r["_row_idx"] = idx

    clean_known_false_positives(all_rows)

    inactive_rows = [r for r in all_rows if r.get("Domain Status") == "UNVERIFIED_INACTIVE"]
    total_target = len(inactive_rows)
    LOG.info("Loaded %d rows total. Inactive rows to evaluate: %d", len(all_rows), total_target)

    config = Config(concurrency=700, max_evidence_pages=1, use_dynamic=False, use_stealth=False, use_proxy=False, respect_robots=False)
    fetcher = TieredFetcher(config)

    worker_sem = asyncio.Semaphore(350)
    replaced_count = 0
    processed_count = 0
    batch_size = 1000
    t0 = time.time()

    async def process_row(r: dict):
        nonlocal replaced_count, processed_count

        c_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
        country_name = COUNTRY_MAP.get(c_code, c_code)
        org = (r.get("Organization Name") or r.get("original_company_name") or r.get("Sales Territory Name") or "").strip()
        clean_org = clean_org_for_search(org)
        legacy = r.get("Original Legacy Domain") or r.get("Domain URL") or ""

        # Retrieve cached search results
        q1 = f"{clean_org} {country_name} official portal"
        q2 = f'"{clean_org}" {country_name} official website'
        q3 = f"{clean_org} {country_name} official website"

        cached_items = []
        for q in (q1, q2, q3):
            row_db = cursor.execute("SELECT results_json FROM searches WHERE query = ?", (q,)).fetchone()
            if row_db and row_db[0]:
                try:
                    items = json.loads(row_db[0])
                    if items:
                        cached_items.extend(items)
                except Exception:
                    pass

        # Extract top distinct candidates
        candidates = []
        for it in cached_items:
            u = it.get("url") or ""
            h = urlsplit(u).hostname or ""
            if h.startswith("www."):
                h = h[4:]
            if h and not any(ex in h for ex in EXTRA_EXCLUDE) and h not in candidates:
                candidates.append(h)

        found_active_cand = None
        for cand in candidates[:3]:
            async with worker_sem:
                verified_dom = await verify_candidate(cand, r, config, fetcher)
            if verified_dom:
                found_active_cand = verified_dom
                break

        if found_active_cand:
            if not r.get("Original Legacy Domain"):
                r["Original Legacy Domain"] = legacy
            r["Domain URL"] = found_active_cand
            r["Domain Status"] = "REPLACED_INACTIVE"
            r["reason"] = f"Replaced inactive legacy domain with verified active domain {found_active_cand}"
            r["confidence"] = "HIGH"
            replaced_count += 1
            LOG.info("[REPLACED #%d] Row %d: '%s' -> %s (Old: %s)",
                     replaced_count, r["_row_idx"], org, found_active_cand, legacy)

        processed_count += 1
        if processed_count % 100 == 0 or processed_count == total_target:
            elapsed = max(0.1, time.time() - t0)
            LOG.info("Progress: %d / %d evaluated (%.1f rows/s) | Inactive Replaced: %d",
                     processed_count, total_target, processed_count / elapsed, replaced_count)

    # Process in batches of 1000 with fast CSV checkpointing
    for i in range(0, total_target, batch_size):
        chunk = inactive_rows[i : i + batch_size]
        LOG.info("--- Starting Inactive Replacement Batch %d/%d (%d rows) ---",
                 (i // batch_size) + 1, (total_target + batch_size - 1) // batch_size, len(chunk))

        await asyncio.gather(*(process_row(row) for row in chunk))

        # Fast Checkpoint save to CSV
        save_csv_checkpoint(all_rows, csv_path)

    LOG.info("==================================================")
    LOG.info("INACTIVE REPLACEMENT PIPELINE COMPLETE")
    LOG.info("Total Inactive Domains Replaced: %d / %d", replaced_count, total_target)
    save_master_files(all_rows, excel_path, csv_path)
    LOG.info("Final Master Files successfully saved.")
    LOG.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
