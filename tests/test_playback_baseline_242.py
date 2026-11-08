"""v0.2.42 起播基线回归：宽松同名绑定、去广告不读分片、无后台补线。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import adfilter


class PlaybackBaseline242Tests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_binding_accepts_source_language_suffix(self):
        chart = {"title": "家弑服务", "cat": "movie", "cover": "", "rating": 7.1}
        source = {"guid": "source-guid", "title": "家弑服务（普通话）",
                  "year": "2025", "lines": {"lzi::lzm3u8": []}}
        remember = Mock(side_effect=lambda rows: rows)
        with patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.chart_cache, "get_guid", return_value=chart), \
                patch.object(appmod, "_aggregate_briefs", new=AsyncMock(return_value=[source])), \
                patch.object(appmod.store, "remember_merged", new=remember):
            result = await appmod.resolve_chart("chart-guid", force=True)
        self.assertIsNotNone(result)
        stored = remember.call_args.args[0][0]
        self.assertEqual(stored["guid"], "chart-guid")
        self.assertEqual(stored["title"], "家弑服务（普通话）")
        self.assertEqual(stored["chart"]["title"], "家弑服务")

    async def test_chart_binding_uses_fast_sources_then_full_fallback(self):
        chart = {"title": "激情邀约", "cat": "movie", "cover": "", "rating": 7.3}
        source = {"guid": "source-guid", "title": "激情邀约", "year": "2026", "lines": {}}
        search = AsyncMock(side_effect=[[], [source]])
        remember = Mock(side_effect=lambda rows: rows)
        with patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.chart_cache, "get_guid", return_value=chart), \
                patch.object(appmod, "_aggregate_briefs", new=search), \
                patch.object(appmod.store, "remember_merged", new=remember):
            result = await appmod.resolve_chart("chart-guid", force=True)
        self.assertIsNotNone(result)
        self.assertEqual(search.await_count, 2)
        self.assertTrue(search.call_args_list[0].kwargs["fast_only"])
        self.assertNotIn("fast_only", search.call_args_list[1].kwargs)
        self.assertEqual(remember.call_args.args[0][0]["title"], "激情邀约")

    def test_chart_binding_rejects_similar_title_and_prioritizes_exact(self):
        chart = {"title": "突如其来", "year": "2026"}
        self.assertEqual(appmod._chart_title_rank(chart, {
            "title": "突如其来的假期", "year": "2021"}), 99)
        self.assertEqual(appmod._chart_title_rank(chart, {
            "title": "突如其来的假期", "year": "2026"}), 99)
        self.assertEqual(appmod._chart_title_rank(chart, {
            "title": "突如其来（普通话）", "year": "2026"}), 1)
        self.assertEqual(appmod._chart_title_rank(chart, {
            "title": "突如其来", "year": "2026"}), 0)

    async def test_playlist_ads_use_sync_rules_without_sps_network_probe(self):
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                "#EXTINF:4,\na.ts\n#EXTINF:4,\nb.ts\n#EXT-X-ENDLIST\n")
        picked = {"online_guid": "og", "line": "lzi::lzm3u8", "index": 0,
                  "url": "https://media.test/list.m3u8", "headers": {}}
        req = Request({"type": "http", "method": "GET", "path": "/online/mg/main.m3u8",
                       "headers": [], "app": appmod.app})
        req.state._play_mg = "mg"
        async_filter = AsyncMock(side_effect=AssertionError("起播不应读取SPS分片"))
        with patch.object(appmod, "_fetch_playlist", new=AsyncMock(
                return_value=(text, picked["url"]))), \
                patch.object(adfilter, "enabled", return_value=True), \
                patch.object(adfilter, "filter_hls", new=Mock(return_value=(text, 0))), \
                patch.object(adfilter, "filter_hls_async", new=async_filter):
            body, _ = await appmod._build_playlist_body("mg", picked, req)
        self.assertIn("/v/media/online/mg/seg?", body)
        async_filter.assert_not_awaited()

    async def test_app_default_line_does_not_start_race(self):
        picked = {"online_guid": "og", "line": "lzi::lzm3u8", "index": 0,
                  "url": "https://media.test/list.m3u8", "title": "测试片"}
        req = Request({"type": "http", "method": "GET", "path": "/online/mg/main.m3u8",
                       "query_string": b"index=0&line=lzi%3A%3Alzm3u8&auto=1",
                       "headers": [], "app": appmod.app})
        ensure = AsyncMock(return_value=picked)
        body = "#EXTM3U\n#EXTINF:4,\na.ts\n"
        with patch.object(appmod, "_await_pending_chart", new=AsyncMock(return_value="")), \
                patch.object(appmod, "_pick_episode", return_value=picked), \
                patch.object(appmod, "_dodge_known_bad", return_value=picked), \
                patch.object(appmod, "_autofail_on", return_value=True), \
                patch.object(appmod, "_ensure_playable", new=ensure), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_get_playlist_cached", new=AsyncMock(return_value=body)), \
                patch.object(appmod, "_verify_rewritten_first_seg", new=AsyncMock(return_value=True)), \
                patch.object(appmod, "_is_slow_line", return_value=False):
            response = await appmod.hls_main("mg", req)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ensure.await_args.kwargs["auto"], False)

    async def test_no_background_fill_or_prewarm_tasks(self):
        self.assertIsNone(appmod.start_fill_lines("og"))
        self.assertIsNone(appmod.schedule_prewarm_title("og", 0))


if __name__ == "__main__":
    unittest.main()
