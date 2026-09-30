"""Apify cloud actor escalation for blocked domains and authoritative discovery."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .models import FetchRecord

LOG = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_apify_client(token: str | None = None):
    try:
        from apify_client import ApifyClient
        api_token = token or os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
        if not api_token:
            return None
        return ApifyClient(api_token)
    except ImportError:
        LOG.warning("apify-client is not installed")
        return None


def _sync_fetch_urls(client, urls: list[str]) -> list[FetchRecord]:
    if not urls:
        return []
    start_urls = [{"url": u} for u in urls]
    LOG.info("Escalating %d URLs to Apify website-content-crawler...", len(urls))
    try:
        run = client.actor("apify/website-content-crawler").call(
            run_input={
                "startUrls": start_urls,
                "maxCrawlPages": len(urls),
                "maxCrawlDepth": 0,
                "saveHtml": True,
                "removeElementsCssSelector": "",
                "htmlTransformer": "none",
                "initialConcurrency": 20,
                "maxConcurrency": 50,
                "requestTimeoutSecs": 30,
                "maxRequestRetries": 1,
            }
        )
        status = getattr(run, "status", None) or (run.get("status") if hasattr(run, "get") else "UNKNOWN")
        if status != "SUCCEEDED":
            LOG.warning("Apify crawler run did not succeed: %s", status)
            return []

        dataset_id = getattr(run, "default_dataset_id", None) or (run.get("defaultDatasetId") if hasattr(run, "get") else None)
        if not dataset_id:
            LOG.warning("No default dataset ID in Apify run")
            return []
        dataset_items = client.dataset(dataset_id).list_items().items
        by_url: dict[str, dict] = {}
        for item in dataset_items:
            u = item.get("url") or ""
            if u:
                by_url[u.rstrip("/")] = item

        records: list[FetchRecord] = []
        for req_url in urls:
            normalized_req = req_url.rstrip("/")
            match = by_url.get(normalized_req)
            if not match:
                for k, v in by_url.items():
                    if urlsplit(k).hostname == urlsplit(req_url).hostname:
                        match = v
                        break
            if match:
                html_text = match.get("html") or match.get("text") or ""
                # Wrap plain text in simple HTML body if only text was returned
                if not html_text.strip().startswith("<"):
                    title = match.get("metadata", {}).get("title") or match.get("title") or ""
                    html_text = f"<html><head><title>{title}</title></head><body>{html_text}</body></html>"
                rec = FetchRecord(
                    requested_url=req_url,
                    final_url=match.get("url") or req_url,
                    status=200,
                    html=html_text,
                    redirect_chain=[req_url, match.get("url")] if match.get("url") and match.get("url") != req_url else [req_url],
                    method="APIFY_CRAWLER",
                    attempts=1,
                    checked_at=utc_now()
                )
                records.append(rec)
            else:
                records.append(FetchRecord(
                    requested_url=req_url,
                    status=0,
                    error="Apify crawl returned no item for URL",
                    error_type="NOT_FOUND",
                    method="APIFY_CRAWLER",
                    attempts=1,
                    checked_at=utc_now()
                ))
        return records
    except Exception as e:
        LOG.warning("Error running Apify website-content-crawler: %s", e)
        return []


async def fetch_urls_with_apify(urls: list[str], token: str | None = None) -> list[FetchRecord]:
    client = get_apify_client(token)
    if not client:
        return []
    return await asyncio.to_thread(_sync_fetch_urls, client, urls)


def _sync_search_apify(client, query: str, max_results: int = 5) -> list[dict]:
    try:
        run = client.actor("apify/google-search-scraper").call(
            run_input={
                "queries": query,
                "maxPagesPerQuery": 1,
                "resultsPerPage": max_results,
            }
        )
        status = getattr(run, "status", None) or (run.get("status") if hasattr(run, "get") else "UNKNOWN")
        if status != "SUCCEEDED":
            return []
        dataset_id = getattr(run, "default_dataset_id", None) or (run.get("defaultDatasetId") if hasattr(run, "get") else None)
        if not dataset_id:
            return []
        items = client.dataset(dataset_id).list_items().items
        results: list[dict] = []
        for it in items:
            for org in it.get("organicResults", []):
                results.append({
                    "url": org.get("url", ""),
                    "title": org.get("title", ""),
                    "description": org.get("description", "")
                })
        return results[:max_results]
    except Exception as e:
        LOG.warning("Apify Google search scraper error: %s", e)
        return []


async def search_with_apify(query: str, token: str | None = None, max_results: int = 5) -> list[dict]:
    client = get_apify_client(token)
    if not client:
        return []
    return await asyncio.to_thread(_sync_search_apify, client, query, max_results)
