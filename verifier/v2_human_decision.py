"""Compatibility adapter for the retired V2 scorer.

The old score used territory and domain similarity as ownership evidence. Every
caller now receives the canonical strict proof-ladder evaluation instead.
"""
from __future__ import annotations

from typing import Any

from .models import DomainRecord
from .normalization import NormalizedDomain
from .proof_ladder import evaluate_proof_ladder


def evaluate_human_researcher_decision(
    row: dict[str, Any], normalized: NormalizedDomain, domain: DomainRecord | None,
    *, network_healthy: bool = True,
) -> dict[str, Any]:
    return {**row, **evaluate_proof_ladder(row, normalized, domain, [], network_healthy=network_healthy)}
