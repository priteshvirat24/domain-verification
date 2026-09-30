"""Extract compact, traceable identity signals from bounded pages."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

KEYWORDS = ("about", "company", "corporate", "contact", "legal", "privacy", "terms", "imprint", "subsidiar")
PARKED = re.compile(r"domain (?:is )?(?:for sale|expired|parked)|buy this domain|sedo parking|afternic", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}")
REGISTRATION = re.compile(r"(?:registration|company|business|vat|uen|abn|acn)\s*(?:no\.?|number|id|#)\s*[:.]?\s*[A-Z0-9-]{5,25}", re.I)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.text: list[str] = []
        self.footer: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.meta_description = ""
        self.canonical = ""
        self.jsonld_raw: list[str] = []
        self._jsonld_current: list[str] = []
        self._tags: list[str] = []
        self._skip = 0
        self._jsonld = False
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self._tags.append(tag)
        if tag in ("style", "noscript"):
            self._skip += 1
        if tag == "script":
            self._jsonld = (attr.get("type") or "").lower() == "application/ld+json"
            if not self._jsonld:
                self._skip += 1
        if tag == "meta" and (attr.get("name") or "").lower() == "description":
            self.meta_description = (attr.get("content") or "")[:500]
        if tag == "link" and (attr.get("rel") or "").lower() == "canonical":
            self.canonical = attr.get("href") or ""
        if tag == "a":
            self._anchor = []
            self.links.append((attr.get("href") or "", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.links:
            href, _ = self.links[-1]
            self.links[-1] = (href, " ".join(self._anchor)[:120])
            self._anchor = []
        if tag == "script":
            if self._jsonld:
                self.jsonld_raw.append("".join(self._jsonld_current))
                self._jsonld_current = []
                self._jsonld = False
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1
        if tag in self._tags:
            self._tags = self._tags[:len(self._tags) - 1 - self._tags[::-1].index(tag)]

    def handle_data(self, data: str) -> None:
        if self._jsonld:
            self._jsonld_current.append(data)
            return
        if self._skip:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        self.text.append(clean)
        if "title" in self._tags:
            self.title.append(clean)
        if "h1" in self._tags:
            self.h1.append(clean)
        if "h2" in self._tags:
            self.h2.append(clean)
        if "footer" in self._tags:
            self.footer.append(clean)
        if "a" in self._tags:
            self._anchor.append(clean)


def _schemas(raw: list[str]) -> list[dict]:
    result: list[dict] = []
    for item in raw:
        try:
            data = json.loads(item)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "@graph" in node:
                    graph = node["@graph"]
                    stack.extend(graph if isinstance(graph, list) else [graph])
                if any(t in str(node.get("@type", "")) for t in ("Organization", "Corporation", "LocalBusiness", "WebSite")):
                    result.append({k: node.get(k) for k in ("@type", "name", "legalName", "url", "parentOrganization", "subOrganization", "address", "sameAs", "brand") if k in node})
    return result[:30]


def extract_page(url: str, html: str, status: int) -> dict:
    parser = _Parser()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = " ".join(parser.text)[:50_000]
    links = []
    host = (urlsplit(url).hostname or "").lower()
    for href, anchor in parser.links[:1000]:
        full = urljoin(url, href).split("#", 1)[0]
        if urlsplit(full).scheme not in ("http", "https"):
            continue
        links.append({"url": full, "anchor": anchor,
                      "internal": (urlsplit(full).hostname or "").lower() == host})
    emails = sorted(set(EMAIL.findall(text)))[:30]
    social_hosts = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com")
    social_links = [link["url"] for link in links if any((urlsplit(link["url"]).hostname or "").endswith(h) for h in social_hosts)]
    addresses = [schema["address"] for schema in _schemas(parser.jsonld_raw) if schema.get("address")]
    return {
        "url": url, "status": status, "title": " ".join(parser.title)[:300],
        "description": parser.meta_description, "h1": parser.h1[:10], "h2": parser.h2[:20],
        "visible_text": text, "footer": " ".join(parser.footer)[:8000],
        "canonical_url": urljoin(url, parser.canonical) if parser.canonical else "",
        "jsonld": _schemas(parser.jsonld_raw), "emails": emails,
        "phones": sorted(set(PHONE.findall(text)))[:20],
        "addresses": addresses[:20], "social_links": list(dict.fromkeys(social_links))[:30],
        "registrations": REGISTRATION.findall(text)[:15],
        "links": links[:500], "parked": bool(PARKED.search(" ".join(parser.title) + " " + text[:800])),
        "js_shell": len(text) < 250 and ("__NEXT_DATA__" in html or "id=\"root\"" in html or "enable javascript" in html.lower()),
    }


def relevant_links(page: dict, max_pages: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    for link in page.get("links", []):
        if not link["internal"]:
            continue
        path = urlsplit(link["url"]).path.lower()
        label = link["anchor"].lower()
        score = sum(3 if key in path else 1 if key in label else 0 for key in KEYWORDS)
        if score:
            scored.append((score, link["url"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return list(dict.fromkeys(url for _, url in scored))[:max_pages]
