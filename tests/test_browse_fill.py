"""浏览海量/独立库首屏的后台补线开关回归。

默认关闭 browse_fill_lines_enabled：warm_catalog_briefs 只落库 + 轻量预热，
不再对首屏多部片排队跨源补线（避免抢占事件循环拖慢二级线路墙）；显式开启后
才恢复 start_fill_lines 调用。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


def _brief(g="og1"):
    return {"guid": g, "title": "测试片", "year": "2026",
            "type_name": "电视剧", "lines": {"lzi::lzm3u8": [
                {"title": "第01集", "url": "https://x.test/1.m3u8", "direct": True}]}}


class BrowseFillTests(unittest.TestCase):
    def test_disabled_does_not_start_fill_lines(self):
        calls = []
        with patch.object(appmod.config, "get",
                          return_value={"linepredict_enabled": True,
                                        "browse_fill_lines_enabled": False}), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.store, "remember_merged") as rem, \
                patch.object(appmod, "start_fill_lines",
                             side_effect=lambda g: calls.append(g)), \
                patch.object(appmod, "schedule_prewarm_title"):
            appmod.warm_catalog_briefs([_brief()])
        rem.assert_called_once()
        self.assertEqual(calls, [])  # 默认不补线

    def test_enabled_starts_fill_lines(self):
        calls = []
        with patch.object(appmod.config, "get",
                          return_value={"linepredict_enabled": True,
                                        "browse_fill_lines_enabled": True}), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.store, "remember_merged"), \
                patch.object(appmod, "start_fill_lines",
                             side_effect=lambda g: calls.append(g)), \
                patch.object(appmod, "schedule_prewarm_title"):
            appmod.warm_catalog_briefs([_brief()])
        self.assertEqual(calls, ["og1"])  # 显式开启才补线

    def test_linepredict_disabled_skips_whole_warm(self):
        with patch.object(appmod.config, "get",
                          return_value={"linepredict_enabled": False}), \
                patch.object(appmod, "start_fill_lines") as fill, \
                patch.object(appmod.store, "remember_merged") as rem:
            appmod.warm_catalog_briefs([_brief()])
        fill.assert_not_called()
        rem.assert_not_called()


if __name__ == "__main__":
    unittest.main()
