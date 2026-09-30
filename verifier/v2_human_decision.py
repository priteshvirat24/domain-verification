"""Human-researcher domain & company verification decision engine (V2).

Mimics a human analyst:
1. Normalizes company name, trailing regional/territory tags, and legal suffixes.
2. Extracts distinctive brand tokens and domain labels.
3. Evaluates multiple independent first-party and corporate signals:
   - Exact legal entity match (JSON-LD legalName, footer, terms/privacy)
   - Core entity name match (title, H1, meta description, visible text)
   - Domain-to-brand correspondence (domain name matches organization brand tokens)
   - Country / ccTLD consistency (e.g. .my for Malaysia, .nz for New Zealand)
   - Official contact & site control (email domain matches, address/phone matches)
   - Corporate group / parent-subsidiary relationship (Sales Territory Name, group portals)
   - Negative / contradictory evidence (unrelated owner, competitor, government mismatch)
4. Employs graduated confidence scoring with early-stop once threshold is satisfied.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from .models import DomainRecord, Evidence
from .normalization import NormalizedDomain, registered_domain

# Comprehensive global & regional legal suffixes
LEGAL_SUFFIXES_RE = re.compile(
    r"\b(?:"
    r"pte\.?\s*ltd\.?|pty\.?\s*ltd\.?|sdn\.?\s*bhd\.?|co\.?,?\s*ltd\.?|"
    r"private\s+limited|proprietary\s+limited|company\s+limited|"
    r"sendirian\s+berhad|berhad|bhd\.?|"
    r"limited|ltd\.?|incorporated|inc\.?|corporation|corp\.?|"
    r"llc|l\.l\.c\.|plc|p\.l\.c\.|gmbh|b\.v\.|bv|a\.g\.|ag|s\.a\.|sa|"
    r"s\.p\.a\.|s\.r\.l\.|sp\.\s*z\s*o\.o\.|s\.r\.o\.|oyj|"
    r"k\.k\.|kabushiki\s+kaisha|g\.k\.|godo\s+kaisha|y\.k\.|"
    r"joint\s+stock\s+company|jsc|"
    r"holding|holdings|group"
    r")\b",
    re.IGNORECASE,
)

# Trailing country / territory tags like ' - SG', ' - JP', ' (MALAYSIA) BERHAD'
TRAILING_TERRITORY_RE = re.compile(
    r"(?:\s*-\s*[A-Z]{2}|\s*\([A-Z]{2,}\)|\s*\([A-Z\s]+\)\s*-\s*[A-Z]{2})$",
    re.IGNORECASE,
)

# Branch / office / department qualifiers
BRANCH_QUALIFIERS_RE = re.compile(
    r"\b(?:branch|transaction\s+office|representative\s+office|operations?|division|subsidiary)\b.*$",
    re.IGNORECASE,
)

# Generic business stopwords to omit when computing distinctive brand tokens
GENERIC_STOPWORDS = {
    "and", "the", "of", "for", "in", "at", "by", "to", "co", "company",
    "services", "service", "international", "global", "national", "asia",
    "pacific", "holdings", "holding", "group", "enterprises", "enterprise",
    "industries", "industry", "technology", "technologies", "solutions",
    "management", "investments", "investment", "trading", "products", "systems",
    "commercial", "bank", "banking", "finance", "financial", "insurance",
    "logistics", "consulting", "marketing", "development", "corp", "corporation"
}

# Country TLD mapping
CCTLD_TO_COUNTRY = {
    "my": "MY", "nz": "NZ", "jp": "JP", "sg": "SG", "au": "AU",
    "th": "TH", "ph": "PH", "id": "ID", "vn": "VN", "in": "IN",
    "kr": "KR", "cn": "CN", "hk": "HK", "tw": "TW", "us": "US",
    "uk": "GB", "gb": "GB", "de": "DE", "fr": "FR", "it": "IT",
    "ca": "CA", "nl": "NL", "es": "ES", "ch": "CH", "ae": "AE",
    "za": "ZA", "br": "BR", "mx": "MX", "ru": "RU", "pk": "PK"
}

COUNTRY_ALIASES = {
    "JP": {"jp", "japan", "japanese", "tokyo", "osaka"},
    "KR": {"kr", "korea", "south korea", "republic of korea", "seoul"},
    "AU": {"au", "australia", "australian", "sydney", "melbourne", "brisbane", "perth"},
    "SG": {"sg", "singapore", "singaporean"},
    "MY": {"my", "malaysia", "malaysian", "kuala lumpur", "selangor", "penang"},
    "TH": {"th", "thailand", "thai", "bangkok"},
    "NZ": {"nz", "new zealand", "auckland", "wellington", "christchurch"},
    "PH": {"ph", "philippines", "philippine", "filipino", "manila", "makati", "quezon"},
    "ID": {"id", "indonesia", "indonesian", "jakarta"},
    "VN": {"vn", "vietnam", "vietnamese", "hanoi", "ho chi minh"},
    "IN": {"in", "india", "indian", "mumbai", "delhi", "bangalore"},
    "US": {"us", "usa", "united states", "united states of america", "america"},
    "PK": {"pk", "pakistan", "pakistani", "karachi", "lahore", "islamabad"},
}


def clean_org_profile(organization: str, territory_name: str = "") -> dict[str, Any]:
    """Extract clean entity names, legal suffixes, territory, and distinctive brand tokens."""
    orig = str(organization or "").strip()
    # 1. Remove branch / office suffix if present
    base = BRANCH_QUALIFIERS_RE.sub("", orig).strip(" ,.-")
    # 2. Remove trailing territory suffix like ' - SG'
    no_terr = TRAILING_TERRITORY_RE.sub("", base).strip(" ,.-")
    # 3. Clean legal suffix to extract core name
    core = LEGAL_SUFFIXES_RE.sub(" ", no_terr)
    core = re.sub(r"[^\w\s]", " ", core)
    core = re.sub(r"\s+", " ", core).strip()

    # Extract distinctive tokens
    tokens = [t.lower() for t in core.split() if len(t) > 1 and t.lower() not in GENERIC_STOPWORDS]
    if not tokens:
        tokens = [t.lower() for t in core.split() if len(t) > 1]

    # Clean territory parent name if provided
    parent_core = ""
    parent_tokens = []
    if territory_name:
        t_base = TRAILING_TERRITORY_RE.sub("", str(territory_name)).strip(" ,.-")
        t_core = LEGAL_SUFFIXES_RE.sub(" ", t_base)
        t_core = re.sub(r"[^\w\s]", " ", t_core)
        parent_core = re.sub(r"\s+", " ", t_core).strip()
        parent_tokens = [t.lower() for t in parent_core.split() if len(t) > 1 and t.lower() not in GENERIC_STOPWORDS]

    return {
        "original": orig,
        "clean_name": no_terr,
        "core_name": core,
        "tokens": tokens,
        "parent_core": parent_core,
        "parent_tokens": parent_tokens,
    }


def clean_domain_profile(domain: str) -> dict[str, Any]:
    """Deconstruct domain into hostname, registered root, main label, and ccTLD."""
    raw = (domain or "").lower().strip()
    if raw.startswith("www."):
        raw = raw[4:]
    reg = registered_domain(raw) or raw
    parts = reg.split(".")
    domain_label = parts[0] if parts else raw
    # Check ccTLD
    tld = parts[-1] if len(parts) > 1 else ""
    cctld_country = CCTLD_TO_COUNTRY.get(tld, "")
    if len(parts) > 2 and parts[-2] in ("co", "com", "net", "org", "gov", "edu", "ac"):
        cctld_country = CCTLD_TO_COUNTRY.get(parts[-1], cctld_country)

    return {
        "host": raw,
        "registered_domain": reg,
        "domain_label": domain_label,
        "cctld_country": cctld_country,
    }


def evaluate_human_researcher_decision(
    row: dict[str, Any],
    normalized: NormalizedDomain,
    domain: DomainRecord | None,
    evidence: list[Evidence] | None = None,
    *,
    network_healthy: bool = True
) -> dict[str, Any]:
    """Execute the human-like researcher verification workflow."""
    org_name = str(row.get("Organization Name") or "").strip()
    country = str(row.get("Country") or row.get("\ufeffCountry") or "").strip().upper()
    territory_name = str(row.get("Sales Territory Name") or "").strip()
    input_domain = normalized.normalized_domain or normalized.original_domain

    # Pre-checks: Invalid input domain
    if normalized.error:
        return _make_result(
            row, normalized, domain,
            status="UNVERIFIED", level="UNVERIFIED", confidence="LOW", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="UNKNOWN",
            reason=f"Invalid input domain: {normalized.error}"
        )

    # Missing domain record
    if domain is None:
        return _make_result(
            row, normalized, domain,
            status="UNVERIFIED", level="UNVERIFIED", confidence="LOW", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="UNKNOWN",
            reason="Domain has not been fetched or independently verified."
        )

    # Operational status: Inactive / Parked
    if domain.parked or domain.http_status == 410:
        return _make_result(
            row, normalized, domain,
            status="INACTIVE", level="INACTIVE", confidence="HIGH", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="PARKED_OR_INACTIVE",
            reason="Domain website is confirmed parked, expired, or returned HTTP 410."
        )
    if domain.dns_status == "FAILED" and domain.http_status == 0 and network_healthy:
        return _make_result(
            row, normalized, domain,
            status="INACTIVE", level="INACTIVE", confidence="HIGH", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="PARKED_OR_INACTIVE",
            reason="DNS host resolution failed while network health was confirmed."
        )

    # Operational status: Blocked
    if domain.fetch_error_type in ("ROBOTS", "UNSAFE_ADDRESS"):
        return _make_result(
            row, normalized, domain,
            status="BLOCKED", level="BLOCKED", confidence="LOW", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="BLOCKED_PORTAL",
            reason=f"Access restricted: {domain.blocked_reason or domain.fetch_error_type}."
        )
    if domain.http_status in (403, 429) or (domain.http_status == 0 and not network_healthy):
        return _make_result(
            row, normalized, domain,
            status="BLOCKED", level="BLOCKED", confidence="LOW", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="BLOCKED_PORTAL",
            reason="Website access remained blocked by anti-bot/WAF security controls."
        )
    if domain.http_status == 0:
        return _make_result(
            row, normalized, domain,
            status="UNVERIFIED", level="UNVERIFIED", confidence="LOW", score=0,
            rel_type="UNKNOWN", strength="NONE", rep_type="UNKNOWN",
            reason="Website could not be reached after enabled fetch tiers."
        )

    # Check for Cross-Domain Redirect
    is_redirect = bool(
        domain.final_registered_domain and
        normalized.registered_domain and
        domain.final_registered_domain.lower() != normalized.registered_domain.lower()
    )

    # --- HUMAN RESEARCHER EVALUATION SIGNALS ---
    org_prof = clean_org_profile(org_name, territory_name)
    dom_prof = clean_domain_profile(domain.final_registered_domain or input_domain)

    pages = domain.pages or []
    homepage = pages[0] if pages else {}
    title = str(homepage.get("title", "") or "").strip()
    h1_text = " ".join(homepage.get("h1", []) or [])
    description = str(homepage.get("description", "") or "").strip()
    footer = str(homepage.get("footer", "") or "").strip()
    visible_text = str(homepage.get("visible_text", "") or "")[:15000]
    all_text = f"{title} {h1_text} {description} {footer} {visible_text}".lower()

    # Collect structured data
    jsonld_legal_names = []
    jsonld_names = []
    for p in pages:
        for s in p.get("jsonld", []):
            if isinstance(s.get("legalName"), str):
                jsonld_legal_names.append(s["legalName"].strip())
            if isinstance(s.get("name"), str):
                jsonld_names.append(s["name"].strip())

    copyright_text = str(homepage.get("copyright") or "")
    emails = [e.lower() for p in pages for e in p.get("emails", [])]
    detected_countries = [c.upper() for p in pages for c in p.get("countries", [])]

    # Signal Scoring Model
    evidence_items: list[dict[str, Any]] = []
    score = 0.0
    group_match = False
    signals_detected: list[str] = []
    independent_sources: set[str] = set()

    # Process external evidence if provided
    if evidence:
        for ev in evidence:
            if ev.relationship == "unrelated" and ev.strength in ("STRONG", "VERY_STRONG"):
                score -= 50
                signals_detected.append("CONTRADICTORY_EXTERNAL_EVIDENCE")
                evidence_items.append({"type": ev.evidence_type, "source": "external", "text": ev.text, "score": -50})
            elif ev.relationship == "exact_entity" and ev.strength in ("STRONG", "VERY_STRONG"):
                score += 40
                signals_detected.append("EXACT_LEGAL_ENTITY")
                independent_sources.add("official_legal")
                evidence_items.append({"type": ev.evidence_type, "source": "external", "text": ev.text, "score": 40})
            elif ev.relationship == "group_entity" and ev.strength in ("STRONG", "VERY_STRONG"):
                group_match = True
                score += 30
                signals_detected.append("CORPORATE_GROUP_RELATIONSHIP")
                independent_sources.add("corporate_structure")
                evidence_items.append({"type": ev.evidence_type, "source": "external", "text": ev.text, "score": 30})

    # 1. Exact Legal Entity Match (+40)
    norm_org_clean = org_prof["clean_name"].lower()
    exact_legal_found = "EXACT_LEGAL_ENTITY" in signals_detected
    for legal in jsonld_legal_names:
        if legal.lower() == norm_org_clean or org_name.lower() in legal.lower():
            exact_legal_found = True
            evidence_items.append({
                "type": "exact_legal_jsonld", "source": "official_legal",
                "text": legal, "score": 40
            })
            break
    if not exact_legal_found:
        if norm_org_clean in footer.lower() or norm_org_clean in copyright_text.lower():
            exact_legal_found = True
            evidence_items.append({
                "type": "exact_legal_footer", "source": "official_legal",
                "text": org_prof["clean_name"], "score": 40
            })

    if exact_legal_found:
        score += 40
        signals_detected.append("EXACT_LEGAL_ENTITY")
        independent_sources.add("official_legal")

    # 2. Core Entity Name Match (+25)
    core_name = org_prof["core_name"].lower()
    core_in_title = bool(core_name and core_name in title.lower())
    core_in_h1_or_desc = bool(core_name and (core_name in h1_text.lower() or core_name in description.lower()))
    core_in_body = bool(core_name and (core_name in visible_text.lower() or any(core_name in n.lower() for n in jsonld_names)))

    if core_in_title or core_in_h1_or_desc:
        score += 25
        signals_detected.append("CORE_NAME_MATCH")
        independent_sources.add("official_identity")
        evidence_items.append({
            "type": "core_name_in_identity", "source": "official_identity",
            "text": title or h1_text or description, "score": 25
        })
    elif core_in_body:
        score += 10
        signals_detected.append("CORE_NAME_IN_BODY")
        independent_sources.add("official_identity")
        evidence_items.append({
            "type": "core_name_in_body", "source": "official_identity",
            "text": core_name, "score": 10
        })

    # 3. Domain-to-Brand / Company Name Direct Correspondence (+20)
    dom_label = dom_prof["domain_label"].lower()
    org_tokens = org_prof["tokens"]
    domain_matches_brand = False

    # Direct match or concatenation match
    combined_tokens = "".join(org_tokens)
    has_site_identity = bool(core_in_title or core_in_h1_or_desc or exact_legal_found or group_match)
    if has_site_identity and dom_label and len(dom_label) >= 3:
        if dom_label in org_tokens or dom_label in combined_tokens or combined_tokens.startswith(dom_label):
            domain_matches_brand = True
        elif any(len(t) >= 4 and (t in dom_label or dom_label in t) for t in org_tokens):
            domain_matches_brand = True

    if domain_matches_brand:
        score += 20
        signals_detected.append("DOMAIN_BRAND_CORRESPONDENCE")
        independent_sources.add("domain_brand")
        evidence_items.append({
            "type": "domain_brand_match", "source": "domain_brand",
            "text": f"Domain '{dom_label}' corresponds directly to organization tokens: {org_tokens}", "score": 20
        })

    # 4. Country Consistency (+15)
    country_matches = False
    if country:
        if dom_prof["cctld_country"] == country:
            country_matches = True
            signals_detected.append("CCTLD_COUNTRY_MATCH")
        elif country in detected_countries:
            country_matches = True
            signals_detected.append("PAGE_COUNTRY_MATCH")
        else:
            aliases = COUNTRY_ALIASES.get(country, set())
            if any(alias in all_text for alias in aliases):
                country_matches = True
                signals_detected.append("TEXT_COUNTRY_MATCH")

    if country_matches:
        score += 15
        independent_sources.add("country_consistency")
        evidence_items.append({
            "type": "country_match", "source": "country_consistency",
            "text": f"Domain ccTLD or page context matches country '{country}'", "score": 15
        })

    # 5. Official Contact Info & Site Control (+15)
    contact_match = False
    if emails:
        for em in emails:
            em_host = em.rsplit("@", 1)[-1].lower()
            if em_host == dom_prof["registered_domain"] or em_host.endswith("." + dom_prof["registered_domain"]):
                contact_match = True
                evidence_items.append({
                    "type": "email_domain_match", "source": "contact_info",
                    "text": em, "score": 15
                })
                break
    if contact_match:
        score += 15
        signals_detected.append("OFFICIAL_CONTACT_INFO")
        independent_sources.add("contact_info")

    # 6. Corporate Group / Parent-Subsidiary Relationship (+30)
    already_group = group_match
    parent_tokens = org_prof["parent_tokens"]
    parent_core = org_prof["parent_core"].lower()
    if not group_match and parent_tokens:
        # Check if domain matches parent
        if any(len(t) >= 3 and (t in dom_label or dom_label in t) for t in parent_tokens):
            group_match = True
        elif parent_core and (parent_core in title.lower() or parent_core in all_text):
            group_match = True

    # Also check if organization name contains global brand and domain matches global brand
    if not group_match and dom_label and len(dom_label) >= 4:
        if dom_label in org_name.lower():
            # Check if domain is major corporate site or mentions the brand
            if dom_label in title.lower() or dom_label in description.lower():
                group_match = True

    if group_match and not already_group:
        score += 30
        signals_detected.append("CORPORATE_GROUP_RELATIONSHIP")
        independent_sources.add("corporate_structure")
        evidence_items.append({
            "type": "corporate_group_match", "source": "corporate_structure",
            "text": f"Corporate group connection established via territory/parent or group brand: {parent_core or dom_label}", "score": 30
        })

    # 7. Check for Contradictory Evidence (-50)
    # If the domain is a government domain (.gov / .gov.xx) but organization is not a government entity
    if ".gov" in dom_prof["registered_domain"] and not any(k in org_name.lower() for k in ("department", "ministry", "government", "court", "board", "authority", "commission", "state")):
        score -= 50
        signals_detected.append("CONTRADICTORY_GOVERNMENT_DOMAIN")
        evidence_items.append({
            "type": "contradictory_entity", "source": "negative",
            "text": f"Government domain {dom_prof['registered_domain']} does not match commercial entity {org_name}", "score": -50
        })

    # Determine final representation type
    rep_type = "OFFICIAL_COMPANY_WEBSITE"
    if group_match:
        rep_type = "GLOBAL_GROUP_PORTAL" if ".com" in dom_prof["registered_domain"] else "REGIONAL_PORTAL"
    elif dom_prof["cctld_country"]:
        rep_type = "REGIONAL_PORTAL"

    # --- CLASSIFICATION & CONFIDENCE LADDER ---
    independent_count = len(independent_sources)
    external_urls = [ev.url for ev in (evidence or []) if getattr(ev, "url", None)]
    primary_url = external_urls[0] if (score < 0 and external_urls) else (homepage.get("url") or domain.final_url or (external_urls[0] if external_urls else f"https://{input_domain}/"))
    supporting_urls = [p.get("url") for p in pages if p.get("url") and p.get("url") != primary_url]

    # Negative / Contradictory -> MISMATCH
    if score < 0:
        return _make_result(
            row, normalized, domain,
            status="MISMATCH", level="MISMATCH", confidence="HIGH", score=score,
            rel_type="UNRELATED_ENTITY", strength="STRONG", rep_type="UNRELATED_SITE",
            reason=f"Authoritative evidence indicates domain {dom_prof['registered_domain']} is operated by an unrelated entity.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 1. VERIFIED_EXACT (Highest Confidence)
    if exact_legal_found and score >= 40:
        return _make_result(
            row, normalized, domain,
            status="VERIFIED_EXACT", level="EXACT", confidence="HIGH", score=score,
            rel_type="EXACT_LEGAL_ENTITY", strength="VERY_STRONG", rep_type=rep_type,
            reason="Official legal entity name explicitly identified in website structured data or corporate notice.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 2. VERIFIED_GROUP (Corporate Group / Subsidiary Match)
    if group_match and score >= 30:
        return _make_result(
            row, normalized, domain,
            status="VERIFIED_GROUP", level="GROUP", confidence="HIGH", score=score,
            rel_type="SUBSIDIARY" if "subsidiary" in signals_detected else "CORPORATE_GROUP", strength="STRONG", rep_type=rep_type,
            reason="Corporate evidence explicitly establishes that the organization operates under or belongs to the website's corporate group.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 2. VERIFIED_ENTITY (High Confidence First-Party Identity Match)
    if (core_in_title or core_in_h1_or_desc) and domain_matches_brand and score >= 35:
        return _make_result(
            row, normalized, domain,
            status="VERIFIED_ENTITY", level="ENTITY", confidence="HIGH", score=score,
            rel_type="SAME_ENTITY", strength="STRONG", rep_type=rep_type,
            reason="Official company website clearly represents the organization with matching identity, branding, and domain.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 3. VERIFIED_GROUP (Corporate Group / Subsidiary Match)
    if group_match and score >= 30:
        return _make_result(
            row, normalized, domain,
            status="VERIFIED_GROUP", level="GROUP", confidence="HIGH", score=score,
            rel_type="SUBSIDIARY" if "subsidiary" in signals_detected else "CORPORATE_GROUP", strength="STRONG", rep_type=rep_type,
            reason="Corporate evidence explicitly establishes that the organization operates under or belongs to the website's corporate group.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 4. STRONG_MATCH (Medium/High Confidence with Multi-Signal Agreement)
    if (has_site_identity or domain_matches_brand) and score >= 25 and independent_count >= 2:
        return _make_result(
            row, normalized, domain,
            status="STRONG_MATCH", level="STRONG", confidence="MEDIUM", score=score,
            rel_type="SAME_ENTITY", strength="MEDIUM", rep_type=rep_type,
            reason="Multiple consistent signals (domain, brand, country, and website identity) strongly indicate official representation.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 5. PROBABLE (Moderate / Meaningful Evidence with Minor Gaps)
    if (has_site_identity or contact_match) and score >= 15:
        return _make_result(
            row, normalized, domain,
            status="PROBABLE", level="PROBABLE", confidence="MEDIUM", score=score,
            rel_type="SAME_ENTITY", strength="MEDIUM", rep_type=rep_type,
            reason="Meaningful brand or entity signals found, but full formal corporate details are missing.",
            signals=signals_detected, ind_count=independent_count,
            primary_url=primary_url, supporting_urls=supporting_urls,
            evidence=evidence_items, is_redirect=is_redirect
        )

    # 6. UNVERIFIED (Insufficient Evidence)
    return _make_result(
        row, normalized, domain,
        status="UNVERIFIED", level="UNVERIFIED", confidence="LOW", score=score,
        rel_type="UNKNOWN", strength="WEAK" if score > 0 else "NONE", rep_type="UNKNOWN",
        reason="Inspected website evidence is insufficient to verify an official relationship with the supplied organization.",
        signals=signals_detected, ind_count=independent_count,
        primary_url=primary_url, supporting_urls=supporting_urls,
        evidence=evidence_items, is_redirect=is_redirect
    )


def _make_result(
    row: dict[str, Any],
    normalized: NormalizedDomain,
    domain: DomainRecord | None,
    *,
    status: str,
    level: str,
    confidence: str,
    score: float,
    rel_type: str,
    strength: str,
    rep_type: str,
    reason: str,
    signals: list[str] | None = None,
    ind_count: int = 0,
    primary_url: str = "",
    supporting_urls: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    is_redirect: bool = False
) -> dict[str, Any]:
    dest_status = status
    final_status = status
    final_reason = reason
    if is_redirect:
        final_status = "REDIRECT"
        level = "REDIRECT"
        final_reason = f"Input domain redirects across registered domains to {domain.final_url}; destination assessment: {dest_status}. {reason}"

    pages = domain.pages if domain else []
    first_page = pages[0] if pages else {}

    signals = signals or []
    supporting_urls = supporting_urls or []
    evidence = evidence or []

    # Format evidence models
    evidence_models = [
        Evidence(
            url=primary_url,
            evidence_type=ev.get("type", "human_signal"),
            text=str(ev.get("text", "")),
            relationship=rel_type.lower(),
            strength=strength,
            title=first_page.get("title", ""),
            source_type=ev.get("source", "website")
        )
        for ev in evidence
    ]

    out = {
        **row,
        "original_domain": normalized.original_domain,
        "normalized_domain": normalized.normalized_domain,
        "registered_domain": normalized.registered_domain,
        "requested_url": normalized.requested_url,
        "final_url": domain.final_url if domain else "",
        "final_registered_domain": domain.final_registered_domain if domain else "",
        "http_status": domain.http_status if domain else 0,
        "dns_status": domain.dns_status if domain else "UNKNOWN",
        "https_available": domain.https_available if domain else None,
        "redirect_chain": domain.redirect_chain if domain else [],
        "domain_active": False if final_status == "INACTIVE" else domain.domain_active if domain else None,
        "website_title": first_page.get("title", ""),
        "website_h1": first_page.get("h1", []),
        "website_description": first_page.get("description", ""),
        "detected_company_name": [first_page.get("title", "")] if first_page.get("title") else [],
        "detected_legal_entity": [e.text for e in evidence_models if "legal" in e.evidence_type],
        "detected_parent_company": row.get("Sales Territory Name") or "",
        "detected_brand": [dom.split(".")[0] for dom in [normalized.normalized_domain] if dom],
        "detected_country": first_page.get("countries", []),
        "exact_entity_match": "EXACT_LEGAL_ENTITY" in signals,
        "corporate_group_match": "CORPORATE_GROUP_RELATIONSHIP" in signals,
        "evidence_url_1": primary_url,
        "evidence_url_2": supporting_urls[0] if supporting_urls else "",
        "evidence_type": [e.evidence_type for e in evidence_models],
        "evidence_text": [e.text for e in evidence_models[:5]],
        "evidence_json": [e.to_dict() for e in evidence_models],
        "evidence_reason": final_reason,
        "verification_status": final_status,
        "destination_verification_status": dest_status if is_redirect else "",
        "verification_reason": final_reason,
        "confidence": confidence,
        "fetch_method": domain.fetch_method if domain else "NONE",
        "fetch_attempts": domain.fetch_attempts if domain else 0,
        "blocked_reason": domain.blocked_reason if domain else "",
        "checked_at": domain.checked_at if domain else "",
        # New V2 Human-Researcher Architecture Fields:
        "verification_level": level,
        "confidence_score": round(score, 1),
        "entity_relationship_type": rel_type,
        "evidence_strength": strength,
        "evidence_count": len(evidence_models),
        "independent_evidence_count": ind_count,
        "primary_evidence_url": primary_url,
        "supporting_evidence_urls": supporting_urls,
        "negative_evidence": [e.text for e in evidence_models if "contradictory" in e.evidence_type],
        "parent_company": str(row.get("Sales Territory Name") or ""),
        "corporate_group": str(row.get("Sales Territory Name") or ""),
        "domain_representation_type": rep_type,
    }
    return out
