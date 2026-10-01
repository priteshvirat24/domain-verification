"""Bounded crawler configuration."""
from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    concurrency: int = 100
    per_host_delay_seconds: float = 0.2
    timeout_seconds: int = 7
    max_http_attempts: int = 2
    max_evidence_pages: int = 4
    max_redirects: int = 8
    cache_ttl_seconds: int = 30 * 86400
    use_dynamic: bool = False
    use_stealth: bool = False
    use_proxy: bool = False
    respect_robots: bool = True
    max_body_bytes: int = 800_000
    use_apify: bool = True
    apify_token: str | None = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")

    def validate(self) -> None:
        if self.concurrency < 1 or self.concurrency > 200:
            raise ValueError("concurrency must be 1..200")
        if self.max_evidence_pages < 0 or self.max_evidence_pages > 10:
            raise ValueError("max_evidence_pages must be 0..10")
        if self.per_host_delay_seconds < 0:
            raise ValueError("per_host_delay_seconds must be nonnegative")
