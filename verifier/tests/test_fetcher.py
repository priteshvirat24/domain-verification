import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from verifier.config import Config
from verifier.fetcher import TieredFetcher


class _Response:
    def __init__(self, status, url='https://example.test/', body=b'<h1>Example</h1>'):
        self.status = status
        self.url = url
        self.body = body
        self.encoding = 'utf-8'
        self.history = []


class FetcherTests(unittest.TestCase):
    def _module(self, statuses):
        calls = []

        class AsyncFetcher:
            @staticmethod
            async def get(url, **kwargs):
                calls.append(('http', url))
                return _Response(statuses.pop(0), url)

        class DynamicFetcher:
            @staticmethod
            async def async_fetch(url, **kwargs):
                calls.append(('dynamic', url))
                return _Response(200, url, b'<h1>Rendered</h1>')

        class StealthyFetcher:
            @staticmethod
            async def async_fetch(url, **kwargs):
                calls.append(('stealth', url))
                return _Response(200, url, b'<h1>Protected</h1>')

        return types.SimpleNamespace(AsyncFetcher=AsyncFetcher, DynamicFetcher=DynamicFetcher,
                                     StealthyFetcher=StealthyFetcher), calls

    def test_429_retries_but_is_bounded(self):
        module, calls = self._module([429, 200])
        cfg = Config(per_host_delay_seconds=0, max_http_attempts=2, respect_robots=False)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'),
                                      'scrapling.fetchers': module}):
            result = asyncio.run(TieredFetcher(cfg).fetch('https://example.test/'))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(calls), 2)

    def test_browser_only_on_explicit_escalation(self):
        module, calls = self._module([200, 200])
        cfg = Config(per_host_delay_seconds=0, max_http_attempts=1,
                     respect_robots=False, use_dynamic=True)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'),
                                      'scrapling.fetchers': module}):
            fetcher = TieredFetcher(cfg)
            normal = asyncio.run(fetcher.fetch('https://example.test/'))
            rendered = asyncio.run(fetcher.fetch('https://example.test/', allow_browser=True,
                                                 force_browser=True))
        self.assertEqual(normal.method, 'HTTP')
        self.assertEqual(rendered.method, 'DYNAMIC')
        self.assertEqual([kind for kind, _ in calls], ['http', 'http', 'dynamic'])


if __name__ == '__main__':
    unittest.main()
