"""Unblock top blocked corporate domains using Apify and update the 4,000-row cache."""
import asyncio
import csv
import json
import logging
import sqlite3
import time
from collections import Counter
from urllib.parse import urlsplit

from verifier.apify_escalation import fetch_urls_with_apify
from verifier.extractor import extract_page
from dataclasses import replace
from verifier.models import DomainRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("unblock_runner")

CACHE_PATH = "verifier/live_validation_4000_cache.sqlite"
APIFY_TOKEN = "YOUR_APIFY_API_TOKEN"


async def main():
    with open("review.csv", "r", encoding="utf-8") as f:
        blocked_rows = [r for r in csv.DictReader(f) if r["verification_status"] == "BLOCKED"]

    counts = Counter(r["normalized_domain"] for r in blocked_rows if r["normalized_domain"])
    LOG.info("Found %d total BLOCKED rows covering %d unique domains", len(blocked_rows), len(counts))

    # Query domains that are currently blocked in cache
    conn = sqlite3.connect(CACHE_PATH)
    c = conn.cursor()
    currently_blocked = set()
    for d, p_str in c.execute("select domain, payload from domains").fetchall():
        p = json.loads(p_str)
        if (p.get("blocked_reason") or p.get("http_status") in (403, 429) or (p.get("http_status") == 0 and p.get("fetch_error_type") in ("TLS", "TIMEOUT"))) and not p.get("pages"):
            currently_blocked.add(d)

    blocked_counts = Counter(r["normalized_domain"] for r in blocked_rows if r["normalized_domain"] in currently_blocked)
    top_domains = [d for d, _ in blocked_counts.most_common(20)]
    LOG.info("Top domains to unblock with Apify: %s", [(d, blocked_counts[d]) for d in top_domains])

    if not top_domains:
        LOG.info("No blocked domains to unblock.")
        conn.close()
        return

    urls_to_fetch = [f"https://{d}/" for d in top_domains]
    LOG.info("Dispatching %d URLs to Apify website-content-crawler...", len(urls_to_fetch))
    
    # Run in batches of 10
    for i in range(0, len(urls_to_fetch), 10):
        batch = urls_to_fetch[i:i + 10]
        records = await fetch_urls_with_apify(batch, token=APIFY_TOKEN)
        for rec in records:
            if rec.status == 200 and rec.html:
                host = urlsplit(rec.final_url or rec.requested_url).hostname or ""
                if host.startswith("www."):
                    host = host[4:]
                matched_domain = host if host in top_domains else next((td for td in top_domains if td in host or host in td), None)
                if matched_domain:
                    # Get existing record
                    row = c.execute("select payload from domains where domain = ?", (matched_domain,)).fetchone()
                    if row:
                        old_dict = json.loads(row[0])
                        extracted = extract_page(rec.final_url or rec.requested_url, rec.html, 200)
                        old_dict["http_status"] = 200
                        old_dict["final_url"] = rec.final_url or old_dict.get("final_url")
                        old_dict["domain_active"] = True
                        old_dict["blocked_reason"] = None
                        old_dict["fetch_error"] = None
                        old_dict["fetch_error_type"] = None
                        old_dict["fetch_method"] = "APIFY_CRAWLER"
                        old_dict["pages"] = [extracted]
                        c.execute("insert or replace into domains (domain, timestamp, payload) values (?, ?, ?)",
                                  (matched_domain, time.time(), json.dumps(old_dict)))
                        conn.commit()
                        LOG.info("Successfully updated cached domain %s with Apify data! (title=%s)",
                                 matched_domain, extracted.get("title", "")[:40])

    conn.close()
    LOG.info("Apify unblocking finished.")


if __name__ == "__main__":
    asyncio.run(main())
