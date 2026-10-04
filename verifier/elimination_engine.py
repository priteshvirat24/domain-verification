"""Compatibility adapter for older recovery commands.

All entity/domain decisions use the shared proof ladder. The former
elimination-first scorer accepted weak matches and must not be revived.
"""
from __future__ import annotations

from typing import Any

from .models import DomainRecord
from .normalization import NormalizedDomain
from .proof_ladder import evaluate_proof_ladder


CCTLD_MAP: dict[str, str] = {
    "sg": "SG", "my": "MY", "ph": "PH", "id": "ID", "th": "TH",
    "vn": "VN", "jp": "JP", "kr": "KR", "au": "AU", "nz": "NZ",
    "in": "IN", "cn": "CN", "hk": "HK", "tw": "TW", "us": "US",
    "uk": "GB", "ca": "CA", "pk": "PK", "bd": "BD",
}


def evaluate_elimination_decision(
    row: dict[str, Any], normalized: NormalizedDomain, domain: DomainRecord | None,
    *, network_healthy: bool = True,
) -> dict[str, Any]:
    """Return the canonical proof-ladder result for a legacy caller."""
    return evaluate_proof_ladder(row, normalized, domain, [], network_healthy=network_healthy)
