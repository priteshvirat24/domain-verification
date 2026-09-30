"""URL and organization normalization without changing source values."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

try:
    import tldextract
except ImportError:  # dependency is required in production, not for offline unit tests
    tldextract = None
_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=()) if tldextract else None

_LEGAL = re.compile(
    r"\b(?:pte\.?\s*ltd\.?|pty\.?\s*ltd\.?|sdn\.?\s*bhd\.?|co\.?\s*ltd\.?|"
    r"limited|ltd|incorporated|inc|corporation|corp|llc|gmbh|plc|bv|ag|"
    r"s\.?a\.?|k\.?k\.?)\b", re.I
)
_SPACE = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class NormalizedDomain:
    original_domain: str
    normalized_domain: str
    registered_domain: str
    requested_url: str
    error: str = ""


def registered_domain(host: str) -> str:
    """Use the public suffix list; never guess a two-label root for co.jp, etc."""
    if not host:
        return ""
    if _EXTRACTOR is None:
        return ""  # unknown is safer than an incorrect root
    part = _EXTRACTOR(host)
    return ".".join(x for x in (part.domain, part.suffix) if x)


def normalize_domain(value: object) -> NormalizedDomain:
    original = "" if value is None else str(value).strip()
    if not original:
        return NormalizedDomain(original, "", "", "", "empty domain")
    candidate = original if re.match(r"^https?://", original, re.I) else "https://" + original
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        ipaddress.ip_address(host)
        return NormalizedDomain(original, "", "", "", "IP address is not a domain")
    except ValueError:
        pass
    except (UnicodeError, AttributeError):
        return NormalizedDomain(original, "", "", "", "invalid hostname")
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return NormalizedDomain(original, "", "", "", "invalid hostname")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        return NormalizedDomain(original, "", "", "", "credentials or nonstandard port")
    scheme = parsed.scheme.lower()
    path = parsed.path or "/"
    requested = urlunsplit((scheme, host, path, parsed.query, ""))
    normalized_host = host[4:] if host.startswith("www.") else host
    return NormalizedDomain(original, normalized_host, registered_domain(normalized_host), requested)


def normalize_name(value: object) -> str:
    text = _SPACE.sub(" ", str(value or "").casefold()).strip()
    return _SPACE.sub(" ", _LEGAL.sub(" ", text)).strip()


def name_tokens(value: object) -> set[str]:
    return {t for t in normalize_name(value).split() if len(t) > 1}
