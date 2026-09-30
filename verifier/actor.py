"""Apify Actor entry point with persistent domain cache and idempotent resume."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from .cache import ApifyCache
from .config import Config
from .fetcher import TieredFetcher
from .input import iter_input
from .pipeline import network_health, process_records, representative_rows


async def _existing_ids(dataset, field: str) -> set[str]:
    ids: set[str] = set()
    async for item in dataset.iterate_items(fields=[field]):
        if field in item:
            ids.add(str(item[field]))
    return ids


async def _summary_from_dataset(dataset) -> dict:
    counts: dict[str, int] = {}
    seen_domains: set[str] = set()
    fetched_domains: set[str] = set()
    failed_domains: set[str] = set()
    high_confidence = 0
    total = 0
    not_attempted: set[str] = set()
    async for row in dataset.iterate_items(fields=["verification_status", "normalized_domain", "http_status", "confidence", "fetch_attempts", "dns_status"]):
        total += 1
        status = row.get("verification_status", "UNVERIFIED")
        counts[status] = counts.get(status, 0) + 1
        domain = row.get("normalized_domain", "")
        if domain:
            seen_domains.add(domain)
            if row.get("http_status"):
                fetched_domains.add(domain)
            elif row.get("fetch_attempts") or row.get("dns_status") in ("FAILED", "UNSAFE_ADDRESS"):
                failed_domains.add(domain)
            else:
                not_attempted.add(domain)
        high_confidence += row.get("confidence") == "HIGH"
    return {"total_rows": total, "unique_domains": len(seen_domains),
            "domains_successfully_fetched": len(fetched_domains),
            "domains_failed": len(failed_domains), "domains_not_attempted": len(not_attempted),
            "status_counts": counts,
            "fetch_success_rate": len(fetched_domains) / (len(fetched_domains) + len(failed_domains)) if fetched_domains or failed_domains else None,
            "verification_rate": (counts.get("VERIFIED_EXACT", 0) + counts.get("VERIFIED_GROUP", 0)) / total if total else 0,
            "high_confidence_rate": high_confidence / total if total else 0,
            "ground_truth_accuracy": None}


def _input_path(value: str) -> tuple[Path, bytes]:
    if value.startswith("https://"):
        with urlopen(value, timeout=60) as response:
            data = response.read(100_000_000 + 1)
        if len(data) > 100_000_000:
            raise ValueError("Input exceeds 100 MB")
        suffix = Path(urlsplit(value).path).suffix.lower()
        if suffix not in (".csv", ".xlsx"):
            raise ValueError("Input URL must end in .csv or .xlsx")
        path = Path(tempfile.gettempdir()) / ("domain-input-" + hashlib.sha256(data).hexdigest()[:12] + suffix)
        path.write_bytes(data)
        return path, data
    path = Path(value)
    data = path.read_bytes()
    return path, data


async def main() -> None:
    from apify import Actor

    async with Actor:
        settings = await Actor.get_input() or {}
        input_file = settings.get("input_file")
        if not input_file:
            raise ValueError("input_file is required")
        input_path, raw = await asyncio.to_thread(_input_path, input_file)
        run_id = hashlib.sha256(raw + json.dumps({
            "pipeline_version": 2,
            "organization_column": settings.get("organization_column"),
            "domain_column": settings.get("domain_column"),
            "dry_run": settings.get("dry_run", True),
            "sample_size": settings.get("sample_size", 75),
            "use_dynamic": settings.get("use_dynamic", False),
            "use_stealth": settings.get("use_stealth", False),
            "use_proxy": settings.get("use_proxy", False),
            "search_ambiguous": settings.get("search_ambiguous", False),
            "run_key": settings.get("run_key", ""),
        }, sort_keys=True).encode()).hexdigest()[:20]
        result_dataset = await Actor.open_dataset(name="domain-results-" + run_id)
        evidence_dataset = await Actor.open_dataset(name="domain-evidence-" + run_id)
        review_dataset = await Actor.open_dataset(name="domain-review-" + run_id)
        store = await Actor.open_key_value_store(name="domain-cache-" + run_id)
        config = Config(
            concurrency=int(settings.get("concurrency", 20)),
            per_host_delay_seconds=float(settings.get("per_host_delay_seconds", 1.0)),
            timeout_seconds=int(settings.get("timeout_seconds", 18)),
            max_evidence_pages=int(settings.get("max_evidence_pages", 7)),
            use_dynamic=bool(settings.get("use_dynamic", False)),
            use_stealth=bool(settings.get("use_stealth", False)),
            use_proxy=bool(settings.get("use_proxy", False)),
        )
        proxy_url = None
        if config.use_proxy:
            proxy = await Actor.create_proxy_configuration(actor_proxy_input=settings.get("proxy"))
            if proxy is None:
                raise RuntimeError("Apify Proxy requested but not configured")
            proxy_url = await proxy.new_url()
        cache = ApifyCache(store, config.cache_ttl_seconds)
        fetcher = TieredFetcher(config, proxy_url)
        if settings.get("dry_run", True):
            sample_size = int(settings.get("sample_size", 75))
            if not 50 <= sample_size <= 100:
                raise ValueError("Dry-run sample size must be 50..100")
            records = representative_rows(str(input_path), sample_size,
                                          settings.get("organization_column"), settings.get("domain_column"))
        else:
            records = list(iter_input(input_path, settings.get("organization_column"),
                                      settings.get("domain_column")))
        result_ids = await _existing_ids(result_dataset, "input_row_id")
        evidence_ids = await _existing_ids(evidence_dataset, "evidence_id")
        review_ids = await _existing_ids(review_dataset, "input_row_id")
        healthy = await network_health()
        external_searches_remaining = int(settings.get("max_external_searches", 1000))
        progress = await store.get_value("PROGRESS") or {}
        start_index = int(progress.get("processed_rows", 0)) if progress.get("run_id") == run_id else 0
        if start_index:
            external_searches_remaining = int(progress.get("external_searches_remaining", external_searches_remaining))
        for start in range(start_index, len(records), 250):
            chunk = records[start:start + 250]
            # Every chunk is re-evaluated on resume, but domain cache prevents repeat crawls.
            output, info = await process_records(chunk, config, cache,
                                                  dry_run=bool(settings.get("dry_run", True)),
                                                  search_ambiguous=bool(settings.get("search_ambiguous", False)),
                                                  proxy_url=proxy_url, batch_domains=50,
                                                  network_healthy=healthy,
                                                  max_external_searches=external_searches_remaining,
                                                  fetcher=fetcher)
            output = json.loads(json.dumps(output, ensure_ascii=False, default=str))
            external_searches_remaining -= info["external_searches"]
            missing = [r for r in output if str(r["input_row_id"]) not in result_ids]
            if missing:
                await result_dataset.push_data(missing)
                result_ids.update(str(r["input_row_id"]) for r in missing)
            evidence_items = []
            for row in output:
                for index, evidence in enumerate(row.get("evidence_json", [])):
                    evidence_id = f'{row["input_row_id"]}-{index}'
                    if evidence_id not in evidence_ids:
                        evidence_items.append({"evidence_id": evidence_id,
                                               "input_row_id": row["input_row_id"],
                                               "organization": row["organization_for_verification"],
                                               "domain": row["normalized_domain"],
                                               "evidence_url": evidence["url"],
                                               "evidence_type": evidence["evidence_type"],
                                               "evidence_text": evidence["text"],
                                               "relationship": evidence["relationship"],
                                               "strength": evidence["strength"],
                                               "source_title": evidence.get("title", ""),
                                               "source_type": evidence.get("source_type", "website")})
            if evidence_items:
                await evidence_dataset.push_data(evidence_items)
                evidence_ids.update(item["evidence_id"] for item in evidence_items)
            review_items = [r for r in output if r["verification_status"] in
                            ("MISMATCH", "PROBABLE", "UNVERIFIED", "BLOCKED", "REDIRECT")
                            and str(r["input_row_id"]) not in review_ids]
            if review_items:
                await review_dataset.push_data(review_items)
                review_ids.update(str(r["input_row_id"]) for r in review_items)
            await store.set_value("PROGRESS", {"processed_rows": min(start + len(chunk), len(records)),
                                               "expected_rows": len(records), "run_id": run_id,
                                               "external_searches_remaining": external_searches_remaining,
                                               "result_dataset_id": result_dataset.id,
                                               "evidence_dataset_id": evidence_dataset.id,
                                               "review_dataset_id": review_dataset.id})
        summary = await _summary_from_dataset(result_dataset)
        if summary["total_rows"] != len(records):
            raise RuntimeError(f'Result dataset has {summary["total_rows"]} rows; expected {len(records)}')
        await store.set_value("SUMMARY", {**summary,
                                          "result_dataset_id": result_dataset.id,
                                          "evidence_dataset_id": evidence_dataset.id,
                                          "review_dataset_id": review_dataset.id,
                                          "dry_run": bool(settings.get("dry_run", True))})
        Actor.log.info("Completed %s rows; result dataset %s", len(records), result_dataset.id)


if __name__ == "__main__":
    asyncio.run(main())
