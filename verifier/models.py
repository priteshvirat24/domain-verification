"""Serializable records shared by local and Actor execution."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any


STATUSES = (
    "VALID", "VALID_GROUP", "VERIFIED_EXACT", "VERIFIED_ENTITY", "VERIFIED_GROUP", "STRONG_MATCH", "PROBABLE",
    "REVIEW", "UNVERIFIED", "MISMATCH", "INACTIVE", "REDIRECT", "BLOCKED",
)


@dataclass
class Evidence:
    url: str
    evidence_type: str
    text: str
    relationship: str
    strength: str
    title: str = ""
    source_type: str = "website"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchRecord:
    requested_url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    method: str = "HTTP"
    attempts: int = 0
    error: str = ""
    error_type: str = ""
    checked_at: str = ""


@dataclass
class DomainRecord:
    domain: str
    registered_domain: str
    requested_url: str
    final_url: str = ""
    final_registered_domain: str = ""
    http_status: int = 0
    dns_status: str = "UNKNOWN"
    https_available: bool | None = None
    redirect_chain: list[str] = field(default_factory=list)
    domain_active: bool | None = None
    parked: bool = False
    blocked_reason: str = ""
    fetch_method: str = "NONE"
    fetch_attempts: int = 0
    checked_at: str = ""
    pages: list[dict[str, Any]] = field(default_factory=list)
    fetch_error: str = ""
    fetch_error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
