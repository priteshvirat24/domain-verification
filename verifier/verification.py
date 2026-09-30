"""Conservative, reproducible organization-domain decisions."""
from __future__ import annotations

from urllib.parse import urlsplit

from .entity_resolution import candidate_identity
from .models import DomainRecord, Evidence
from .normalization import NormalizedDomain, normalize_name


def _decision(row: dict, normalized: NormalizedDomain, domain: DomainRecord | None,
              evidence: list[Evidence], *, network_healthy: bool = True) -> dict:
    organization = str(row.get("Organization Name") or "")
    urls = list(dict.fromkeys(e.url for e in evidence))
    identity = candidate_identity(domain.pages if domain else [])
    exact = [e for e in evidence if e.relationship == "exact_entity" and e.strength in ("STRONG", "VERY_STRONG")]
    group = [e for e in evidence if e.relationship == "group_entity" and e.strength in ("STRONG", "VERY_STRONG")]
    unrelated = [e for e in evidence if e.relationship == "unrelated" and e.strength in ("STRONG", "VERY_STRONG")]
    weak_mentions = [e for e in evidence if e.relationship == "name_mention"]
    contact = [e for e in evidence if e.relationship == "site_control"]
    country_support = [e for e in evidence if e.relationship == "country_match"]
    status = "UNVERIFIED"
    reason = "Insufficient evidence of an official organization-domain relationship."
    confidence = "LOW"
    if normalized.error:
        reason = f"Invalid input domain: {normalized.error}."
    elif domain is None:
        reason = "Domain has not been fetched or independently verified."
    elif domain.fetch_error_type == "ROBOTS":
        status, reason = "BLOCKED", "robots.txt disallows the requested page."
    elif domain.fetch_error_type == "UNSAFE_ADDRESS":
        status, reason = "BLOCKED", "DNS returned a private or non-global address; fetch was refused."
    elif domain.parked or domain.http_status == 410:
        status, reason = "INACTIVE", "Fetched website shows parking/expiry or HTTP 410."
    elif domain.dns_status == "FAILED" and domain.http_status == 0 and network_healthy:
        status, reason = "INACTIVE", "DNS failed while network health was confirmed."
    elif domain.http_status in (403, 429) or (not (200 <= domain.http_status < 400) and domain.fetch_error_type in ("TLS", "TIMEOUT")):
        status, reason = "BLOCKED", "Access remained insufficient after bounded retries and enabled fallback."
    elif domain.http_status == 0:
        status, reason = ("BLOCKED", "Execution environment cannot reach external sites; domain status is unknown.") if not network_healthy else ("UNVERIFIED", "Fetch failed without evidence that the domain is inactive.")
    elif 500 <= domain.http_status <= 599:
        status, reason = "BLOCKED", "Server errors persisted after bounded retries."
    elif domain.http_status in (404, 451):
        status, reason = "UNVERIFIED", "Homepage is unavailable; domain ownership remains unknown."
    elif unrelated and not exact and not group:
        status, reason, confidence = "MISMATCH", "Authoritative evidence identifies an unrelated owner or relationship.", "HIGH"
    elif exact:
        status, reason, confidence = "VERIFIED_EXACT", "Official legal or structured identity evidence names the exact organization.", "HIGH"
    elif group:
        status, reason, confidence = "VERIFIED_GROUP", "Official corporate evidence explicitly connects the entity to the website's group.", "HIGH"
    elif weak_mentions and (contact or country_support):
        status, reason, confidence = "PROBABLE", "Entity mention and contact/country evidence agree; legal or group proof is missing.", "MEDIUM"
    elif len({e.url for e in weak_mentions}) >= 2:
        status, reason, confidence = "PROBABLE", "Multiple fetched pages mention the entity; official relationship is not established.", "LOW"
    final_host = (urlsplit(domain.final_url).hostname or "").lower() if domain else ""
    redirected = bool(domain and domain.final_registered_domain and normalized.registered_domain and
                      domain.final_registered_domain != normalized.registered_domain)
    destination_status = status
    if redirected:
        status = "REDIRECT"
        reason = f"Input domain redirects across registered domains to {domain.final_url}; destination assessment: {destination_status}. " + reason
    return {
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
        "domain_active": False if status == "INACTIVE" else domain.domain_active if domain else None,
        "website_title": domain.pages[0].get("title", "") if domain and domain.pages else "",
        "website_h1": domain.pages[0].get("h1", []) if domain and domain.pages else [],
        "website_description": domain.pages[0].get("description", "") if domain and domain.pages else "",
        "detected_company_name": identity["company_names"],
        "detected_legal_entity": identity["legal_entities"],
        "detected_parent_company": identity["parents"],
        "detected_brand": identity["brands"],
        "detected_country": identity["countries"],
        "exact_entity_match": bool(exact),
        "corporate_group_match": bool(group),
        "evidence_url_1": urls[0] if urls else "",
        "evidence_url_2": urls[1] if len(urls) > 1 else "",
        "evidence_type": [e.evidence_type for e in evidence],
        "evidence_text": [e.text for e in evidence[:5]],
        "evidence_json": [e.to_dict() for e in evidence],
        "evidence_reason": reason,
        "verification_status": status,
        "destination_verification_status": destination_status if redirected else "",
        "verification_reason": reason,
        "confidence": confidence,
        "fetch_method": domain.fetch_method if domain else "NONE",
        "fetch_attempts": domain.fetch_attempts if domain else 0,
        "blocked_reason": domain.blocked_reason if domain else "",
        "checked_at": domain.checked_at if domain else "",
    }


def verify_row(row: dict, normalized: NormalizedDomain, domain: DomainRecord | None,
               evidence: list[Evidence], *, network_healthy: bool = True) -> dict:
    from .elimination_engine import evaluate_elimination_decision
    return evaluate_elimination_decision(row, normalized, domain, network_healthy=network_healthy)
