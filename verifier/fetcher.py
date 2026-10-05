"""Bounded Scrapling HTTP -> dynamic -> stealth fetching."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .config import Config
from .models import FetchRecord

LOG = logging.getLogger(__name__)
USER_AGENT = "DomainVerificationAuditor/2.0 (+https://github.com/priteshvirat24/domain-verification)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_type(exc: Exception) -> str:
    message = str(exc).lower()
    if any(x in message for x in ("certificate", "ssl", "tls")):
        return "TLS"
    if any(x in message for x in ("timeout", "timed out")):
        return "TIMEOUT"
    if any(x in message for x in ("resolve", "dns", "name or service", "nodename")):
        return "DNS"
    return type(exc).__name__.upper()


async def dns_status(host: str) -> str:
    for attempt in range(2):
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
            if any(not ip_address(a[4][0]).is_global for a in addresses):
                return "UNSAFE_ADDRESS"
            return "RESOLVED"
        except socket.gaierror as exc:
            if exc.errno == socket.EAI_NONAME:
                return "NXDOMAIN"
            if attempt == 0:
                await asyncio.sleep(0.2)
        except Exception:
            return "UNKNOWN"
    return "FAILED"


class TieredFetcher:
    def __init__(self, config: Config, proxy_url: str | None = None):
        self.config = config
        self.proxy_url = proxy_url if config.use_proxy else None
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._semaphore = asyncio.Semaphore(config.concurrency)

    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            wait = self.config.per_host_delay_seconds - (now - self._last_request.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = asyncio.get_running_loop().time()

    async def _http(self, url: str) -> FetchRecord:
        host = urlsplit(url).hostname or ""
        result = FetchRecord(requested_url=url, checked_at=utc_now())
        for attempt in range(1, self.config.max_http_attempts + 1):
            result.attempts = attempt
            try:
                await self._pace(host)
                async with self._semaphore:
                    try:
                        from scrapling.fetchers import AsyncFetcher
                        kwargs = dict(timeout=self.config.timeout_seconds, retries=0,
                                      follow_redirects="safe", max_redirects=self.config.max_redirects,
                                      headers={"User-Agent": USER_AGENT}, verify=True)
                        response = await AsyncFetcher.get(url, **kwargs)
                        result.status = int(response.status)
                        result.final_url = str(response.url)
                        result.redirect_chain = [str(x.url) for x in response.history] + [result.final_url]
                        result.html = response.body[:self.config.max_body_bytes].decode(response.encoding or "utf-8", "replace")
                    except (ImportError, Exception):
                        import httpx
                        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=True, verify=False) as client:
                            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
                            result.status = int(resp.status_code)
                            result.final_url = str(resp.url)
                            result.redirect_chain = [str(r.url) for r in resp.history] + [result.final_url]
                            result.html = resp.text[:self.config.max_body_bytes]
                if result.status in (429, 500, 502, 503, 504) and attempt < self.config.max_http_attempts:
                    await asyncio.sleep(min(5, 2 ** (attempt - 1)) + random.random())
                    continue
                return result
            except Exception as exc:
                result.error = str(exc)[:400]
                result.error_type = _error_type(exc)
                LOG.warning("HTTP %s attempt %s: %s", host, attempt, result.error)
                if result.error_type in ("TIMEOUT", "DNS"):
                    break
                if attempt < self.config.max_http_attempts:
                    await asyncio.sleep(min(5, 2 ** (attempt - 1)) + random.random())
        return result

    async def robots_allowed(self, url: str) -> bool | None:
        if not self.config.respect_robots:
            return True
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host not in self._robots:
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            try:
                from scrapling.fetchers import AsyncFetcher
                kwargs = dict(timeout=min(4, self.config.timeout_seconds), retries=0,
                              follow_redirects="safe", max_redirects=3,
                              headers={"User-Agent": USER_AGENT}, verify=True)
                async with self._semaphore:
                    resp = await AsyncFetcher.get(robots_url, **kwargs)
                if int(resp.status) == 200:
                    robot = RobotFileParser()
                    body = resp.body[:100_000].decode(resp.encoding or "utf-8", "replace")
                    robot.parse(body.splitlines())
                    self._robots[host] = robot
                else:
                    self._robots[host] = None
            except Exception:
                self._robots[host] = None
        robot = self._robots[host]
        return robot.can_fetch(USER_AGENT, url) if robot else None

    async def fetch(self, url: str, *, allow_browser: bool = False,
                    use_stealth: bool = False, force_browser: bool = False) -> FetchRecord:
        permission = await self.robots_allowed(url)
        if permission is False:
            return FetchRecord(requested_url=url, error="robots.txt disallows fetch",
                               error_type="ROBOTS", checked_at=utc_now())
        result = await self._http(url)
        if result.status == 0 and url.startswith("https://") and result.error_type in ("TLS", "SSLError"):
            fallback = await self._http("http://" + url.removeprefix("https://"))
            fallback.attempts += result.attempts
            if fallback.status:
                fallback.requested_url = url
                result = fallback
        if not allow_browser or not (self.config.use_dynamic or self.config.use_stealth):
            return result
        if not force_browser and result.status not in (0, 403, 429) and len(result.html) > 1200:
            return result
        if result.status in (403, 429) and not use_stealth:
            return result
        method = "STEALTH" if use_stealth and self.config.use_stealth else "DYNAMIC"
        if method == "DYNAMIC" and not self.config.use_dynamic:
            return result
        try:
            from scrapling.fetchers import DynamicFetcher, StealthyFetcher
            fetcher = StealthyFetcher if method == "STEALTH" else DynamicFetcher
            kwargs = {"timeout": self.config.timeout_seconds * 1000, "disable_resources": True}
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            async with self._semaphore:
                page = await fetcher.async_fetch(url, **kwargs)
            return FetchRecord(requested_url=url, final_url=str(page.url), status=int(page.status),
                               html=page.body[:self.config.max_body_bytes].decode(page.encoding or "utf-8", "replace"),
                               redirect_chain=[str(x.url) for x in page.history] + [str(page.url)],
                               method=method, attempts=result.attempts + 1, checked_at=utc_now())
        except Exception as exc:
            result.error = (result.error + "; " + str(exc))[:400]
            result.error_type = _error_type(exc)
            result.attempts += 1
            return result
