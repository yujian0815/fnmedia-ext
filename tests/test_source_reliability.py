"""XML 接口保留、探活回退与小众源健康判定。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import providers
import tvbox


class SourceReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_xml_api_is_not_rewritten_to_json(self):
        status, normalized = tvbox.classify_site({
            "name": "XML源", "type": 1,
            "api": "https://example.com/provide/vod/at/xml?token=abc"})
        self.assertEqual(status, "add_xml")
        self.assertEqual(normalized["api"],
                         "https://example.com/provide/vod/at/xml?token=abc")

    async def test_probe_list_fallback_parses_xml(self):
        xml = """<?xml version='1.0' encoding='utf-8'?><rss><list><video>
          <id>1</id><name>测试影片</name><type>电影</type><year>2026</year>
          <play_url>https://cdn.test/v.m3u8</play_url></video></list></rss>"""
        detail = type("Resp", (), {"text": "<rss></rss>", "status_code": 200})()
        listing = type("Resp", (), {"text": xml, "status_code": 200})()
        client = AsyncMock()
        client.get.side_effect = [detail, listing]
        with patch.object(providers.netutil, "async_client") as factory:
            factory.return_value.__aenter__.return_value = client
            # 第一次 detail 无结果，第二次 ac=list 返回 XML 合法结构。
            detail.json = lambda: {"code": 1, "list": []}
            result = await providers.probe_source(
                "https://example.com/provide/vod/at/xml", keyword="不存在")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertIn("接口正常", result["error"])

    async def test_niche_valid_source_is_not_marked_dead(self):
        with patch.object(providers, "probe_source",
                          new=AsyncMock(return_value={
                              "ok": True, "status": 200, "elapsed_ms": 20,
                              "count": 0, "sample": "", "error": "接口正常（该词无结果）"})):
            result = await providers.probe_source_alive({"api": "https://cms.test"})
        self.assertIsNone(result["alive"])
        self.assertTrue(result["reachable"])


if __name__ == "__main__":
    unittest.main()
