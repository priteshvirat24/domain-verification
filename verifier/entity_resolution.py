"""Extract candidate identities; candidates are signals, not proof."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .normalization import normalize_name

RELATION = re.compile(r"\b(subsidiary|subsidiaries|affiliate|affiliated|owned by|part of|member of|operated by|division of|group compan(?:y|ies))\b", re.I)
PARENT = re.compile(r"(?:part of|owned by|subsidiary of|member of|division of)\s+(?:the\s+)?([\w&.,\- ]{3,90})", re.I)
LEGAL_NAME = re.compile(r"\b([A-Z][\w&.,'\- ]{3,90}\s(?:Ltd\.?|Limited|Pte\.? Ltd\.?|Pty\.? Ltd\.?|Inc\.?|Corporation|GmbH|Sdn\.? Bhd\.?|LLC|PLC))\b", re.I)


def candidate_identity(pages: list[dict]) -> dict[str, object]:
    names: list[str] = []
    legal: list[str] = []
    parents: list[str] = []
    brands: list[str] = []
    countries: list[str] = []
    email_domains: set[str] = set()
    registrations: list[str] = []
    for page in pages:
        for schema in page.get("jsonld", []):
            for value in (schema.get("name"),):
                if isinstance(value, str):
                    names.append(value)
            value = schema.get("legalName")
            if isinstance(value, str):
                legal.append(value)
            value = schema.get("parentOrganization")
            if isinstance(value, dict):
                value = value.get("name")
            if isinstance(value, str):
                parents.append(value)
            value = schema.get("brand")
            if isinstance(value, dict):
                value = value.get("name")
            if isinstance(value, str):
                brands.append(value)
            address = schema.get("address")
            if isinstance(address, dict) and address.get("addressCountry"):
                countries.append(str(address["addressCountry"]))
        legal.extend(LEGAL_NAME.findall(page.get("footer", "")))
        legal.extend(LEGAL_NAME.findall(page.get("visible_text", "")[:12000])[:10])
        parents.extend(PARENT.findall(page.get("visible_text", "")[:15000])[:5])
        for email in page.get("emails", []):
            email_domains.add(email.rsplit("@", 1)[-1].lower())
        registrations.extend(page.get("registrations", []))
    def uniq(items: list[str]) -> list[str]:
        return list(dict.fromkeys(x.strip() for x in items if x and normalize_name(x)))[:20]
    return {"company_names": uniq(names), "legal_entities": uniq(legal),
            "parents": uniq(parents), "brands": uniq(brands),
            "countries": uniq(countries), "email_domains": sorted(email_domains),
            "registrations": uniq(registrations)}


LEGAL_SUFFIX_MAP = [
    (r"\b(?:pte\.?\s*ltd\.?|private\s+limited)\b", r"(?:pte\.?\s*ltd\.?|private\s+limited)"),
    (r"\b(?:pty\.?\s*ltd\.?|proprietary\s+limited)\b", r"(?:pty\.?\s*ltd\.?|proprietary\s+limited)"),
    (r"\b(?:co\.?,?\s*ltd\.?|company\s+limited)\b", r"(?:co\.?,?\s*ltd\.?|company\s+limited)"),
    (r"\b(?:ltd\.?|limited)\b", r"(?:ltd\.?|limited)"),
    (r"\b(?:inc\.?|incorporated)\b", r"(?:inc\.?|incorporated)"),
    (r"\b(?:corp\.?|corporation)\b", r"(?:corp\.?|corporation)"),
    (r"\b(?:llc|l\.l\.c\.)\b", r"(?:llc|l\.l\.c\.)"),
    (r"\b(?:plc|p\.l\.c\.)\b", r"(?:plc|p\.l\.c\.)"),
    (r"\b(?:gmbh)\b", r"gmbh"),
    (r"\b(?:sdn\.?\s*bhd\.?)\b", r"(?:sdn\.?\s*bhd\.?)"),
]


def _flexible_entity_regex(organization: str) -> re.Pattern | None:
    cleaned = re.sub(r"\s+", " ", organization.strip())
    matched_pattern = None
    core_name = cleaned
    for pat, repl in LEGAL_SUFFIX_MAP:
        m = re.search(pat + r"$", cleaned, re.I)
        if m:
            core_name = cleaned[:m.start()].strip(" ,.-")
            matched_pattern = repl
            break
    if not core_name or len(core_name) < 3:
        return None
    words = [re.escape(w) for w in core_name.split() if w]
    core_regex = r"\s+".join(words)
    if matched_pattern:
        full_regex = core_regex + r"(?:,?\s+" + matched_pattern + r")"
    else:
        full_regex = core_regex
    return re.compile(r"\b" + full_regex + r"\b", re.I)


def entity_mention(text: str, organization: str) -> str:
    """Return the actual local context for a complete entity mention."""
    if not organization or len(normalize_name(organization)) < 4:
        return ""
    pattern = re.compile(re.escape(organization), re.I)
    match = pattern.search(text)
    if not match:
        flex = _flexible_entity_regex(organization)
        if flex:
            match = flex.search(text)
    if not match:
        return ""
    return text[max(0, match.start() - 160):match.end() + 180].strip()[:500]


def relationship_mention(text: str, organization: str) -> str:
    context = entity_mention(text, organization)
    if not context:
        return ""
    name = re.search(re.escape(organization), context, re.I)
    if not name:
        name = _flexible_entity_regex(organization).search(context) if _flexible_entity_regex(organization) else None
    if not name:
        return ""
    return context if any(abs(match.start() - name.end()) <= 120 or abs(name.start() - match.end()) <= 120
                          for match in RELATION.finditer(context)) else ""


def official_domain_link(page: dict, target_domain: str) -> str:
    """Find a website's explicit link to the target host."""
    for link in page.get("links", []):
        if (urlsplit(link["url"]).hostname or "").lower() == target_domain.lower():
            return link["url"]
    return ""
