"""
Pre-crawl script: Re-fetch domains that are cached but have no page content.
These are domains where the first crawl returned empty HTML (timeout, JS-shell, etc.)
Uses maximum concurrency to clear these quickly before the main 10,000-row run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from urllib.parse import urlsplit

from verifier.cache import SQLiteCache
from verifier.config import Config
from verifier.extractor import extract_page
from verifier.fetcher import TieredFetcher, dns_status, utc_now
from verifier.models import DomainRecord
from verifier.pipeline import investigate_domain

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CACHE_PATH = "verifier/live_validation_4000_cache.sqlite"
CONCURRENCY = 200  # Maximum parallel workers


def get_refetchable_domains(cache_path: str) -> list[str]:
    """Find cached domains that have no page content but are potentially alive."""
    conn = sqlite3.connect(cache_path)
    cur = conn.cursor()
    refetchable = []
    all_rows = cur.execute("SELECT domain, payload FROM domains").fetchall()
    for domain, payload in all_rows:
        data = json.loads(payload)
        pages = data.get("pages", [])
        http = data.get("http_status", 0)
        dns = data.get("dns_status", "UNKNOWN")
        blocked = data.get("blocked_reason", "")
        fetch_err = data.get("fetch_error_type", "")
        parked = data.get("parked", False)
        has_content = any(p.get("visible_text") for p in pages)
        # Re-fetchable: DNS ok, not parked, no content, not hard-failed
        if (dns != "FAILED" and not parked and not has_content
                and fetch_err not in ("ROBOTS", "UNSAFE_ADDRESS")
                and http not in (410, 404)):
            refetchable.append(domain)
    conn.close()
    return refetchable


async def main():
    domains = get_refetchable_domains(CACHE_PATH)
    LOG.info("Found %d domains to re-fetch with page content", len(domains))

    config = Config(
        concurrency=CONCURRENCY,
        max_evidence_pages=4,
        use_dynamic=False,
        use_stealth=False,
        use_proxy=False,
        use_apify=True,
        apify_token=os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN"),
    )

    cache = SQLiteCache(CACHE_PATH, config.cache_ttl_seconds)
    fetcher = TieredFetcher(config, proxy_url=None)

    queue: asyncio.Queue[str] = asyncio.Queue()
    for d in domains:
        await queue.put(d)

    total = len(domains)
    completed = 0
    improved = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal completed, improved
        while True:
            try:
                domain = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                record = await investigate_domain(domain, config, fetcher)
                has_content = any(p.get("visible_text") for p in record.pages)
                await cache.put(record)
                async with lock:
                    completed += 1
                    if has_content:
                        improved += 1
                    if completed % 100 == 0 or completed == total:
                        LOG.info(
                            "Progress: %d/%d domains re-fetched | %d improved (have content now)",
                            completed, total, improved
                        )
            except Exception as e:
                LOG.warning("Error re-fetching %s: %s", domain, e)
            finally:
                queue.task_done()

    num_workers = min(CONCURRENCY, total)
    LOG.info("Starting %d parallel workers to re-fetch %d domains...", num_workers, total)
    await asyncio.gather(*(worker() for _ in range(num_workers)))
    cache.close()
    LOG.info("Re-fetch complete: %d/%d domains now have page content.", improved, total)


if __name__ == "__main__":
    asyncio.run(main())
