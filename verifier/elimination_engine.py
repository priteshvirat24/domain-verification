"""Elimination-First Domain & Organization Verification Engine.

Architectural Principle:
The dataset contains candidate organization -> website mappings.
Assume each supplied URL is potentially correct unless concrete evidence proves
it is incorrect, unrelated, inactive, or represents a different organization.

Absence of evidence is UNKNOWN, never NEGATIVE.
Concrete contradictory evidence is MISMATCH.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from .models import DomainRecord
from .normalization import NormalizedDomain, registered_domain

# Standard global and regional legal suffixes
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
    r"co\.?|company"
    r")\b",
    re.IGNORECASE,
)

# Trailing country / territory tags like ' - SG', ' - JP', ' (M) BERHAD', ' (EXPORT)'
TRAILING_TERRITORY_RE = re.compile(
    r"(?:\s*-\s*[A-Z]{2}|\s*\([A-Z]{2,}\)|\s*\([A-Z\s]+\)\s*-\s*[A-Z]{2}|\s*\([A-Z\s]+\))$",
    re.IGNORECASE,
)

# Generic business stopwords
GENERIC_STOPWORDS = {
    "and", "the", "of", "for", "in", "at", "by", "to", "co", "company",
    "services", "service", "international", "global", "national", "asia",
    "pacific", "holdings", "holding", "group", "enterprises", "enterprise",
    "industries", "industry", "technology", "technologies", "solutions",
    "management", "investments", "investment", "trading", "products", "systems",
    "corp", "corporation", "ltd", "limited", "inc", "incorporated", "pte", "sdn", "bhd",
}

# Country code to country name / aliases mapping
COUNTRY_ALIASES: dict[str, set[str]] = {
    "SG": {"singapore", "sg"},
    "MY": {"malaysia", "malaysian", "my"},
    "PH": {"philippines", "philippine", "filipino", "ph"},
    "ID": {"indonesia", "indonesian", "id"},
    "TH": {"thailand", "thai", "th"},
    "VN": {"vietnam", "vietnamese", "vn"},
    "JP": {"japan", "japanese", "jp"},
    "KR": {"korea", "korean", "south korea", "kr"},
    "AU": {"australia", "australian", "au"},
    "NZ": {"new zealand", "nz"},
    "IN": {"india", "indian", "in"},
    "CN": {"china", "chinese", "cn"},
    "HK": {"hong kong", "hk"},
    "TW": {"taiwan", "taiwanese", "tw"},
    "US": {"united states", "usa", "us", "america", "american"},
    "GB": {"united kingdom", "uk", "great britain", "britain", "british"},
    "CA": {"canada", "canadian", "ca"},
    "PK": {"pakistan", "pakistani", "pk"},
    "BD": {"bangladesh", "bangladeshi", "bd"},
}

# ccTLD to country mapping
CCTLD_MAP: dict[str, str] = {
    "sg": "SG", "my": "MY", "ph": "PH", "id": "ID", "th": "TH", "vn": "VN",
    "jp": "JP", "kr": "KR", "au": "AU", "nz": "NZ", "in": "IN", "cn": "CN",
    "hk": "HK", "tw": "TW", "us": "US", "uk": "GB", "ca": "CA", "pk": "PK",
    "bd": "BD",
}

GOV_TERMS = {
    "department", "ministry", "government", "court", "board", "authority",
    "commission", "state of", "city of", "municipality", "agency", "council",
    "parliament", "police", "customs", "tax office", "bureau"
}


def clean_organization_name(raw_name: str) -> dict[str, Any]:
    """Deconstruct an organization name into normalized and distinctive components."""
    if not raw_name:
        return {"raw": "", "clean_name": "", "core_name": "", "tokens": [], "parent_hint": ""}
    
    s = raw_name.strip()
    # Strip territory tag at end
    s_noterr = TRAILING_TERRITORY_RE.sub("", s).strip()
    # Strip legal suffix
    s_clean = LEGAL_SUFFIXES_RE.sub("", s_noterr).strip()
    s_clean = re.sub(r"[\s,.-]+$", "", s_clean).strip()
    
    # Extract distinctive brand tokens
    raw_tokens = [re.sub(r"[^\w]", "", t).lower() for t in s_clean.split()]
    tokens = [t for t in raw_tokens if t and len(t) >= 2 and t not in GENERIC_STOPWORDS]
    
    # Core name
    core_name = s_clean if s_clean else s_noterr
    
    return {
        "raw": raw_name,
        "clean_name": s_clean,
        "core_name": core_name,
        "tokens": tokens,
    }


def extract_website_observable_facts(domain: DomainRecord | None) -> dict[str, Any]:
    """Extract concrete, observable structured facts from fetched website pages."""
    facts = {
        "website_title": "",
        "homepage_h1": [],
        "organization_names_found": [],
        "legal_names_found": [],
        "brand_names_found": [],
        "identified_operator": "",
        "identified_parent": "",
        "identified_group": "",
        "website_country": "",
        "website_addresses": [],
        "website_phone_numbers": [],
        "website_email_domains": [],
        "business_description": "",
        "registration_numbers": [],
        "canonical_domain": "",
        "redirect_destination": "",
    }
    
    if not domain or not domain.pages:
        if domain:
            facts["canonical_domain"] = domain.final_registered_domain or domain.registered_domain
            if domain.final_url and domain.final_registered_domain != domain.registered_domain:
                facts["redirect_destination"] = domain.final_url
        return facts
    
    home = domain.pages[0]
    facts["website_title"] = home.get("title", "")
    facts["homepage_h1"] = home.get("h1", [])
    facts["business_description"] = home.get("description", "")
    facts["canonical_domain"] = domain.final_registered_domain or domain.registered_domain
    if domain.final_url and domain.final_registered_domain != domain.registered_domain:
        facts["redirect_destination"] = domain.final_url
        
    all_pages = domain.pages
    emails = []
    phones = []
    addresses = []
    registrations = []
    jsonld_org_names = []
    jsonld_legal_names = []
    jsonld_parents = []
    jsonld_brands = []
    
    for p in all_pages:
        emails.extend(p.get("emails", []))
        phones.extend(p.get("phones", []))
        addresses.extend(p.get("addresses", []))
        registrations.extend(p.get("registrations", []))
        
        for schema in p.get("jsonld", []):
            if schema.get("name"):
                n = str(schema["name"]).strip()
                if n and n not in jsonld_org_names:
                    jsonld_org_names.append(n)
            if schema.get("legalName"):
                ln = str(schema["legalName"]).strip()
                if ln and ln not in jsonld_legal_names:
                    jsonld_legal_names.append(ln)
            if schema.get("parentOrganization"):
                parent = schema["parentOrganization"]
                p_name = parent.get("name") if isinstance(parent, dict) else str(parent)
                if p_name and p_name not in jsonld_parents:
                    jsonld_parents.append(str(p_name).strip())
            if schema.get("brand"):
                b = schema["brand"]
                b_name = b.get("name") if isinstance(b, dict) else str(b)
                if b_name and b_name not in jsonld_brands:
                    jsonld_brands.append(str(b_name).strip())
                    
    facts["organization_names_found"] = jsonld_org_names[:10]
    facts["legal_names_found"] = jsonld_legal_names[:10]
    facts["brand_names_found"] = jsonld_brands[:10]
    facts["identified_parent"] = jsonld_parents[0] if jsonld_parents else ""
    facts["website_addresses"] = addresses[:5]
    facts["website_phone_numbers"] = list(dict.fromkeys(phones))[:5]
    facts["registration_numbers"] = list(dict.fromkeys(registrations))[:5]
    
    # Extract unique email domains
    email_domains = []
    for em in emails:
        if "@" in em:
            em_dom = em.split("@")[-1].lower().strip()
            if em_dom and em_dom not in email_domains:
                email_domains.append(em_dom)
    facts["website_email_domains"] = email_domains[:5]
    
    # Infer identified operator
    if jsonld_legal_names:
        facts["identified_operator"] = jsonld_legal_names[0]
    elif jsonld_org_names:
        facts["identified_operator"] = jsonld_org_names[0]
    elif facts["website_title"]:
        # Extract title prefix before | or - or :
        parts = re.split(r"[\s|–—:-]+", facts["website_title"])
        if parts and len(parts[0]) >= 2:
            facts["identified_operator"] = parts[0]
            
    # Infer website country
    host = domain.final_registered_domain or domain.registered_domain
    tld = host.split(".")[-1].lower() if host else ""
    if tld in CCTLD_MAP:
        facts["website_country"] = CCTLD_MAP[tld]
    elif ".com." in host or ".co." in host or ".org." in host or ".edu." in host or ".gov." in host:
        sec_tld = host.split(".")[-1].lower()
        if sec_tld in CCTLD_MAP:
            facts["website_country"] = CCTLD_MAP[sec_tld]
    else:
        # Check addresses in jsonld
        for addr in addresses:
            if isinstance(addr, dict) and addr.get("addressCountry"):
                c = str(addr["addressCountry"]).strip().upper()
                if len(c) == 2 and c in COUNTRY_ALIASES:
                    facts["website_country"] = c
                    break
                    
    return facts


def evaluate_elimination_decision(
    row: dict[str, Any],
    normalized: NormalizedDomain,
    domain: DomainRecord | None,
    *,
    network_healthy: bool = True
) -> dict[str, Any]:
    """Execute the Elimination Decision Tree for candidate organization-domain mapping."""
    org_raw = str(row.get("Organization Name") or "").strip()
    input_country = str(row.get("Country") or "").strip().upper()
    sales_territory = str(row.get("Sales Territory Name") or "").strip()
    
    org_prof = clean_organization_name(org_raw)
    clean_org = org_prof["clean_name"]
    core_org = org_prof["core_name"]
    org_tokens = org_prof["tokens"]
    terr_prof = clean_organization_name(sales_territory)
    terr_tokens = terr_prof["tokens"]

    # Extract website facts
    facts = extract_website_observable_facts(domain)
    title = facts["website_title"]
    h1_text = " ".join(facts["homepage_h1"])
    desc = facts["business_description"]
    all_visible = " ".join(p.get("visible_text", "") for p in (domain.pages if domain else []))[:60_000]
    
    # Specific Positive Checks State
    check_a_search = "UNCLEAR"
    check_b_org_name = "NO"
    check_c_brand = "NONE"
    check_d_country = "NEUTRAL"
    check_e_address = "NEUTRAL"
    check_f_phone = "NEUTRAL"
    check_g_email = "NEUTRAL"
    check_h_business = "NEUTRAL"
    check_i_corporate = "NONE"
    
    contradiction_found = False
    contradiction_type = "NONE"
    contradiction_detail = ""
    
    # Target domain label
    dom_str = normalized.normalized_domain or normalized.original_domain
    dom_registered = normalized.registered_domain or registered_domain(dom_str)
    dom_label = dom_registered.split(".")[0].lower() if dom_registered else ""
    
    # ----------------------------------------------------
    # Check B: Organization Name Match (Observable Fact)
    # ----------------------------------------------------
    clean_lower = clean_org.lower()
    core_lower = core_org.lower()
    if clean_lower and (clean_lower in title.lower() or clean_lower in h1_text.lower() or clean_lower in desc.lower()):
        check_b_org_name = "YES"
    elif core_lower and (core_lower in title.lower() or core_lower in h1_text.lower() or core_lower in desc.lower()):
        check_b_org_name = "YES"
    elif any(clean_lower == str(name).lower() for name in facts["organization_names_found"] + facts["legal_names_found"]):
        check_b_org_name = "YES"
    elif org_tokens and all(t in title.lower() or t in h1_text.lower() or t in all_visible.lower() for t in org_tokens):
        check_b_org_name = "PARTIAL"
    elif org_tokens and any(t in title.lower() or t in h1_text.lower() for t in org_tokens if len(t) >= 4):
        check_b_org_name = "PARTIAL"
        
    # ----------------------------------------------------
    # Check C: Brand Relationship (Observable Fact)
    # ----------------------------------------------------
    core_compact = re.sub(r"[^a-z0-9]", "", core_lower)
    clean_compact = re.sub(r"[^a-z0-9]", "", clean_lower)
    if dom_label and len(dom_label) >= 3:
        if dom_label in org_tokens or "".join(org_tokens).startswith(dom_label) or dom_label in clean_compact or (core_compact and core_compact in dom_label and len(core_compact) >= 5):
            check_c_brand = "YES"
        elif len(org_tokens) == 1 and len(org_tokens[0]) >= 5 and org_tokens[0] == dom_label:
            check_c_brand = "YES"

    # ----------------------------------------------------
    # Check D: Country Consistency (Observable Fact)
    # ----------------------------------------------------
    site_country = facts["website_country"]
    if site_country:
        if site_country == input_country:
            check_d_country = "CONSISTENT"
        elif site_country in ("US", "GB") and dom_registered.endswith((".com", ".org", ".net")):
            check_d_country = "NEUTRAL"
        else:
            # Another specific ccTLD
            if check_i_corporate in ("GROUP", "PARENT", "SUBSIDIARY") or check_c_brand == "YES":
                check_d_country = "NEUTRAL"
            else:
                check_d_country = "CONTRADICTORY"
    else:
        # Check text country mentions
        aliases = COUNTRY_ALIASES.get(input_country, set())
        if any(alias in all_visible.lower() for alias in aliases):
            check_d_country = "CONSISTENT"
        else:
            check_d_country = "NEUTRAL"
            
    # ----------------------------------------------------
    # Check G: Email Domain Consistency
    # ----------------------------------------------------
    if facts["website_email_domains"]:
        if any(ed == dom_registered or ed.endswith("." + dom_registered) for ed in facts["website_email_domains"]):
            check_g_email = "CONSISTENT"
            
    # ----------------------------------------------------
    # Check I: Corporate Relationship
    # ----------------------------------------------------
    if check_b_org_name == "YES":
        check_i_corporate = "DIRECT"
    elif check_i_corporate != "GROUP":
        if facts["identified_parent"] and any(t in facts["identified_parent"].lower() for t in org_tokens if len(t) >= 4):
            check_i_corporate = "SUBSIDIARY"
            
    # Check business activity
    if desc:
        facts["business_description"] = desc[:300]
        check_h_business = "CONSISTENT"
        
    # ----------------------------------------------------
    # ELIMINATION DECISION TREE
    # ----------------------------------------------------
    
    # STEP 1: Is domain genuinely dead?
    if normalized.error:
        return _build_result(
            row, facts,
            classification="INACTIVE", confidence="HIGH",
            reason=f"Invalid domain syntax or unparseable input: {normalized.error}",
            contradiction_found=True, contradiction_type="PARKED_DOMAIN",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=normalized.requested_url or "",
            evidence_text="Invalid domain name format."
        )
        
    if domain is None:
        return _build_result(
            row, facts,
            classification="REVIEW", confidence="LOW",
            reason="Domain has not been fetched or evidence is unavailable.",
            contradiction_found=False, contradiction_type="NONE",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url="", evidence_text=""
        )
        
    if domain.dns_status == "FAILED" and domain.http_status == 0:
        return _build_result(
            row, facts,
            classification="INACTIVE", confidence="HIGH",
            reason="Domain is genuinely inactive: DNS resolution failed (host does not exist).",
            contradiction_found=True, contradiction_type="PARKED_DOMAIN",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.requested_url,
            evidence_text="DNS resolution failed: NXDOMAIN or server failure."
        )
        
    if domain.http_status == 410:
        return _build_result(
            row, facts,
            classification="INACTIVE", confidence="HIGH",
            reason="Domain server returned HTTP 410 Gone (website permanently removed).",
            contradiction_found=True, contradiction_type="PARKED_DOMAIN",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text="HTTP 410 Gone."
        )

    # STEP 2: Can the website be inspected?
    if domain.blocked_reason or domain.http_status in (403, 429) or domain.fetch_error_type in ("ROBOTS", "UNSAFE_ADDRESS"):
        return _build_result(
            row, facts,
            classification="BLOCKED", confidence="LOW",
            reason=f"Website could not be inspected due to anti-bot/WAF access restriction (HTTP {domain.http_status} / {domain.blocked_reason or domain.fetch_error_type}).",
            contradiction_found=False, contradiction_type="NONE",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Inspection blocked: {domain.blocked_reason or domain.http_status}"
        )

    # STEP 3: Is it a real website rather than parking/for-sale/error page?
    if domain.parked:
        return _build_result(
            row, facts,
            classification="INACTIVE", confidence="HIGH",
            reason="Domain is demonstrably parked, expired, or offered for sale.",
            contradiction_found=True, contradiction_type="PARKED_DOMAIN",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text="Page content contains parking, domain sale, or expired domain indicators."
        )

    # Cross-domain redirect detection
    is_cross_domain_redirect = bool(
        domain.final_registered_domain and
        dom_registered and
        domain.final_registered_domain != dom_registered
    )

    # STEP 4 & 5: Contradiction / Mismatch Detection (The Elimination Test)
    # Check Government Portal Mismatch (Negative Rule 5)
    is_gov_domain = ".gov" in dom_registered or dom_registered.endswith(".go.th") or dom_registered.endswith(".go.id")
    is_org_gov = any(g in org_raw.lower() for g in GOV_TERMS)
    if is_gov_domain and not is_org_gov:
        return _build_result(
            row, facts,
            classification="MISMATCH", confidence="HIGH",
            reason=f"Government website ({dom_registered}) clearly belongs to a public agency; supplied organization '{org_raw}' is an unrelated non-governmental entity.",
            contradiction_found=True, contradiction_type="GOVERNMENT_PORTAL",
            c_a="NO", c_b="NO", c_c="NO", c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h="CONTRADICTORY",
            c_i="NONE", evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Government portal operated by public administration: {title}"
        )

    # Check Concrete Unrelated Organization / Operator (Negative Rule 1, 2, 8)
    # If the website explicitly identifies an operator/owner that has 0 token overlap with the organization,
    # 0 brand connection, 0 group connection, and country is contradictory:
    identified_op = facts["identified_operator"]
    if identified_op and len(identified_op) >= 4:
        op_prof = clean_organization_name(identified_op)
        op_tokens = op_prof["tokens"]
        has_overlap = any(t in op_tokens for t in org_tokens) or any(t in terr_tokens for t in op_tokens)
        
        # If no overlap, no brand match, no group match, and site operator is a distinct named entity:
        if not has_overlap and check_c_brand != "YES" and check_i_corporate == "NONE":
            # If country is contradictory or domain is a generic portal for another entity
            if check_d_country == "CONTRADICTORY" or (op_prof["clean_name"] and op_prof["clean_name"].lower() not in org_raw.lower()):
                # Confirm there is no mention of org in body text
                if not any(t in all_visible.lower() for t in org_tokens if len(t) >= 4):
                    return _build_result(
                        row, facts,
                        classification="MISMATCH", confidence="HIGH",
                        reason=f"Website explicitly identifies an unrelated organization ({identified_op}) with no plausible brand, territory, or corporate group connection.",
                        contradiction_found=True, contradiction_type="UNRELATED_ENTITY",
                        c_a="NO", c_b="NO", c_c="NO", c_d=check_d_country,
                        c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h="CONTRADICTORY",
                        c_i="NONE", evidence_url=domain.final_url or domain.requested_url,
                        evidence_text=f"Website operator explicitly identified as '{identified_op}'."
                    )

    # Handle cross-domain redirect if no mismatch found
    if is_cross_domain_redirect:
        dest_desc = "valid destination" if (check_b_org_name == "YES" or check_c_brand == "YES" or check_i_corporate in ("GROUP", "DIRECT")) else "unconfirmed destination"
        return _build_result(
            row, facts,
            classification="REDIRECT", confidence="HIGH" if check_i_corporate in ("GROUP", "DIRECT") else "MEDIUM",
            reason=f"Domain redirects across registered domains to {domain.final_url}; destination assessment: {dest_desc}.",
            contradiction_found=False, contradiction_type="NONE",
            c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url,
            evidence_text=f"Redirect chain: {' -> '.join(domain.redirect_chain)}" if domain.redirect_chain else f"Redirected to {domain.final_url}"
        )

    # STEP 6: Legitimate Organization / Corporate Group Match
    if check_i_corporate in ("GROUP", "PARENT", "SUBSIDIARY"):
        group_name = facts["identified_group"] or terr_prof["clean_name"] or dom_label.upper()
        return _build_result(
            row, facts,
            classification="VALID_GROUP", confidence="HIGH",
            reason=f"Corporate group relationship confirmed: organization operates under the website's corporate group ({group_name}).",
            contradiction_found=False, contradiction_type="NONE",
            c_a="YES", c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h="CONSISTENT",
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Corporate group/parent '{group_name}' officially associated via territory and site identity."
        )

    if check_b_org_name == "YES":
        return _build_result(
            row, facts,
            classification="VALID", confidence="HIGH",
            reason=f"Website title, headers, or structured identity explicitly identify the organization ({core_org}).",
            contradiction_found=False, contradiction_type="NONE",
            c_a="YES", c_b="YES", c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h="CONSISTENT",
            c_i="DIRECT", evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Website identity matches '{core_org}' in title: '{title}'."
        )

    # STEP 7: Consistency Signals Support (Elimination Default)
    # The dataset already contains candidate organization -> website mappings.
    # If active, brand matches domain, contact/country is consistent, and no contradiction exists:
    if check_c_brand == "YES" and check_d_country != "CONTRADICTORY":
        return _build_result(
            row, facts,
            classification="VALID", confidence="MEDIUM",
            reason=f"Candidate mapping validated by consistent brand domain ('{dom_label}'), matching country context, and absence of contradictory evidence.",
            contradiction_found=False, contradiction_type="NONE",
            c_a="YES", c_b=check_b_org_name, c_c="YES", c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Domain '{dom_label}' corresponds directly to brand tokens of '{clean_org}'."
        )

    if check_b_org_name == "PARTIAL" and (check_g_email == "CONSISTENT" or check_d_country == "CONSISTENT"):
        return _build_result(
            row, facts,
            classification="VALID", confidence="MEDIUM",
            reason=f"Candidate mapping validated: core organization tokens found on website with consistent contact/country context.",
            contradiction_found=False, contradiction_type="NONE",
            c_a=check_a_search, c_b="PARTIAL", c_c=check_c_brand, c_d=check_d_country,
            c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
            c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
            evidence_text=f"Core brand tokens {org_tokens} observed in website content."
        )

    # If evidence is genuinely ambiguous:
    return _build_result(
        row, facts,
        classification="REVIEW", confidence="LOW",
        reason="Ambiguous evidence: website identity is not conclusively established, but no concrete contradiction was found to eliminate the candidate mapping.",
        contradiction_found=False, contradiction_type="NONE",
        c_a=check_a_search, c_b=check_b_org_name, c_c=check_c_brand, c_d=check_d_country,
        c_e=check_e_address, c_f=check_f_phone, c_g=check_g_email, c_h=check_h_business,
        c_i=check_i_corporate, evidence_url=domain.final_url or domain.requested_url,
        evidence_text="Inspected website lacks explicit brand, legal, or contact alignment with the supplied candidate."
    )


def _build_result(
    row: dict[str, Any],
    facts: dict[str, Any],
    *,
    classification: str,
    confidence: str,
    reason: str,
    contradiction_found: bool,
    contradiction_type: str,
    c_a: str, c_b: str, c_c: str, c_d: str,
    c_e: str, c_f: str, c_g: str, c_h: str,
    c_i: str,
    evidence_url: str,
    evidence_text: str
) -> dict[str, Any]:
    """Construct output dictionary preserving all original columns and appending required fields."""
    res = dict(row)
    
    # Appended fields required by Section 1 & Section 11
    res["classification"] = classification
    res["confidence"] = confidence
    res["website_title"] = facts.get("website_title", "")
    res["homepage_h1"] = facts.get("homepage_h1", [])
    res["website_identified_entity"] = facts.get("identified_operator", "")
    res["website_identified_brand"] = facts.get("brand_names_found", [])
    res["website_identified_group"] = facts.get("identified_group", "")
    res["website_country"] = facts.get("website_country", "")
    res["website_address"] = facts.get("website_addresses", [])
    res["website_phone"] = facts.get("website_phone_numbers", [])
    res["website_email_domain"] = facts.get("website_email_domains", [])
    res["business_activity"] = facts.get("business_description", "")
    res["organization_names_found"] = facts.get("organization_names_found", [])
    res["legal_names_found"] = facts.get("legal_names_found", [])
    res["registration_numbers"] = facts.get("registration_numbers", [])
    res["canonical_domain"] = facts.get("canonical_domain", "")
    res["redirect_destination"] = facts.get("redirect_destination", "")
    
    # Specific Positive Checks
    res["search_association"] = c_a
    res["organization_name_match"] = c_b
    res["brand_relationship"] = c_c
    res["country_consistency"] = c_d
    res["address_consistency"] = c_e
    res["phone_consistency"] = c_f
    res["email_consistency"] = c_g
    res["business_activity_consistency"] = c_h
    res["corporate_relationship"] = c_i
    
    # Negative / Contradiction Rules
    res["contradiction_found"] = contradiction_found
    res["contradiction_type"] = contradiction_type
    
    # Traceability & Explanations
    res["evidence_url"] = evidence_url
    res["evidence_text"] = evidence_text
    res["decision_reason"] = reason
    
    return res
