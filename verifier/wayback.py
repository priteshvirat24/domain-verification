"""Wayback Machine / Internet Archive Snapshot Inspection.

Implements Recommendation 2.3:
"Use the old website's history for dead domains. The free Internet Archive (web.archive.org)
usually keeps copies of old websites. The last copy often shows a 'we have moved' notice,
a link to the new site or the company's exact name, so check it before searching."
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import urllib.request
import urllib.error

LOG = logging.getLogger("wayback")

WAYBACK_AVAILABILITY_API = "https://archive.org/wayback/available"

MOVE_PATTERNS = [
    re.compile(r"\b(?:we(?:\s+have|\s+'ve)?\s+moved\s+to|our\s+new\s+website\s+is|visit\s+us\s+at|redirecting\s+to|please\s+visit)\s+(https?://[a-zA-Z0-9.-]+)", re.I),
    re.compile(r"\b(?:new\s+domain|new\s+url)\s*[:=]?\s*(https?://[a-zA-Z0-9.-]+)", re.I),
    re.compile(r"\b(?:acquired\s+by|now\s+part\s+of|merged\s+with)\s+([A-Za-z0-9&.,\s]{3,60})", re.I),
]


def check_wayback_history(domain: str, timeout: float = 5.0) -> dict[str, Any]:
    """Query Wayback Machine Availability API for the latest snapshot of a dead domain."""
    clean_domain = domain.strip().lower()
    if clean_domain.startswith("http://"):
        clean_domain = clean_domain[7:]
    elif clean_domain.startswith("https://"):
        clean_domain = clean_domain[8:]
    clean_domain = clean_domain.split("/")[0]

    api_url = f"{WAYBACK_AVAILABILITY_API}?url={clean_domain}"
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "DomainVerificationAuditor/2.0 (Audit Research Engine)"}
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return {"available": False, "error": f"HTTP {response.status}"}
            data = json.loads(response.read().decode("utf-8"))
            snapshots = data.get("archived_snapshots", {})
            closest = snapshots.get("closest", {})
            if not closest or not closest.get("available"):
                return {"available": False, "snapshot_url": "", "timestamp": ""}
            
            return {
                "available": True,
                "snapshot_url": closest.get("url", ""),
                "timestamp": closest.get("timestamp", ""),
                "status": closest.get("status", ""),
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"available": False, "error": str(e)}


def inspect_archive_snapshot_content(snapshot_html: str) -> dict[str, Any]:
    """Inspect the HTML of an archived page for relocation notices or acquisition details."""
    result = {
        "relocation_url": "",
        "acquisition_entity": "",
        "notes": "",
    }
    if not snapshot_html:
        return result

    for pat in MOVE_PATTERNS:
        m = pat.search(snapshot_html)
        if m:
            matched = m.group(1).strip()
            if matched.startswith("http"):
                result["relocation_url"] = matched
                result["notes"] = f"Relocation notice identified in archive snapshot: {matched}"
                break
            else:
                result["acquisition_entity"] = matched
                result["notes"] = f"Acquisition notice in archive snapshot: {matched}"
                break

    return result
