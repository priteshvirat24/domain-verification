"""Evidence creation and optional authoritative-source discovery."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

from .entity_resolution import entity_mention, relationship_mention
from .models import Evidence
from .normalization import normalize_name

COUNTRY_ALIASES = {
    "JP": {"jp", "japan"}, "KR": {"kr", "korea", "south korea", "republic of korea"},
    "AU": {"au", "australia"}, "SG": {"sg", "singapore"},
    "MY": {"my", "malaysia"}, "TH": {"th", "thailand"},
    "NZ": {"nz", "new zealand"}, "PH": {"ph", "philippines"},
    "ID": {"id", "indonesia"}, "VN": {"vn", "vietnam", "viet nam"},
    "IN": {"in", "india"}, "CN": {"cn", "china"},
    "US": {"us", "usa", "united states", "united states of america"},
}


def website_evidence(pages: list[dict], organization: str, domain: str,
                     country: str = "") -> list[Evidence]:
    records: list[Evidence] = []
    wanted = normalize_name(organization)
    official_names = [
        name for page in pages for schema in page.get("jsonld", [])
        if any(kind in str(schema.get("@type", "")) for kind in ("Organization", "Corporation", "LocalBusiness"))
        for name in (schema.get("name"), schema.get("legalName")) if isinstance(name, str)
    ]
    for page in pages:
        url = page["url"]
        for schema in page.get("jsonld", []):
            legal = schema.get("legalName")
            name = schema.get("name")
            if isinstance(legal, str) and normalize_name(legal) == wanted:
                records.append(Evidence(url, "organization_jsonld_legal_name", legal,
                                        "exact_entity", "VERY_STRONG", page.get("title", "")))
            elif isinstance(name, str) and normalize_name(name) == wanted:
                records.append(Evidence(url, "organization_jsonld_name", name,
                                        "exact_entity", "STRONG", page.get("title", "")))
            address = schema.get("address")
            found_country = address.get("addressCountry") if isinstance(address, dict) else None
            aliases = COUNTRY_ALIASES.get(country.upper(), {country.casefold()})
            if country and isinstance(found_country, str) and found_country.casefold() in aliases:
                records.append(Evidence(url, "organization_jsonld_country", found_country,
                                        "country_match", "MEDIUM", page.get("title", "")))
        legal_text = page.get("footer", "")
        if any(k in urlsplit(url).path.lower() for k in ("legal", "privacy", "terms", "imprint")):
            legal_text += " " + page.get("visible_text", "")
        mention = entity_mention(legal_text, organization)
        if mention:
            records.append(Evidence(url, "official_legal_text", mention,
                                    "exact_entity", "STRONG", page.get("title", "")))
        rel = relationship_mention(page.get("visible_text", ""), organization)
        if rel:
            connects_to_site = any(normalize_name(name) and normalize_name(name) in normalize_name(rel)
                                   for name in official_names)
            records.append(Evidence(url, "official_relationship_text", rel,
                                    "group_entity", "STRONG" if connects_to_site else "MEDIUM", page.get("title", "")))
        body = entity_mention(page.get("visible_text", ""), organization)
        if body and not mention and not rel:
            records.append(Evidence(url, "body_mention", body,
                                    "name_mention", "WEAK", page.get("title", "")))
        for email in page.get("emails", []):
            email_domain = email.rsplit("@", 1)[-1].lower()
            if email_domain == domain or email_domain.endswith("." + domain):
                records.append(Evidence(url, "contact_email_domain", email_domain,
                                        "site_control", "MEDIUM", page.get("title", "")))
    return list({(e.url, e.evidence_type, e.text): e for e in records}.values())


def external_evidence_from_file(path: str | None, organization: str, domain: str) -> list[Evidence]:
    """Read previously fetched, human-reviewed sources; never invent URLs."""
    if not path:
        return []
    records: list[Evidence] = []
    with open(path, encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            item = json.loads(line)
            if normalize_name(item.get("organization")) != normalize_name(organization):
                continue
            if item.get("domain", "").lower() != domain.lower():
                continue
            url = item.get("source_url", "")
            if urlsplit(url).scheme not in ("http", "https") or not item.get("fetched_at"):
                continue
            records.append(Evidence(url, item.get("source_type", "external"),
                                    str(item.get("evidence_text", ""))[:1000],
                                    item.get("relationship", "unknown"),
                                    item.get("strength", "MEDIUM"),
                                    item.get("source_title", ""), "external"))
    return records


async def search_authoritative(organization: str, domain: str, fetcher,
                               api_key: str | None = None) -> list[Evidence]:
    """Optional Tavily discovery. Search snippets are never verification evidence.

    The selected source URL is fetched through Scrapling before extracting text.
    """
    key = api_key or os.environ.get("TAVILY_API_KEY")
    apify_token = getattr(fetcher.config, "apify_token", None) or os.environ.get("APIFY_TOKEN") if hasattr(fetcher, "config") else None
    if not key and not apify_token:
        return []
    queries = [f'"{organization}" "{domain}"', f'"{organization}" official website subsidiary']
    found: list[Evidence] = []
    seen: set[str] = set()
    for query in queries:
        candidate_urls: list[str] = []
        if apify_token:
            from .apify_escalation import search_with_apify
            apify_items = await search_with_apify(query, token=apify_token, max_results=4)
            candidate_urls.extend([it["url"] for it in apify_items if it.get("url")])
        elif key:
            from scrapling.fetchers import AsyncFetcher
            try:
                response = await AsyncFetcher.post("https://api.tavily.com/search",
                                                   json={"query": query,
                                                         "search_depth": "basic", "max_results": 5},
                                                   headers={"Authorization": f"Bearer {key}",
                                                            "Content-Type": "application/json"},
                                                   timeout=20, retries=1)
                payload = response.json()
                candidate_urls.extend([it.get("url", "") for it in payload.get("results", [])])
            except Exception:
                continue
        for url in candidate_urls:
            host = (urlsplit(url).hostname or "").lower()
            if not host or url in seen or urlsplit(url).scheme not in ("http", "https"):
                continue
            seen.add(url)
            # Search is discovery only. Source text is fetched and inspected below.
            fetched = await fetcher.fetch(url)
            if fetched.status != 200:
                continue
            from .extractor import extract_page
            page = extract_page(fetched.final_url or url, fetched.html, fetched.status)
            mention = relationship_mention(page["visible_text"], organization)
            if not mention:
                mention = entity_mention(page["visible_text"], organization)
            if not mention:
                continue
            source_type = "official_parent" if host == domain or host.endswith("." + domain) else (
                "government_or_regulator" if re.search(r"\.(gov|go\.jp|gov\.au|go\.kr|gov\.sg)(?:\.|$)", host) else "external_site")
            strength = "VERY_STRONG" if source_type == "government_or_regulator" else (
                "STRONG" if source_type == "official_parent" else "MEDIUM")
            relationship = "group_entity" if relationship_mention(mention, organization) else "name_mention"
            found.append(Evidence(page["url"], source_type, mention, relationship,
                                  strength, page.get("title", ""), "external"))
            if len(found) >= 4:
                return found
    return found
