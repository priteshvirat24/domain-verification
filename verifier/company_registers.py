"""Official Corporate Registry Connectors & High-Risk Disambiguation.

Implements Recommendation 2.4:
"Check official registers for high-risk rows. Where a domain would be replaced or rejected,
look the company up in its country's free official register (for example ABN Lookup in Australia
or ACRA in Singapore). This confirms the exact legal name, separates same-name companies and
shows whether the company has closed or merged."
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

LOG = logging.getLogger("company_registers")

REGISTRY_ENDPOINTS = {
    "AU": {
        "name": "Australian Business Register (ABN Lookup)",
        "search_url_template": "https://abr.business.gov.au/Search/ResultsActive?SearchText={query}",
        "reg_id_name": "ABN / ACN",
    },
    "SG": {
        "name": "Accounting and Corporate Regulatory Authority (ACRA BizFile)",
        "search_url_template": "https://www.uen.gov.sg/ueninternet/faces/pages/uenSearch.jspx?searchQuery={query}",
        "reg_id_name": "UEN",
    },
    "GB": {
        "name": "Companies House UK",
        "search_url_template": "https://find-and-update.company-information.service.gov.uk/search?q={query}",
        "reg_id_name": "Company Number",
    },
    "JP": {
        "name": "National Tax Agency Corporate Number Publication Site",
        "search_url_template": "https://www.houjin-bangou.nta.go.jp/en/kensaku-kekka.html?NAME={query}",
        "reg_id_name": "Corporate Number",
    },
    "NZ": {
        "name": "New Zealand Companies Office",
        "search_url_template": "https://app.companiesoffice.govt.nz/companies/app/ui/pages/companies/search?q={query}",
        "reg_id_name": "NZBN",
    },
    "MY": {
        "name": "Suruhanjaya Syarikat Malaysia (SSM)",
        "search_url_template": "https://www.mydata-ssm.com.my/",
        "reg_id_name": "Registration No",
    },
    "US": {
        "name": "SEC EDGAR / SEC Corporate Filings",
        "search_url_template": "https://www.sec.gov/edgar/searchedgar/companysearch?company={query}",
        "reg_id_name": "CIK / EIN",
    },
}


def build_registry_lookup_link(organization: str, country_code: str) -> dict[str, str]:
    """Generate the official company register search URL for high-risk manual or automated review."""
    country = country_code.upper().strip()
    registry = REGISTRY_ENDPOINTS.get(country)
    if not registry:
        return {
            "registry_name": "General Web Search",
            "lookup_url": f"https://www.google.com/search?q={urllib.parse.quote_plus(organization + ' official company register')}",
            "reg_id_name": "Registration Number",
        }

    encoded_query = urllib.parse.quote_plus(organization.strip())
    return {
        "registry_name": registry["name"],
        "lookup_url": registry["search_url_template"].format(query=encoded_query),
        "reg_id_name": registry["reg_id_name"],
    }


def compare_registry_entities(
    candidate_name: str,
    target_name: str,
    candidate_country: str,
    target_country: str,
    candidate_reg_no: str = "",
    target_reg_no: str = "",
) -> dict[str, Any]:
    """Differentiate same-name companies across different jurisdictions or legal structures (Gap 9)."""
    # Check registration number if available
    if candidate_reg_no and target_reg_no:
        if candidate_reg_no.strip().upper() == target_reg_no.strip().upper():
            return {
                "match": True,
                "confidence": "EXACT_REGISTRATION_MATCH",
                "reason": f"Registration number {candidate_reg_no} matches exactly.",
            }
        else:
            return {
                "match": False,
                "confidence": "REGISTRATION_MISMATCH",
                "reason": f"Different registration numbers ({candidate_reg_no} vs {target_reg_no}). Distinct legal entities.",
            }

    # Compare country
    if candidate_country and target_country and candidate_country.upper() != target_country.upper():
        return {
            "match": False,
            "confidence": "JURISDICTION_MISMATCH",
            "reason": f"Entities in different jurisdictions ({candidate_country} vs {target_country}). Must not copy website without group proof.",
        }

    # Compare full legal name (e.g. Pty Ltd vs Pte Ltd)
    clean_cand = re.sub(r"\s+", " ", candidate_name.strip().lower())
    clean_target = re.sub(r"\s+", " ", target_name.strip().lower())

    cand_has_pty = bool(re.search(r"\bpty\b", clean_cand))
    target_has_pty = bool(re.search(r"\bpty\b", clean_target))
    cand_has_pte = bool(re.search(r"\bpte\b", clean_cand))
    target_has_pte = bool(re.search(r"\bpte\b", clean_target))

    if (cand_has_pty != target_has_pty) or (cand_has_pte != target_has_pte):
        return {
            "match": False,
            "confidence": "LEGAL_STRUCTURE_MISMATCH",
            "reason": f"Legal structures differ ('{candidate_name}' vs '{target_name}'). Collapsing legal endings is prohibited.",
        }

    return {
        "match": True if clean_cand == clean_target else False,
        "confidence": "NAME_COMPARISON",
        "reason": "Names compared with legal structures intact.",
    }
