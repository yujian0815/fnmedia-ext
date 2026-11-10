"""海量分类聚合库首页并发竞速回归。

旧实现跨源串行回退：首个源超时时整页要等满，TV 端首屏 item/list 卡 4~6s
触发“加载失败”。改为所有源并发、最快有效结果胜出、取消其余慢源。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog as catalogmod  # noqa: E402


class CatalogAggRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.slow_cancelled = asyncio.Event()

        async def fake_fetch(client, source, type_id, page, timeout):
            if source["id"] == "slow":
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    self.slow_cancelled.set()
                    raise
                return ([{"id": 1}], 1)
            await asyncio.sleep(0.05)
            return ([{"id": 2}], 1)

        self.fake_fetch = fake_fetch

    async def test_fast_source_wins_without_waiting_slow(self):
        c = catalogmod.catalog
        routes = [
            {"source_id": "slow", "type_id": 1},
            {"source_id": "fast", "type_id": 1},
        ]

        def briefs(vods, source, route, scope, cat):
            return [{"guid": f"g-{source['id']}", "title": "片",
                     "lines": {"a": [1]}}]

        loop = asyncio.get_event_loop()
        scope_key = c._scope_key(catalogmod.AGG_SCOPE, "movie")
        with patch.object(c, "_routes", {scope_key: routes}), \
                patch.object(c, "_source_by_id",
                             side_effect=lambda sid: {"id": sid}), \
                patch.object(c, "_briefs_from_vods", side_effect=briefs), \
                patch.object(catalogmod, "_fetch_category_page",
                             new=AsyncMock(side_effect=self.fake_fetch)), \
                patch("store.store.remember_merged",
                      side_effect=lambda rows: rows):
            t0 = loop.time()
            out = await c._load_page(catalogmod.AGG_SCOPE, "movie", 1)
            elapsed = loop.time() - t0
        # 快速源结果在列，且整体远小于慢源的 5s
        self.assertTrue(any(r.get("guid") == "g-fast" for r in out))
        self.assertLess(elapsed, 1.0)
        await asyncio.sleep(0.05)
        self.assertTrue(self.slow_cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
