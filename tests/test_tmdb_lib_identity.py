"""冷片绑定继承库身份：海量/热门冷片搜源绑定后仍可识别所属媒体库。

TMDB 按库识别（默认仅热门四库+海量四库）依赖 brief 上的静态库身份；
搜索聚合出的 brief 不带归属，绑定时必须从来源目录继承，否则海量库冷片
绑定后会被误判为「无归属」而排除。纯展示层数据，不涉及播放策略。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import tmdb


class InheritLibIdentityTests(unittest.TestCase):
    def test_chart_guid_inherits_chart_marker(self):
        bound = {"guid": "cg", "title": "热门片", "year": "2026"}
        meta = {"title": "热门片", "cat": "movie",
                "cover": "https://img.test/c.jpg", "rating": 8.1}
        with patch.object(appmod.chart_cache, "get_guid", return_value=meta), \
                patch.object(appmod.catalog, "find_brief_by_guid") as fb:
            appmod._inherit_lib_identity("cg", bound)
            fb.assert_not_called()
        self.assertEqual(bound["chart"], meta)
        self.assertEqual(tmdb.lib_key_of_brief(bound), "chart:movie")
        self.assertTrue(tmdb.lib_allowed(bound))

    def test_catalog_guid_inherits_agg_scope(self):
        bound = {"guid": "kg", "title": "海量冷片", "year": "2026"}
        cb = {"guid": "kg", "catalog_cat": "tv",
              "catalog_scope": "__agg__", "catalog_source": "lzi"}
        with patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=cb):
            appmod._inherit_lib_identity("kg", bound)
        self.assertEqual(bound["catalog_scope"], "__agg__")
        self.assertEqual(bound["catalog_cat"], "tv")
        self.assertEqual(bound["catalog_source"], "lzi")
        self.assertEqual(tmdb.lib_key_of_brief(bound), "catalog:tv")
        self.assertTrue(tmdb.lib_allowed(bound))

    def test_standalone_guid_inherits_but_excluded_by_default(self):
        bound = {"guid": "sg", "title": "单源库片"}
        cb = {"guid": "sg", "catalog_cat": "movie",
              "catalog_scope": "custom_abc", "catalog_source": "custom_abc"}
        with patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=cb):
            appmod._inherit_lib_identity("sg", bound)
        self.assertEqual(tmdb.lib_key_of_brief(bound), "standalone:custom_abc")
        # 默认白名单只有 chart/catalog：单源独立/自定义库先排除
        self.assertFalse(tmdb.lib_allowed(bound))

    def test_unknown_guid_adds_nothing(self):
        bound = {"guid": "ug", "title": "搜索片"}
        with patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=None):
            appmod._inherit_lib_identity("ug", bound)
        self.assertNotIn("chart", bound)
        self.assertNotIn("catalog_scope", bound)
        self.assertFalse(tmdb.lib_allowed(bound))

    def test_history_snapshot_keeps_chart_marker(self):
        import history
        brief = {"guid": "cg", "title": "热门片", "year": "2026",
                 "chart": {"cat": "tv", "title": "热门片"}}
        snap = history.build_snapshot(brief)
        self.assertEqual(snap.get("chart"), brief["chart"])
        self.assertTrue(tmdb.lib_allowed(snap))


if __name__ == "__main__":
    unittest.main()
