"""Domain investigation and row-level evidence reuse."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import replace
from urllib.parse import urlsplit
from pathlib import Path

from .cache import SQLiteCache
from .config import Config
from .evidence import external_evidence_from_file, search_authoritative, website_evidence
from .extractor import extract_page, relevant_links
from .fetcher import TieredFetcher, dns_status, utc_now
from .input import iter_input
from .models import DomainRecord
from .normalization import normalize_domain, registered_domain
from .verification import verify_row
from .proof_ladder import legal_identity_key

LOG = logging.getLogger(__name__)
EVIDENCE_VERSION = "strict-proof-v3"


async def investigate_domain(domain: str, config: Config, fetcher: TieredFetcher) -> DomainRecord:
    root = "https://" + domain + "/"
    record = DomainRecord(domain, registered_domain(domain), root, checked_at=utc_now())
    record.dns_status = await dns_status(domain)
    if record.dns_status == "UNSAFE_ADDRESS":
        record.blocked_reason = "DNS returned a private or non-global address"
        record.fetch_error_type = "UNSAFE_ADDRESS"
        return record
    if record.dns_status in ("FAILED", "NXDOMAIN"):
        # Fallback: try www.{domain} before giving up (Gap 6)
        www_domain = "www." + domain if not domain.startswith("www.") else domain
        www_dns = await dns_status(www_domain)
        if www_dns in ("FAILED", "NXDOMAIN"):
            record.http_status = 0
            record.domain_active = False
            record.fetch_error = "DNS resolution failed (host not found)"
            record.fetch_error_type = "NXDOMAIN" if record.dns_status == "NXDOMAIN" and www_dns == "NXDOMAIN" else "DNS"
            return record
        # www resolves — use it instead
        root = "https://" + www_domain + "/"
        record.dns_status = www_dns
    home = await fetcher.fetch(root)
    # Fallback: try www. variant if apex returned error (Gap 6)
    if home.status in (0,) or (home.status >= 400 and not domain.startswith("www.")):
        www_root = "https://www." + domain + "/"
        www_home = await fetcher.fetch(www_root)
        if www_home.status and (200 <= www_home.status < 400 or (www_home.status in (403, 429) and home.status == 0)):
            home = www_home
            root = www_root
    # Fallback: try http:// if https:// failed completely (Gap 6)
    if home.status == 0 or (home.error_type == "TLS"):
        http_root = "http://" + domain + "/"
        http_home = await fetcher.fetch(http_root)
        if http_home.status and http_home.status > 0:
            home = http_home
            root = http_root
    if home.status in (403, 429) and config.use_stealth:
        home = await fetcher.fetch(root, allow_browser=True, use_stealth=True)
    page = extract_page(home.final_url or root, home.html, home.status)
    if page["js_shell"] and config.use_dynamic:
        home = await fetcher.fetch(root, allow_browser=True, force_browser=True)
        page = extract_page(home.final_url or root, home.html, home.status)
    record.http_status = home.status
    record.final_url = home.final_url
    record.final_registered_domain = registered_domain(urlsplit(home.final_url).hostname or "")
    record.redirect_chain = home.redirect_chain
    record.fetch_method = home.method
    record.fetch_attempts = home.attempts
    record.fetch_error = home.error
    record.fetch_error_type = home.error_type
    record.https_available = home.final_url.startswith("https://") if home.final_url else None
    record.domain_active = (200 <= home.status < 400) and not page["parked"] if home.status else None
    record.parked = page["parked"]
    if home.status in (403, 429):
        record.blocked_reason = f"HTTP {home.status} after enabled fetch tiers"
    elif home.error_type == "ROBOTS":
        record.blocked_reason = home.error
    if home.status < 200 or home.status >= 400:
        return record
    record.pages.append(page)
    for url in relevant_links(page, config.max_evidence_pages):
        fetched = await fetcher.fetch(url)
        record.fetch_attempts += fetched.attempts
        if 200 <= fetched.status < 400:
            record.pages.append(extract_page(fetched.final_url or url, fetched.html, fetched.status))
    return record


async def network_health() -> bool:
    results = await asyncio.gather(dns_status("example.com"), dns_status("cloudflare.com"))
    return any(result == "RESOLVED" for result in results)


async def process_records(records: list[tuple[int, dict, dict]], config: Config, cache,
                          *, dry_run: bool = False, external_evidence_file: str | None = None,
                          search_ambiguous: bool = False, proxy_url: str | None = None,
                          batch_domains: int = 50, fetch_domains: bool = True,
                          network_healthy: bool | None = None,
                          max_external_searches: int = 100,
                          reviewer_decisions_file: str | None = None,
                          fetcher: TieredFetcher | None = None) -> tuple[list[dict], dict]:
    config.validate()
    fetcher = fetcher or TieredFetcher(config, proxy_url)
    healthy = (await network_health() if fetch_domains else True) if network_healthy is None else network_healthy
    normalized_by_row = [(row_id, original, canonical, normalize_domain(canonical.get("Domain Name")))
                         for row_id, original, canonical in records]
    review_decisions: dict[tuple[str, str, str], dict] = {}
    if reviewer_decisions_file:
        with Path(reviewer_decisions_file).open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    item = json.loads(line)
                    key = (legal_identity_key(str(item.get("organization") or "")),
                           str(item.get("country") or "").upper(),
                           normalize_domain(item.get("domain")).normalized_domain)
                    if all(key):
                        review_decisions[key] = item
    domains = list(dict.fromkeys(n.normalized_domain for _, _, _, n in normalized_by_row if n.normalized_domain))
    # One domain record can serve many row-level entity decisions.
    domain_records: dict[str, DomainRecord] = {}
    queue: asyncio.Queue[str] = asyncio.Queue()
    for domain in domains:
        cached = await cache.get(domain)
        if cached:
            domain_records[domain] = cached
        elif fetch_domains:
            await queue.put(domain)
        else:
            domain_records[domain] = DomainRecord(domain, registered_domain(domain), "https://" + domain + "/")

    total_domains = len(domains)
    completed_domains = len(domain_records)
    lock = asyncio.Lock()

    async def domain_worker():
        nonlocal completed_domains
        while True:
            try:
                domain = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                record = await investigate_domain(domain, config, fetcher)
                domain_records[record.domain] = record
                await cache.put(record)
            except Exception as e:
                LOG.warning("Error investigating %s: %s", domain, e)
            finally:
                queue.task_done()
                async with lock:
                    completed_domains += 1
                    if completed_domains % 50 == 0 or completed_domains == total_domains:
                        LOG.info("investigated %s/%s domains", completed_domains, total_domains)

    num_workers = min(config.concurrency, max(1, queue.qsize()))
    if num_workers > 0:
        await asyncio.gather(*(domain_worker() for _ in range(num_workers)))
    LOG.info("completed domain investigations (%s total)", len(domain_records))

    if fetch_domains and config.use_apify and getattr(config, "apify_token", None):
        blocked_domains = [d for d, r in domain_records.items()
                           if (r.blocked_reason or r.http_status in (403, 429)) and not r.pages and r.dns_status != "FAILED"]
        if blocked_domains:
            LOG.info("Escalating %d blocked domains to Apify website-content-crawler...", len(blocked_domains))
            from .apify_escalation import fetch_urls_with_apify
            async def _handle_apify_batch(batch: list[str]) -> None:
                batch_urls = [f"https://{d}/" for d in batch]
                try:
                    apify_records = await fetch_urls_with_apify(batch_urls, token=config.apify_token)
                    for rec in apify_records:
                        if rec.status == 200 and rec.html:
                            host = urlsplit(rec.requested_url).hostname or ""
                            if host.startswith("www."):
                                host = host[4:]
                            matched_domain = host if host in batch else next((bd for bd in batch if host == "www." + bd), None)
                            if matched_domain and matched_domain in domain_records:
                                old = domain_records[matched_domain]
                                extracted = extract_page(rec.final_url or rec.requested_url, rec.html, 200)
                                new_rec = replace(old,
                                                  http_status=200,
                                                  final_url=rec.final_url or old.final_url,
                                                  final_registered_domain=registered_domain(host) or old.final_registered_domain,
                                                  domain_active=True,
                                                  blocked_reason=None,
                                                  fetch_error=None,
                                                  fetch_error_type=None,
                                                  fetch_method="APIFY_CRAWLER",
                                                  pages=old.pages + [extracted])
                                domain_records[matched_domain] = new_rec
                                await cache.put(new_rec)
                                LOG.info("Apify successfully unblocked %s (title=%s)", matched_domain, extracted.get("title", "")[:40])
                except Exception as exc:
                    LOG.warning("Apify unblocking batch error: %s", exc)

            batches = [blocked_domains[i:i + 20] for i in range(0, len(blocked_domains), 20)]
            await asyncio.gather(*(_handle_apify_batch(b) for b in batches))

    path_records = {}
    path_urls = list(dict.fromkeys(
        n.requested_url for _, _, _, n in normalized_by_row
        if n.requested_url and urlsplit(n.requested_url).path not in ("", "/")
        and (n.normalized_domain not in domain_records or domain_records[n.normalized_domain].dns_status != "FAILED")
    ))
    path_queue: asyncio.Queue[str] = asyncio.Queue()
    for url in path_urls:
        cached = await cache.get_url(url)
        if cached:
            path_records[url] = cached
        elif fetch_domains:
            await path_queue.put(url)

    async def path_worker():
        while True:
            try:
                url = path_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                record = await fetcher.fetch(url)
                path_records[record.requested_url] = record
                await cache.put_url(record)
            except Exception as e:
                LOG.warning("Error fetching path %s: %s", url, e)
            finally:
                path_queue.task_done()

    num_path_workers = min(config.concurrency, max(1, path_queue.qsize()))
    if num_path_workers > 0:
        await asyncio.gather(*(path_worker() for _ in range(num_path_workers)))
    results: list[dict] = []
    evidence_cache: dict[tuple[str, str], list] = {}
    searches = 0
    for row_id, original, canonical, normalized in normalized_by_row:
        domain = domain_records.get(normalized.normalized_domain)
        target = path_records.get(normalized.requested_url)
        if domain and target:
            extra = extract_page(target.final_url or target.requested_url, target.html, target.status)
            domain = replace(domain, pages=domain.pages + ([extra] if 200 <= target.status < 400 else []),
                             final_url=target.final_url or domain.final_url,
                             final_registered_domain=registered_domain(urlsplit(target.final_url).hostname or "") or domain.final_registered_domain,
                             http_status=target.status or domain.http_status,
                             redirect_chain=target.redirect_chain or domain.redirect_chain,
                             fetch_method=target.method,
                             fetch_attempts=domain.fetch_attempts + target.attempts)
        key = (EVIDENCE_VERSION, normalized.normalized_domain, str(canonical.get("Organization Name") or ""),
               normalized.requested_url if target else "",
               (domain.checked_at if domain else "") + (target.checked_at if target else ""),
               str(canonical.get("Country") or ""))
        if key not in evidence_cache:
            evidence = await cache.get_pair(key) if domain else None
            if evidence is None:
                evidence = website_evidence(domain.pages, key[2], key[1],
                                            str(canonical.get("Country") or "")) if domain else []
                evidence += external_evidence_from_file(external_evidence_file, key[2], key[1])
                # Search is only for ambiguous pairs and only if explicitly enabled.
                if search_ambiguous and domain and searches < max_external_searches and not any(e.relationship in ("exact_entity", "group_entity") and e.strength in ("STRONG", "VERY_STRONG") for e in evidence):
                    evidence += await search_authoritative(key[2], key[1], fetcher)
                    searches += 1
                if domain:
                    await cache.put_pair(key, evidence)
            evidence_cache[key] = evidence
        reviewer_key = (legal_identity_key(str(canonical.get("Organization Name") or "")),
                        str(canonical.get("Country") or "").upper(), normalized.normalized_domain)
        result = verify_row(canonical, normalized, domain, evidence_cache[key], network_healthy=healthy,
                            reviewer_decision=review_decisions.get(reviewer_key))
        result = {**original, **{k: v for k, v in result.items() if k not in canonical},
                  "input_row_id": row_id, "organization_for_verification": key[2]}
        results.append(result)
        if dry_run:
            LOG.info("dry-run row=%s organization=%s domain=%s status=%s reason=%s",
                     row_id, key[2], key[1],
                     result.get("classification") or result.get("verification_status"),
                     result.get("decision_reason") or result.get("verification_reason"))
    return results, {"unique_domains": len(domains), "network_healthy": healthy,
                     "external_searches": searches,
                     "fetched_domains": sum(bool(d.http_status) for d in domain_records.values())}


def _quick_host(val: object) -> tuple[str, bool]:
    if not val:
        return "", False
    s = str(val).strip().lower()
    if not s:
        return "", False
    if s.startswith("http://"):
        s = s[7:]
    elif s.startswith("https://"):
        s = s[8:]
    parts = s.split("/", 1)
    host = parts[0].strip()
    if host.startswith("www."):
        host = host[4:]
    has_path = len(parts) > 1 and bool(parts[1].strip())
    return host, has_path


def representative_rows(path: str, count: int, organization_column: str | None = None,
                        domain_column: str | None = None, seed: int = 42) -> list[tuple[int, dict, dict]]:
    import random
    import re
    records = list(iter_input(path, organization_column, domain_column))
    if count <= 0 or count >= len(records):
        return records

    rng = random.Random(seed)
    quick_info = [(_quick_host(rec[2].get("Domain Name")), rec) for rec in records]
    domain_counts = Counter(host for (host, _), _ in quick_info if host)

    selected_indices: set[int] = set()
    selected_records: list[tuple[int, dict, dict]] = []

    def add_rec(idx: int, rec: tuple[int, dict, dict]) -> bool:
        if len(selected_records) >= count:
            return False
        if idx not in selected_indices:
            selected_indices.add(idx)
            selected_records.append(rec)
            return True
        return False

    # 1. Blank / missing domains (~2.5% of sample, up to 100 rows)
    target_blanks = min(100, max(1, int(count * 0.025)))
    blank_recs = [rec for (host, _), rec in quick_info if not host]
    rng.shuffle(blank_recs)
    for rec in blank_recs[:target_blanks]:
        add_rec(rec[0], rec)

    # 2. Path domains (~6% of sample, up to 250 rows)
    target_paths = min(250, max(1, int(count * 0.06)))
    path_recs = [rec for (_, has_path), rec in quick_info if has_path]
    rng.shuffle(path_recs)
    for rec in path_recs[:target_paths]:
        add_rec(rec[0], rec)

    # 3. Top shared corporate domains (panasonic, harveynorman, moph, keppel, 3m, lendlease, etc.)
    top_domains = [d for d, _ in domain_counts.most_common(25)]
    for dom in top_domains:
        dom_recs = [rec for (host, _), rec in quick_info if host == dom]
        rng.shuffle(dom_recs)
        for rec in dom_recs[:max(1, min(10, count // 200))]:
            add_rec(rec[0], rec)

    # 4. Stratified across all countries in dataset
    by_country: dict[str, list] = {}
    for (host, _), rec in quick_info:
        c = str(rec[2].get("Country") or "UNKNOWN").upper()
        by_country.setdefault(c, []).append(rec)

    for country, items in sorted(by_country.items()):
        rng.shuffle(items)
        count_for_c = min(len(items), max(1, min(10, count // max(1, len(by_country) * 8))))
        for rec in items[:count_for_c]:
            add_rec(rec[0], rec)

    # 5. Suspicious / free-host / third-party / non-standard cases
    suspicious_patterns = re.compile(r"(blogspot|wordpress|wixsite|weebly|sites\.google|github\.io|facebook\.com|linkedin\.com|\d{5,}|[0-9-]{8,})", re.I)
    suspicious_recs = [rec for (host, _), rec in quick_info if host and suspicious_patterns.search(host)]
    rng.shuffle(suspicious_recs)
    for rec in suspicious_recs[:min(100, max(1, count // 50))]:
        add_rec(rec[0], rec)

    # 6. Fill remainder deterministically from entire population to reach exactly count
    all_indices = list(range(len(records)))
    rng.shuffle(all_indices)
    for idx in all_indices:
        if len(selected_records) >= count:
            break
        rec = records[idx]
        add_rec(rec[0], rec)

    return sorted(selected_records[:count], key=lambda r: r[0])
