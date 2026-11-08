"""网络瞬断容错：海报短重试、搜索超时重试、绑定绕过负缓存。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


class RetryClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers=None):
        self.calls += 1
        return self.response


class NetworkResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_poster_network_failure_uses_fresh_client_once(self):
        req = Request({"type": "http", "method": "GET", "path": "/online/x.jpg",
                       "headers": [], "app": appmod.app})
        original = b"\xff\xd8\xff\xe0poster"
        retry = RetryClient(httpx.Response(
            200, content=original, headers={"Content-Type": "image/jpeg"}))
        with patch.object(appmod, "_poster_http", side_effect=httpx.ConnectTimeout("timeout")), \
                patch.object(appmod.netutil, "async_client", return_value=retry):
            response = await appmod._poster_fetch(
                req, "https://img3.doubanio.com/poster.jpg", {}, "img3.doubanio.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, original)
        self.assertEqual(retry.calls, 1)

    async def test_search_timeout_retries_once_and_keeps_results(self):
        req = Request({"type": "http", "method": "GET", "path": "/v/api/v1/search/list",
                       "query_string": "q=家弑服务".encode(), "headers": [], "app": appmod.app})
        item = {"guid": "matched", "title": "家弑服务", "media_guid": "m1"}
        search_mock = AsyncMock(side_effect=[TimeoutError(), [item]])
        with patch.object(appmod, "forward_upstream", new=AsyncMock(return_value=appmod.Response(
                content='{"code":0,"data":[]}', media_type="application/json"))), \
                patch.object(appmod, "online_search", new=search_mock), \
                patch.object(appmod.vlibs, "account_online_allowed", return_value=True), \
                patch.object(appmod.clientmod, "normalize_cards", side_effect=lambda rows, _client: rows):
            response = await appmod.search_list(req)
        data = response.body and __import__("json").loads(response.body)["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "家弑服务")
        self.assertEqual(search_mock.await_count, 2)

    async def test_blocking_source_uses_dedicated_executor(self):
        name = await appmod.netutil.run_blocking_source(
            lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("blocking-source"), name)

    async def test_refresh_skips_empty_aggregate_cache(self):
        key = "家弑服务\x001\x000"
        appmod._AGG_CACHE[key] = (appmod.time.time(), [])
        appmod._AGG_INFLIGHT.pop(key, None)
        expected = [{"guid": "bound", "title": "家弑服务", "lines": {}}]
        with patch.object(appmod, "__aggregate_briefs_inner", new=AsyncMock(return_value=expected)) as inner, \
                patch.object(appmod.store, "remember_merged", side_effect=lambda rows: rows):
            cached = await appmod._aggregate_briefs("家弑服务", fast_only=True, include_t4=False)
            fresh = await appmod._aggregate_briefs(
                "家弑服务", fast_only=True, include_t4=False, refresh=True)
        self.assertEqual(cached, [])
        self.assertEqual(fresh, expected)
        self.assertEqual(inner.await_count, 1)


if __name__ == "__main__":
    unittest.main()
