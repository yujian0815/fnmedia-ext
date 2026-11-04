"""Offline module contracts; no production services or credentials."""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import providers
import t4
import tvbox

SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}


class T4ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_source_list_does_not_enable_defaults(self):
        with patch.object(providers, "search_source", new_callable=AsyncMock) as search:
            self.assertEqual(await providers.search_all("Test", []), [])
            search.assert_not_awaited()


    async def test_bounded_search(self):
        active = high = 0

        async def search(*_):
            nonlocal active, high
            active += 1
            high = max(high, active)
            await asyncio.sleep(0.001)
            active -= 1
            return []

        with patch.object(providers, "search_source", side_effect=search):
            await providers.search_all("Test", [dict(SOURCE, id=str(i)) for i in range(35)])
        self.assertLessEqual(high, 8)


    async def test_search_budget_keeps_fast_results_and_cancels_slow(self):
        cancelled = asyncio.Event()

        async def search(_client, source, _keyword):
            if source["id"] == "fast":
                return [t4.map_title({"vod_id": "1", "vod_name": "Fast"}, source)]
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with patch.object(providers, "search_source", side_effect=search):
            results = await providers.search_all("Test", [
                dict(SOURCE, id="fast"), dict(SOURCE, id="slow")], budget=0.02)
        self.assertEqual([t.title for t in results], ["Fast"])
        self.assertTrue(cancelled.is_set())


    async def test_mixed_search_does_not_starve_either_source_type(self):
        started = []

        async def search(_client, source, _keyword):
            started.append(source["id"])
            await asyncio.sleep(10)

        sources = [dict(SOURCE, id=f"cms-{i}", kind="cms") for i in range(12)]
        sources += [dict(SOURCE, id=f"t4-{i}") for i in range(12)]
        with patch.object(providers, "search_source", side_effect=search):
            await providers.search_all("Test", sources, budget=0.02)
        self.assertEqual(len(started), 8)
        self.assertEqual(sum(sid.startswith("t4-") for sid in started), 4)
        self.assertEqual(sum(sid.startswith("cms-") for sid in started), 4)


    async def test_remote_subscription_wrapper_and_relative_api(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def client():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"sites": [
                        {"type": 4, "name": "Relative", "api": "../api/demo"}]}))) as value:
                yield value

        with patch("subfetch.async_sub_client", client):
            sites, subs = await tvbox.load_remote("https://t4.test/t4/config.json")
        self.assertEqual(sites[0]["api"], "https://t4.test/api/demo")
        self.assertEqual(subs, 0)
