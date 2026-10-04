"""Proof Ladder Verification Engine.

Implements the Logical Framework Review principles (2026-10-01):
1. Proof Ladder:
   - STRONG: Full legal name or registration number on site; subsidiary named on group site; official company register listing the website. (Solo ACCEPT)
   - SUPPORTING: Entity or brand name in page title/heading; matching address; matching phone country code; matching country (whole word only). (2 required, at least 1 must be company name)
   - HINT ONLY: Domain looks like company name; contact email on site's own domain. (Never enough on its own)
2. Three Terminal Decisions:
   - ACCEPT: Direct positive proof linking company to domain. Rule confidence is
     not an empirical accuracy estimate until the audit set is labeled.
   - REJECT: Clear proof of mismatch/unrelated owner, parked/for-sale, non-existent domain, or dissolved entity.
   - NEEDS_REVIEW: Ambiguous, unreachable, unreadable, or potential group site lacking explicit subsidiary linkage.
3. Strict Whole-Word & Full Country Matching (never 2-letter codes inside words).
4. Full Redirect Assessment: evaluates final destination using the same proof ladder.
5. Inactive State Disaggregation: NO_DOMAIN_SUPPLIED, DOMAIN_DOES_NOT_EXIST, PARKED_OR_FOR_SALE, TEMPORARILY_UNREACHABLE.
6. Transparent Check Accounting: unperformed checks are recorded as NOT_CHECKED.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .models import DomainRecord, Evidence
from .entity_resolution import candidate_identity
from .normalization import NormalizedDomain, normalize_name, registered_domain

# ---------------------------------------------------------------------------
# Strict regexes & country mappings (WHOLE WORDS ONLY, NO 2-LETTER CODES)
# ---------------------------------------------------------------------------

# Full country names and dial codes. NO 2-letter codes like 'in', 'us', 'th', 'sg' in body text!
COUNTRY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "SG": {
        "name": "Singapore",
        "patterns": [r"\bsingapore\b", r"\bsingaporean\b"],
        "dial_codes": [r"\+65\b"],
        "cities": [r"\bsingapore\b", r"\bjurong\b", r"\bchangi\b", r"\btuas\b"],
    },
    "MY": {
        "name": "Malaysia",
        "patterns": [r"\bmalaysia\b", r"\bmalaysian\b"],
        "dial_codes": [r"\+60\b"],
        "cities": [r"\bkuala\s+lumpur\b", r"\bpenang\b", r"\bjohor\s+bahru\b", r"\bselangor\b", r"\bpetaling\s+jaya\b", r"\bshah\s+alam\b"],
    },
    "TH": {
        "name": "Thailand",
        "patterns": [r"\bthailand\b", r"\bthai\b"],
        "dial_codes": [r"\+66\b"],
        "cities": [r"\bbangkok\b", r"\bnonthaburi\b", r"\bchiang\s+mai\b", r"\bchonburi\b", r"\bsamut\s+prakan\b", r"\brayong\b"],
    },
    "ID": {
        "name": "Indonesia",
        "patterns": [r"\bindonesia\b", r"\bindonesian\b"],
        "dial_codes": [r"\+62\b"],
        "cities": [r"\bjakarta\b", r"\bsurabaya\b", r"\bbandung\b", r"\bbekasi\b", r"\bmedan\b", r"\btangerang\b", r"\bsemarang\b"],
    },
    "VN": {
        "name": "Vietnam",
        "patterns": [r"\bvietnam\b", r"\bviet\s+nam\b", r"\bvietnamese\b"],
        "dial_codes": [r"\+84\b"],
        "cities": [r"\bho\s+chi\s+minh\b", r"\bhanoi\b", r"\bda\s+nang\b", r"\bhai\s+phong\b", r"\bbinh\s+duong\b", r"\bdong\s+nai\b"],
    },
    "PH": {
        "name": "Philippines",
        "patterns": [r"\bphilippines\b", r"\bphilippine\b", r"\bfilipino\b"],
        "dial_codes": [r"\+63\b"],
        "cities": [r"\bmanila\b", r"\bquezon\s+city\b", r"\bmakati\b", r"\btaguig\b", r"\bcebu\b", r"\bpasig\b", r"\bdavao\b"],
    },
    "JP": {
        "name": "Japan",
        "patterns": [r"\bjapan\b", r"\bjapanese\b", r"\b日本\b"],
        "dial_codes": [r"\+81\b"],
        "cities": [r"\btokyo\b", r"\bosaka\b", r"\byokohama\b", r"\bnagoya\b", r"\bfukuoka\b", r"\bkobe\b", r"\bkyoto\b"],
    },
    "KR": {
        "name": "South Korea",
        "patterns": [r"\bsouth\s+korea\b", r"\brepublic\s+of\s+korea\b", r"\bkorea\b", r"\bkorean\b", r"\b대한민국\b", r"\b한국\b"],
        "dial_codes": [r"\+82\b"],
        "cities": [r"\bseoul\b", r"\bbusan\b", r"\bincheon\b", r"\bdaegu\b", r"\bsuwon\b"],
    },
    "IN": {
        "name": "India",
        "patterns": [r"\bindia\b", r"\bindian\b"],
        "dial_codes": [r"\+91\b"],
        "cities": [r"\bmumbai\b", r"\bdelhi\b", r"\bbangalore\b", r"\bbengaluru\b", r"\bhyderabad\b", r"\bchennai\b", r"\bkane\b", r"\bpune\b", r"\bgurgaon\b", r"\bnoida\b"],
    },
    "AU": {
        "name": "Australia",
        "patterns": [r"\baustralia\b", r"\baustralian\b"],
        "dial_codes": [r"\+61\b"],
        "cities": [r"\bsydney\b", r"\bmelbourne\b", r"\bbrisbane\b", r"\bperth\b", r"\badelaide\b"],
    },
    "US": {
        "name": "United States",
        "patterns": [r"\bunited\s+states\b", r"\bamerica\b", r"\bamerican\b", r"\bu\.s\.a\.\b", r"\bu\.s\.\b"],
        "dial_codes": [r"\+1\b"],
        "cities": [r"\bnew\s+york\b", r"\blos\s+angeles\b", r"\bchicago\b", r"\bhouston\b", r"\bsan\s+francisco\b"],
    },
    "CN": {
        "name": "China",
        "patterns": [r"\bchina\b", r"\bchinese\b", r"\b中国\b"],
        "dial_codes": [r"\+86\b"],
        "cities": [r"\bbeijing\b", r"\bshanghai\b", r"\bshenzhen\b", r"\bguangzhou\b"],
    },
    "NZ": {"name": "New Zealand", "patterns": [r"\bnew\s+zealand\b"],
           "dial_codes": [r"\+64\b"], "cities": [r"\bauckland\b", r"\bwellington\b", r"\bchristchurch\b"]},
    "GB": {"name": "United Kingdom", "patterns": [r"\bunited\s+kingdom\b", r"\bgreat\s+britain\b"],
           "dial_codes": [r"\+44\b"], "cities": [r"\blondon\b", r"\bmanchester\b", r"\bbirmingham\b"]},
    "CA": {"name": "Canada", "patterns": [r"\bcanada\b", r"\bcanadian\b"],
           "dial_codes": [], "cities": [r"\btoronto\b", r"\bvancouver\b", r"\bmontreal\b"]},
    "HK": {"name": "Hong Kong", "patterns": [r"\bhong\s+kong\b", r"\b香港\b"],
           "dial_codes": [r"\+852\b"], "cities": [r"\bkowloon\b", r"\bcentral\s+hong\s+kong\b"]},
    "TW": {"name": "Taiwan", "patterns": [r"\btaiwan\b", r"\b台灣\b", r"\b臺灣\b"],
           "dial_codes": [r"\+886\b"], "cities": [r"\btaipei\b", r"\bkaohsiung\b"]},
}

# Legal suffixes to distinguish exact legal forms
LEGAL_SUFFIX_FORMS = [
    (r"\b(?:pte\.?\s*ltd\.?|private\s+limited)\b", "PTE_LTD"),
    (r"\b(?:pty\.?\s*ltd\.?|proprietary\s+limited)\b", "PTY_LTD"),
    (r"\b(?:sdn\.?\s*bhd\.?|sendirian\s+berhad)\b", "SDN_BHD"),
    (r"\b(?:co\.?,?\s*ltd\.?|company\s+limited)\b", "CO_LTD"),
    (r"\b(?:bhd\.?|berhad)\b", "BHD"),
    (r"\b(?:ltd\.?|limited)\b", "LTD"),
    (r"\b(?:inc\.?|incorporated)\b", "INC"),
    (r"\b(?:corp\.?|corporation)\b", "CORP"),
    (r"\b(?:llc|l\.l\.c\.)\b", "LLC"),
    (r"\b(?:plc|p\.l\.c\.)\b", "PLC"),
    (r"\b(?:gmbh)\b", "GMBH"),
    (r"\b(?:k\.k\.|kabushiki\s+kaisha|株式会社)\b", "KK"),
    (r"\b(?:g\.k\.|godo\s+kaisha|合同会社)\b", "GK"),
]

# Patterns indicating parked / for-sale domains
PARKED_PATTERNS = [
    re.compile(r"\b(?:domain\s+for\s+sale|buy\s+this\s+domain|is\s+for\s+sale|is\s+parked|parked\s+free|sedo\s+parking|godaddy\s+parking|afternic|hugedomains|dan\.com|uniregistry)\b", re.I),
    re.compile(r"\b(?:this\s+domain\s+has\s+expired|domain\s+expired|renew\s+this\s+domain|domain\s+may\s+be\s+for\s+sale)\b", re.I),
]

# Patterns indicating company closure, bankruptcy, or liquidation
CLOSED_PATTERNS = [
    re.compile(r"\b(?:has\s+ceased\s+operations|company\s+is\s+closed|filed\s+for\s+bankruptcy|liquidation|went\s+out\s+of\s+business|no\s+longer\s+in\s+business|permanently\s+closed)\b", re.I),
]

# Patterns indicating corporate acquisition / merger
ACQUISITION_PATTERNS = [
    re.compile(r"\b(?:(?:was\s+)?acquired\s+by|now\s+part\s+of|merged\s+with|a\s+division\s+of\s+[\w\s&.,]+|joined\s+(?:the\s+)?[\w\s&.,]+\s+family)\b", re.I),
]

# Patterns for explicit subsidiary / group declaration
SUBSIDIARY_PATTERNS = [
    re.compile(r"\b(?:subsidiary\s+of|group\s+company\s+of|member\s+of\s+(?:the\s+)?[\w\s&.,]+|regional\s+(?:office|headquarters|branch)\s+of)\b", re.I),
]


def _clean_text_for_search(text: str) -> str:
    return " ".join((text or "").split())


def legal_identity_key(value: str) -> str:
    """Keep jurisdictional legal forms so Pty Ltd and Pte Ltd cannot collide."""
    text = re.sub(r"[^\w]+", " ", (value or "").casefold()).strip()
    tokens = text.split()
    aliases = {"limited": "ltd", "incorporated": "inc", "corporation": "corp"}
    return " ".join(aliases.get(token, token) for token in tokens)


def extract_core_company_name(organization: str) -> str:
    """Extract core company name without legal or territory suffixes, preserving whole words."""
    cleaned = re.sub(r"\s+", " ", organization.strip())
    # Remove trailing country tags like ' - SG', ' (M) BERHAD', ' (EXPORT)'
    cleaned = re.sub(r"(?:\s*-\s*[A-Z]{2}|\s*\([A-Z]{2,}\)|\s*\([A-Z\s]+\)\s*-\s*[A-Z]{2}|\s*\([A-Z\s]+\))$", "", cleaned, flags=re.I)
    # Remove legal suffixes
    for pat, _ in LEGAL_SUFFIX_FORMS:
        cleaned = re.sub(pat, "", cleaned, flags=re.I)
    # Clean whitespace and punctuation
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0e00-\u0e7f]", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def check_word_boundary_match(pattern: str, text: str) -> bool:
    """Match word boundary strictly; prevents 'Star' matching 'Starlight'."""
    if not pattern or not text:
        return False
    escaped = re.escape(pattern.strip())
    # Allow whitespace flexibility
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    regex = rf"\b{escaped}\b"
    return bool(re.search(regex, text, re.I))


@dataclass
class ProofItem:
    level: str  # STRONG, SUPPORTING, HINT, CONTRADICTION
    check_type: str  # LEGAL_NAME, REGISTRATION_NO, GROUP_SUBSIDIARY, TITLE_BRAND, ADDRESS, PHONE, COUNTRY, DOMAIN_SIMILARITY
    source_url: str
    evidence_snippet: str
    explanation: str
    source_type: str = "website"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProofEvaluation:
    decision: str  # ACCEPT, REJECT, NEEDS_REVIEW
    decision_reason: str
    answer_type: str  # OWN_SITE, GROUP_SITE, REGIONAL_SITE, DISTRIBUTOR_PAGE, PARKED, UNREACHABLE, CLOSED, UNKNOWN
    proof_level: str  # STRONG, SUPPORTING, HINT_ONLY, CONTRADICTORY, NONE
    confidence: str  # HIGH, MEDIUM, LOW
    proofs: list[ProofItem] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)
    detected_entities: dict[str, list[str]] = field(default_factory=dict)
    destination_url: str = ""
    is_redirected: bool = False
    inactive_subtype: str = ""  # NO_DOMAIN_SUPPLIED, DOMAIN_DOES_NOT_EXIST, PARKED_OR_FOR_SALE, TEMPORARILY_UNREACHABLE, ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_proof_ladder(
    row: dict,
    normalized: NormalizedDomain,
    domain: DomainRecord | None,
    evidence_list: list[Evidence] | None = None,
    *,
    network_healthy: bool = True,
    reviewer_decision: dict | None = None,
) -> dict:
    """Evaluate organization-domain pairing using the strict Proof Ladder."""
    # 0. Check for human reviewer override from persistent store (Recommendation 2.7)
    if reviewer_decision:
        required = ("decision", "reason", "evidence_url", "evidence_text", "reviewer", "reviewed_at", "fetched_at")
        reviewed_url = str(reviewer_decision.get("evidence_url") or "")
        if all(reviewer_decision.get(key) for key in required) and urlsplit(reviewed_url).scheme in ("http", "https"):
            human_proof = ProofItem(
                level="STRONG", check_type="HUMAN_REVIEW", source_url=reviewed_url,
                evidence_snippet=str(reviewer_decision["evidence_text"])[:300],
                explanation=f"Reviewer {reviewer_decision['reviewer']} documented this decision on {reviewer_decision['reviewed_at']}.",
                source_type="human_review")
            return _format_output(row, normalized, domain, ProofEvaluation(
                decision=reviewer_decision["decision"],
                decision_reason=f"Human review: {reviewer_decision['reason']}",
                answer_type=reviewer_decision.get("answer_type", "UNKNOWN"),
                proof_level="STRONG", confidence="HIGH", proofs=[human_proof],
                inactive_subtype=reviewer_decision.get("inactive_subtype", ""),
                checks={"human_reviewed": "PASSED"}))

    org_name = str(row.get("Organization Name") or row.get("organization_for_verification") or "").strip()
    country_code = str(row.get("Country") or row.get("\ufeffCountry") or "").strip().upper()
    checks: dict[str, str] = {
        "legal_name_match": "NOT_CHECKED",
        "registration_number_match": "NOT_CHECKED",
        "group_subsidiary_link": "NOT_CHECKED",
        "title_brand_match": "NOT_CHECKED",
        "address_match": "NOT_CHECKED",
        "phone_dial_match": "NOT_CHECKED",
        "country_word_match": "NOT_CHECKED",
        "domain_similarity_hint": "NOT_CHECKED",
        "contact_email_hint": "NOT_CHECKED",
        "unrelated_owner_check": "NOT_CHECKED",
        "parked_check": "NOT_CHECKED",
        "closure_check": "NOT_CHECKED",
    }

    # 1. Check for empty or invalid input domain (Gap 10)
    if not normalized or normalized.error or not normalized.normalized_domain:
        empty_input = not normalized or not normalized.original_domain
        return _format_output(
            row, normalized, domain,
            ProofEvaluation(
                decision="REJECT" if empty_input else "NEEDS_REVIEW",
                decision_reason="No domain supplied in input." if empty_input else f"Invalid supplied domain: {normalized.error}.",
                answer_type="UNKNOWN",
                proof_level="NONE",
                confidence="HIGH" if empty_input else "LOW",
                inactive_subtype="NO_DOMAIN_SUPPLIED" if empty_input else "INVALID_INPUT",
                checks={**checks, "domain_supplied": "FAILED"},
            )
        )

    # 2. Check for missing domain investigation
    if domain is None:
        return _format_output(
            row, normalized, domain,
            ProofEvaluation(
                decision="NEEDS_REVIEW",
                decision_reason="Domain has not been fetched or evaluated.",
                answer_type="UNKNOWN",
                proof_level="NONE",
                confidence="LOW",
                checks=checks,
            )
        )

    # 3. Check for Non-existent domain (NXDOMAIN / DNS Failed)
    if domain.dns_status in ("FAILED", "NXDOMAIN") and domain.http_status == 0:
        if network_healthy and domain.fetch_error_type == "NXDOMAIN":
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="REJECT",
                    decision_reason="Domain does not exist in DNS (NXDOMAIN / lookup failed).",
                    answer_type="UNKNOWN",
                    proof_level="CONTRADICTORY",
                    confidence="HIGH",
                    inactive_subtype="DOMAIN_DOES_NOT_EXIST",
                    checks={**checks, "dns_resolution": "FAILED"},
                )
            )
        else:
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="NEEDS_REVIEW",
                    decision_reason="Local network unhealthy; DNS lookup inconclusive.",
                    answer_type="UNKNOWN",
                    proof_level="NONE",
                    confidence="LOW",
                    inactive_subtype="TEMPORARILY_UNREACHABLE",
                    checks={**checks, "dns_resolution": "UNVERIFIED"},
                )
            )

    # 4. Check for Parked or For-Sale domains (Gap 10)
    if domain.parked or domain.http_status == 410:
        return _format_output(
            row, normalized, domain,
            ProofEvaluation(
                decision="REJECT",
                decision_reason="Domain is parked, for sale, or expired (HTTP 410 / Parking registrar signature).",
                answer_type="PARKED",
                proof_level="CONTRADICTORY",
                confidence="HIGH",
                inactive_subtype="PARKED_OR_FOR_SALE",
                checks={**checks, "parked_check": "FAILED"},
            )
        )

    # 5. Check for Network / Server / Block Failures without readable content (Gap 5)
    # Sites that can't be read are NEVER judged as valid or mismatch!
    has_readable_pages = bool(domain.pages and any(
        200 <= int(p.get("status", domain.http_status) or 0) < 400
        and (len(p.get("visible_text", "").strip()) >= 40 or p.get("jsonld"))
        for p in domain.pages))
    
    if not has_readable_pages:
        if domain.http_status in (403, 429) or domain.blocked_reason:
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="NEEDS_REVIEW",
                    decision_reason=f"Site blocked by anti-bot WAF / HTTP {domain.http_status}; content unreadable.",
                    answer_type="UNKNOWN",
                    proof_level="NONE",
                    confidence="LOW",
                    inactive_subtype="TEMPORARILY_UNREACHABLE",
                    checks={**checks, "page_content_readable": "FAILED"},
                )
            )
        if domain.http_status in (404, 451) or (domain.http_status >= 500):
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="NEEDS_REVIEW",
                    decision_reason=f"HTTP server error {domain.http_status}; homepage unreachable.",
                    answer_type="UNKNOWN",
                    proof_level="NONE",
                    confidence="LOW",
                    inactive_subtype="TEMPORARILY_UNREACHABLE",
                    checks={**checks, "page_content_readable": "FAILED"},
                )
            )
        if domain.http_status == 0 or domain.fetch_error:
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="NEEDS_REVIEW",
                    decision_reason=f"Connection failure / timeout: {domain.fetch_error or 'HTTP 0'}.",
                    answer_type="UNKNOWN",
                    proof_level="NONE",
                    confidence="LOW",
                    inactive_subtype="TEMPORARILY_UNREACHABLE",
                    checks={**checks, "page_content_readable": "FAILED"},
                )
            )
        return _format_output(row, normalized, domain, ProofEvaluation(
            decision="NEEDS_REVIEW", decision_reason="No readable company content was fetched.",
            answer_type="UNKNOWN", proof_level="NONE", confidence="LOW",
            inactive_subtype="TEMPORARILY_UNREACHABLE",
            checks={**checks, "page_content_readable": "FAILED"}))

    # Combine extracted pages
    pages = domain.pages
    page_texts = " ".join(p.get("visible_text", "") for p in pages)
    footers = " ".join(p.get("footer", "") for p in pages)
    titles = " ".join(p.get("title", "") for p in pages)
    h1s = " ".join(" ".join(p.get("h1", [])) for p in pages)
    descriptions = " ".join(p.get("description", "") for p in pages)
    registrations = [r for p in pages for r in p.get("registrations", [])]
    emails = [e for p in pages for e in p.get("emails", [])]
    phones = [ph for p in pages for ph in p.get("phones", [])]
    
    # Check for near-empty JavaScript shell pages (e.g. "Loading...", "Enable JavaScript")
    if len(page_texts.strip()) < 120 and any(p.get("js_shell") for p in pages):
        return _format_output(
            row, normalized, domain,
            ProofEvaluation(
                decision="NEEDS_REVIEW",
                decision_reason="Page is an unrendered JavaScript single-page application ('Loading...'). Needs headless browser review.",
                answer_type="UNKNOWN",
                proof_level="NONE",
                confidence="LOW",
                checks={**checks, "page_content_readable": "FAILED"},
            )
        )

    # Check for Parked signature in readable text
    for pat in PARKED_PATTERNS:
        if pat.search(titles) or pat.search(page_texts[:1500]):
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="REJECT",
                    decision_reason="Page content indicates domain is parked or for sale.",
                    answer_type="PARKED",
                    proof_level="CONTRADICTORY",
                    confidence="HIGH",
                    inactive_subtype="PARKED_OR_FOR_SALE",
                    checks={**checks, "parked_check": "FAILED"},
                )
            )
    checks["parked_check"] = "PASSED"

    # A closure sentence can refer to another company. Preserve it for review;
    # it is not evidence that this website is unrelated to the input entity.
    for pat in CLOSED_PATTERNS:
        match = pat.search(page_texts[:5000])
        if match and check_word_boundary_match(org_name, page_texts[max(0, match.start()-160):match.end()+160]):
            return _format_output(
                row, normalized, domain,
                ProofEvaluation(
                    decision="NEEDS_REVIEW",
                    decision_reason="Website appears to state that this company ceased operations; confirm against an official register.",
                    answer_type="CLOSED",
                    proof_level="SUPPORTING",
                    confidence="LOW",
                    checks={**checks, "closure_check": "FLAGGED"},
                )
            )
    checks["closure_check"] = "PASSED"

    # -----------------------------------------------------------------------
    # PROOF LADDER EVALUATION
    # -----------------------------------------------------------------------
    proofs: list[ProofItem] = []
    strong_proofs: list[ProofItem] = []
    supporting_proofs: list[ProofItem] = []
    hints: list[ProofItem] = []
    contradictions: list[str] = []

    core_org = extract_core_company_name(org_name)
    norm_org = normalize_name(org_name)

    # -----------------------------------------------------------------------
    # LEVEL 1: STRONG PROOFS (Solo Accept)
    # -----------------------------------------------------------------------
    
    # 1.0 External / Authoritative Registry Evidence (Gap 1 & Recommendation 2.1)
    if evidence_list:
        for ev in evidence_list:
            source_host = (urlsplit(ev.url).hostname or "").lower().removeprefix("www.")
            final_host = (urlsplit(domain.final_url).hostname or "").lower().removeprefix("www.")
            first_party = bool(source_host and final_host and source_host == final_host)
            external_domain_link = bool(normalized.normalized_domain and normalized.normalized_domain.lower() in ev.text.lower())
            exact_source_ok = (first_party and ev.evidence_type in ("organization_jsonld_legal_name", "official_legal_text")) or (external_domain_link and ev.evidence_type in ("government_registry", "official_filing"))
            group_source_ok = (first_party and ev.evidence_type == "official_relationship_text") or (external_domain_link and ev.evidence_type in ("official_parent", "government_registry", "official_filing"))
            if ev.relationship == "exact_entity" and ev.strength in ("STRONG", "VERY_STRONG") and exact_source_ok:
                strong = ProofItem(
                    level="STRONG",
                    check_type="LEGAL_NAME",
                    source_url=ev.url,
                    evidence_snippet=ev.text[:200],
                    explanation=f"Fetched source confirms exact entity: {ev.text[:120]}",
                    source_type=ev.source_type,
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                checks["legal_name_match"] = "PASSED"
            elif ev.relationship == "group_entity" and ev.strength in ("STRONG", "VERY_STRONG") and group_source_ok:
                strong = ProofItem(
                    level="STRONG",
                    check_type="GROUP_SUBSIDIARY",
                    source_url=ev.url,
                    evidence_snippet=ev.text[:200],
                    explanation=f"Fetched source confirms corporate group relationship: {ev.text[:120]}",
                    source_type=ev.source_type,
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                checks["group_subsidiary_link"] = "PASSED"

    # 1.A Full Legal Name in Schema / Footer / Legal Text (Gap 4)
    legal_name_found = False
    
    # Check JSON-LD schema
    for p in pages:
        for s in p.get("jsonld", []):
            s_legal = str(s.get("legalName") or "").strip()
            s_name = str(s.get("name") or "").strip()
            if s_legal and legal_identity_key(s_legal) == legal_identity_key(org_name):
                strong = ProofItem(
                    level="STRONG",
                    check_type="LEGAL_NAME",
                    source_url=p.get("url", ""),
                    evidence_snippet=f'JSON-LD legalName: "{s_legal}"',
                    explanation="Exact match of organization full legal name in structured JSON-LD data."
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                legal_name_found = True
            elif s_name and s_name.casefold().strip() == org_name.casefold().strip() and len(norm_org) >= 6:
                strong = ProofItem(
                    level="STRONG",
                    check_type="LEGAL_NAME",
                    source_url=p.get("url", ""),
                    evidence_snippet=f'JSON-LD name: "{s_name}"',
                    explanation="Full legal name match in organization schema."
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                legal_name_found = True

    # Check footer and copyright for full legal name
    if not legal_name_found and norm_org:
        for p in pages:
            footer_clean = _clean_text_for_search(p.get("footer", ""))
            if check_word_boundary_match(org_name, footer_clean):
                strong = ProofItem(
                    level="STRONG",
                    check_type="LEGAL_NAME",
                    source_url=p.get("url", ""),
                    evidence_snippet=footer_clean[max(0, footer_clean.casefold().find(org_name.casefold())-80):][:240],
                    explanation="Full legal entity name identified in website footer or copyright section."
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                legal_name_found = True
                break

    # Check full visible text of legal / privacy / imprint pages
    if not legal_name_found and norm_org:
        for p in pages:
            url_path = urlsplit(p.get("url", "")).path.lower()
            if any(k in url_path for k in ("legal", "privacy", "terms", "imprint", "about")):
                p_text = _clean_text_for_search(p.get("visible_text", ""))
                name_match = re.search(rf"\b{re.escape(org_name)}\b", p_text, re.I)
                context = p_text[max(0, name_match.start()-140):name_match.end()+140] if name_match else ""
                owner_context = re.search(r"\b(?:operated by|owned by|registered (?:as|company)|data controller|privacy controller|legal entity|copyright|contact us at|company number|registration number)\b", context, re.I)
                if name_match and owner_context:
                    strong = ProofItem(
                        level="STRONG",
                        check_type="LEGAL_NAME",
                        source_url=p.get("url", ""),
                        evidence_snippet=context[:240],
                        explanation="Full legal name appears with an ownership or operator statement on a corporate page."
                    )
                    strong_proofs.append(strong)
                    proofs.append(strong)
                    legal_name_found = True
                    break

    checks["legal_name_match"] = "PASSED" if legal_name_found else "FAILED"

    # 1.B Official Company Registration Number / Tax ID / UEN / ABN
    row_reg_no = str(row.get("Registration Number") or row.get("Company ID") or row.get("UEN") or row.get("ABN") or "").strip()
    reg_matched = False
    if row_reg_no and len(row_reg_no) >= 5:
        # Check against extracted registrations or body text
        for r_item in registrations:
            if row_reg_no.upper() in r_item.upper():
                strong = ProofItem(
                    level="STRONG",
                    check_type="REGISTRATION_NO",
                    source_url=pages[0].get("url", "") if pages else "",
                    evidence_snippet=f'Registration match: {r_item}',
                    explanation=f"Exact match on official corporate registration number ({row_reg_no})."
                )
                strong_proofs.append(strong)
                proofs.append(strong)
                reg_matched = True
                break
        if not reg_matched and check_word_boundary_match(row_reg_no, page_texts):
            strong = ProofItem(
                level="STRONG",
                check_type="REGISTRATION_NO",
                source_url=pages[0].get("url", "") if pages else "",
                evidence_snippet=f'Corporate ID in body: {row_reg_no}',
                explanation=f"Exact match on official corporate ID ({row_reg_no}) in page body."
            )
            strong_proofs.append(strong)
            proofs.append(strong)
            reg_matched = True
        checks["registration_number_match"] = "PASSED" if reg_matched else "FAILED"
    else:
        checks["registration_number_match"] = "NOT_CHECKED"

    # 1.C Subsidiary named on Corporate Group website (Gap 8)
    # A group site is ONLY strong proof if it explicitly names this subsidiary or regional entity!
    group_subsidiary_found = False
    for p in pages:
        p_text = p.get("visible_text", "")
        for pat in SUBSIDIARY_PATTERNS:
            m = pat.search(p_text)
            if m:
                # Require the full legal entity close to the stated relationship.
                start = max(0, m.start() - 120)
                end = min(len(p_text), m.end() + 120)
                window = p_text[start:end]
                if check_word_boundary_match(org_name, window):
                    strong = ProofItem(
                        level="STRONG",
                        check_type="GROUP_SUBSIDIARY",
                        source_url=p.get("url", ""),
                        evidence_snippet=window.strip()[:180],
                        explanation=f"Parent group website explicitly lists '{org_name}' as a subsidiary or group member."
                    )
                    strong_proofs.append(strong)
                    proofs.append(strong)
                    group_subsidiary_found = True
                    break
        if group_subsidiary_found:
            break
    checks["group_subsidiary_link"] = "PASSED" if group_subsidiary_found else "FAILED"

    # -----------------------------------------------------------------------
    # LEVEL 2: SUPPORTING PROOFS (Need 2, at least 1 must be entity name)
    # -----------------------------------------------------------------------
    
    # 2.A Entity or Brand Name in Title or H1 (Whole words only)
    title_brand_found = False
    if core_org and len(core_org) >= 3:
        if check_word_boundary_match(core_org, titles) or check_word_boundary_match(core_org, h1s):
            sup = ProofItem(
                level="SUPPORTING",
                check_type="TITLE_BRAND",
                source_url=pages[0].get("url", "") if pages else "",
                evidence_snippet=f'Title/H1: "{titles[:100]}"',
                explanation=f"Entity name '{core_org}' found as a complete word in page title or primary heading."
            )
            supporting_proofs.append(sup)
            proofs.append(sup)
            title_brand_found = True
    checks["title_brand_match"] = "PASSED" if title_brand_found else "FAILED"

    # 2.B Matching Country (WHOLE WORDS ONLY, NO 2-LETTER CODES) (Gap 3)
    country_matched = False
    c_def = COUNTRY_DEFINITIONS.get(country_code)
    if c_def:
        # Check whole word full country names
        for c_pat in c_def["patterns"]:
            if re.search(c_pat, footers, re.I) or any(re.search(c_pat, str(s.get("address", "")), re.I) for p in pages for s in p.get("jsonld", [])):
                sup = ProofItem(
                    level="SUPPORTING",
                    check_type="COUNTRY",
                    source_url=pages[0].get("url", "") if pages else "",
                    evidence_snippet=f'Country match: {c_def["name"]}',
                    explanation=f"Official country name '{c_def['name']}' matched as a whole word."
                )
                supporting_proofs.append(sup)
                proofs.append(sup)
                country_matched = True
                break
        checks["country_word_match"] = "PASSED" if country_matched else "FAILED"
    else:
        checks["country_word_match"] = "NOT_CHECKED"

    # 2.C Matching Phone Dial Code or City / Address
    phone_dial_matched = False
    if c_def:
        for d_pat in c_def["dial_codes"]:
            for ph in phones:
                if re.search(d_pat, ph):
                    sup = ProofItem(
                        level="SUPPORTING",
                        check_type="PHONE",
                        source_url=pages[0].get("url", "") if pages else "",
                        evidence_snippet=f'Phone number: {ph}',
                        explanation=f"Phone dial code matches company's expected jurisdiction ({c_def['name']})."
                    )
                    supporting_proofs.append(sup)
                    proofs.append(sup)
                    phone_dial_matched = True
                    break
            if phone_dial_matched:
                break
        checks["phone_dial_match"] = "PASSED" if phone_dial_matched else "FAILED"
    else:
        checks["phone_dial_match"] = "NOT_CHECKED"

    # City / Address Match
    city_matched = False
    if c_def:
        for city_pat in c_def["cities"]:
            if re.search(city_pat, footers, re.I):
                sup = ProofItem(
                    level="SUPPORTING",
                    check_type="ADDRESS",
                    source_url=pages[0].get("url", "") if pages else "",
                    evidence_snippet=f'City match in text',
                    explanation=f"Jurisdictional city matched within official address/contact section."
                )
                supporting_proofs.append(sup)
                proofs.append(sup)
                city_matched = True
                break
        checks["address_match"] = "PASSED" if city_matched else "FAILED"
    else:
        checks["address_match"] = "NOT_CHECKED"

    # -----------------------------------------------------------------------
    # LEVEL 3: HINTS ONLY (Never enough on their own) (Gap 2)
    # -----------------------------------------------------------------------
    
    # 3.A Domain name looks like company name
    host_clean = re.sub(r"[^a-z0-9]", "", normalized.normalized_domain.lower())
    core_clean = re.sub(r"[^a-z0-9]", "", core_org.lower())
    if core_clean and (core_clean in host_clean or host_clean in core_clean) and len(core_clean) >= 4:
        hint = ProofItem(
            level="HINT",
            check_type="DOMAIN_SIMILARITY",
            source_url=normalized.requested_url,
            evidence_snippet=f"Domain host '{normalized.normalized_domain}' resembles core name '{core_org}'",
            explanation="Domain name resembles company name (hint only, never sufficient on its own).",
            source_type="input",
        )
        hints.append(hint)
        proofs.append(hint)
        checks["domain_similarity_hint"] = "PASSED"
    else:
        checks["domain_similarity_hint"] = "FAILED"

    # 3.B Contact email on site's own domain
    site_domain = normalized.registered_domain or normalized.normalized_domain
    contact_email_found = any(e.endswith(f"@{site_domain}") or e.endswith(f".{site_domain}") for e in emails)
    if contact_email_found:
        hint = ProofItem(
            level="HINT",
            check_type="CONTACT_EMAIL",
            source_url=pages[0].get("url", "") if pages else "",
            evidence_snippet=f"Contact emails present on domain @{site_domain}",
            explanation="Contact email uses the website's own domain (self-referential hint only)."
        )
        hints.append(hint)
        proofs.append(hint)
        checks["contact_email_hint"] = "PASSED"
    else:
        checks["contact_email_hint"] = "FAILED"

    # -----------------------------------------------------------------------
    # CONTRADICTION & MISMATCH CHECKS (Gap 6)
    # Call a mismatch ONLY when the site clearly names a DIFFERENT company as owner
    # -----------------------------------------------------------------------
    unrelated_owner_found = False
    
    # Check external authoritative mismatch evidence (Gap 6)
    if evidence_list:
        for ev in evidence_list:
            if (ev.relationship == "unrelated" and ev.strength in ("STRONG", "VERY_STRONG")
                    and ev.evidence_type in ("government_registry", "official_filing", "official_parent")
                    and normalized.normalized_domain.lower() in ev.text.lower()):
                contradictions.append(f"Authoritative evidence identifies unrelated owner: {ev.text}")
                unrelated_owner_found = True
                proofs.append(ProofItem(
                    level="CONTRADICTORY",
                    check_type="AUTHORITATIVE_MISMATCH",
                    source_url=ev.url,
                    evidence_snippet=ev.text[:200],
                    explanation=f"Official registry confirms domain belongs to unrelated party: {ev.text[:120]}",
                    source_type=ev.source_type,
                ))
                break
    
    # Extract distinct copyright / legal owner from footer
    copyright_m = re.search(r"(?:©|copyright|\(c\))\s*(?:\d{4}[-\s\d]*)?\s*([A-Za-z0-9&.,\s]{4,60})", footers, re.I)
    if copyright_m:
        claimed_owner = copyright_m.group(1).strip()
        claimed_clean = normalize_name(claimed_owner)
        # Check if claimed owner contradicts the organization
        if claimed_clean and len(claimed_clean) >= 5 and core_clean not in claimed_clean and claimed_clean not in core_clean:
            # Check whether it's a generic word like 'all rights reserved'
            if not any(w in claimed_clean for w in ("all rights", "reserved", "inc", "privacy", "terms", "sitemap")):
                # If there are NO strong proofs for our target org, this is a clear mismatch
                if not strong_proofs and not title_brand_found:
                    contradictions.append(f"Site footer names '{claimed_owner}'; parent or subsidiary relationship remains unverified.")

    checks["unrelated_owner_check"] = "FAILED" if unrelated_owner_found else "PASSED"

    # -----------------------------------------------------------------------
    # REDIRECT EVALUATION (Gap 7)
    # Check if domain redirected to another registered domain
    # -----------------------------------------------------------------------
    is_redirected = False
    dest_url = domain.final_url or ""
    if domain.final_registered_domain and normalized.registered_domain:
        if domain.final_registered_domain != normalized.registered_domain:
            is_redirected = True

    # -----------------------------------------------------------------------
    # FINAL THREE-STATE DECISION ARBITRATION (2.1 & 2.2)
    # -----------------------------------------------------------------------
    decision: str
    decision_reason: str
    answer_type: str
    overall_proof_level: str
    confidence: str

    if unrelated_owner_found and not strong_proofs:
        decision = "REJECT"
        decision_reason = f"Mismatch: website clearly names a different legal entity as owner: {contradictions[0]}"
        answer_type = "UNKNOWN"
        overall_proof_level = "CONTRADICTORY"
        confidence = "HIGH"

    elif strong_proofs:
        decision = "ACCEPT"
        first_strong = next((p for p in strong_proofs if p.check_type in ("LEGAL_NAME", "REGISTRATION_NO")), strong_proofs[0])
        decision_reason = f"Strong Proof: {first_strong.explanation} [{first_strong.evidence_snippet}]"
        answer_type = "GROUP_SITE" if first_strong.check_type == "GROUP_SUBSIDIARY" else "OWN_SITE"
        overall_proof_level = "STRONG"
        confidence = "HIGH"
        if is_redirected:
            decision_reason = f"Redirect Destination Verified: {decision_reason} (Redirected to {dest_url})"

    elif len({p.check_type for p in supporting_proofs}) >= 2 and title_brand_found:
        decision = "NEEDS_REVIEW"
        reasons_list = [p.check_type for p in supporting_proofs]
        decision_reason = f"Supporting signals ({', '.join(reasons_list)}) need manual calibration before acceptance."
        answer_type = "OWN_SITE"
        overall_proof_level = "SUPPORTING"
        confidence = "MEDIUM"
        if is_redirected:
            decision_reason = f"Redirect Destination Verified: {decision_reason} (Redirected to {dest_url})"

    elif hints and not supporting_proofs and not strong_proofs:
        # Only hints exist (domain resembles name, self-referential email) -> Gap 2: NEVER ACCEPT
        decision = "NEEDS_REVIEW"
        decision_reason = "Insufficient Proof: domain matches name as a hint only, but page does not substantiate the legal entity."
        answer_type = "UNKNOWN"
        overall_proof_level = "HINT_ONLY"
        confidence = "LOW"

    else:
        # Weak or incomplete evidence
        decision = "NEEDS_REVIEW"
        if supporting_proofs:
            decision_reason = f"Partial Evidence: found {supporting_proofs[0].explanation}, but lacks required second proof or explicit legal ownership."
        else:
            decision_reason = "No verifiable linkage found between website content and the requested corporate entity."
        answer_type = "UNKNOWN"
        overall_proof_level = "NONE"
        confidence = "LOW"

    eval_result = ProofEvaluation(
        decision=decision,
        decision_reason=decision_reason,
        answer_type=answer_type,
        proof_level=overall_proof_level,
        confidence=confidence,
        proofs=proofs,
        checks=checks,
        contradictions=contradictions,
        detected_entities={
            "core_org": [core_org],
            "strong_proofs": [p.explanation for p in strong_proofs],
            "supporting_proofs": [p.explanation for p in supporting_proofs],
        },
        destination_url=dest_url if is_redirected else "",
        is_redirected=is_redirected,
    )

    return _format_output(row, normalized, domain, eval_result)


def _format_output(
    row: dict,
    normalized: NormalizedDomain,
    domain: DomainRecord | None,
    evaluation: ProofEvaluation,
) -> dict:
    """Format decision output for export, preserving full compatibility across old and new pipelines."""
    # Map terminal 3-state decisions to historical status fields for complete pipeline compatibility
    if evaluation.decision == "ACCEPT":
        hist_status = "VERIFIED_GROUP" if evaluation.answer_type == "GROUP_SITE" else "VERIFIED_EXACT"
        elim_classification = "VALID_GROUP" if evaluation.answer_type == "GROUP_SITE" else "VALID"
    elif evaluation.decision == "REJECT":
        if evaluation.inactive_subtype:
            hist_status = "INACTIVE"
            elim_classification = "INACTIVE"
        else:
            hist_status = "MISMATCH"
            elim_classification = "MISMATCH"
    else:  # NEEDS_REVIEW
        if evaluation.proof_level == "SUPPORTING" and not evaluation.inactive_subtype:
            hist_status = "PROBABLE"
            elim_classification = "PROBABLE"
        elif evaluation.inactive_subtype == "TEMPORARILY_UNREACHABLE":
            hist_status = "BLOCKED" if (domain and domain.http_status in (403, 429)) else "UNVERIFIED"
            elim_classification = "BLOCKED" if (domain and domain.http_status in (403, 429)) else "REVIEW"
        else:
            hist_status = "UNVERIFIED"
            elim_classification = "REVIEW"

    is_redirected = evaluation.is_redirected or bool(
        domain and domain.final_registered_domain and normalized.registered_domain
        and domain.final_registered_domain != normalized.registered_domain)
    destination_status = hist_status if is_redirected else ""
    if is_redirected and evaluation.decision == "NEEDS_REVIEW":
        hist_status = "REDIRECT"
        elim_classification = "REVIEW"

    identity = candidate_identity(domain.pages if domain else [])
    home = domain.pages[0] if domain and domain.pages else {}
    proof_sources = [p.source_url for p in evaluation.proofs if p.source_url]
    
    return {
        # Core terminal decision (Logical Framework 2.2)
        "decision": evaluation.decision,
        "decision_reason": evaluation.decision_reason,
        "answer_type": evaluation.answer_type,
        "proof_level": evaluation.proof_level,
        "confidence": evaluation.confidence,
        
        # Transparent Check Accounting (Gap 17)
        "checks_performed": evaluation.checks,
        "inactive_subtype": evaluation.inactive_subtype,
        
        # Legacy / pipeline compatibility fields
        "classification": elim_classification,
        "verification_status": hist_status,
        "verification_reason": evaluation.decision_reason,
        "destination_verification_status": destination_status,
        "exact_entity_match": evaluation.answer_type == "OWN_SITE" and evaluation.proof_level == "STRONG",
        "corporate_group_match": evaluation.answer_type == "GROUP_SITE",
        
        # Domains and URLs
        "original_domain": normalized.original_domain if normalized else "",
        "normalized_domain": normalized.normalized_domain if normalized else "",
        "registered_domain": normalized.registered_domain if normalized else "",
        "requested_url": normalized.requested_url if normalized else "",
        "final_url": domain.final_url if domain else "",
        "final_registered_domain": domain.final_registered_domain if domain else "",
        "redirect_destination": evaluation.destination_url or (domain.final_url if is_redirected and domain else ""),
        "is_redirected": is_redirected,
        
        # Network facts
        "http_status": domain.http_status if domain else 0,
        "dns_status": domain.dns_status if domain else "UNKNOWN",
        "https_available": domain.https_available if domain else None,
        "redirect_chain": domain.redirect_chain if domain else [],
        "domain_active": False if evaluation.decision == "REJECT" and evaluation.inactive_subtype in ("DOMAIN_DOES_NOT_EXIST", "PARKED_OR_FOR_SALE") else domain.domain_active if domain else None,
        "fetch_method": domain.fetch_method if domain else "NONE",
        "fetch_attempts": domain.fetch_attempts if domain else 0,
        "blocked_reason": domain.blocked_reason if domain else "",
        "checked_at": domain.checked_at if domain else "",
        "website_title": home.get("title", ""),
        "website_h1": home.get("h1", []),
        "website_description": home.get("description", ""),
        "detected_company_name": identity.get("company_names", []),
        "detected_legal_entity": identity.get("legal_entities", []),
        "detected_parent_company": identity.get("parents", []),
        "detected_brand": identity.get("brands", []),
        "detected_country": identity.get("countries", []),
        
        # Evidence trail
        "evidence_url_1": proof_sources[0] if proof_sources else "",
        "evidence_url_2": proof_sources[1] if len(proof_sources) > 1 else "",
        "evidence_type": [p.check_type for p in evaluation.proofs],
        "evidence_text": [p.evidence_snippet for p in evaluation.proofs[:5]],
        "evidence_reason": evaluation.decision_reason,
        "evidence_json": [{"url": p.source_url, "evidence_type": p.check_type,
                           "text": p.evidence_snippet, "relationship": evaluation.answer_type,
                           "strength": p.level, "source_type": p.source_type}
                          for p in evaluation.proofs],
        "contradiction_found": bool(evaluation.contradictions),
        "contradiction_type": evaluation.contradictions[0] if evaluation.contradictions else "",
    }
