"""TMDB 客户端纯逻辑回归：片名归一化/搜索结果匹配/缓存键/识别库范围（不打真实 API）。"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import tmdb


class NormTests(unittest.TestCase):
    def test_norm_strips_punct(self):
        self.assertEqual(tmdb._norm("蜘蛛侠：崭新之日"), "蜘蛛侠崭新之日")
        self.assertEqual(tmdb._norm("Spider-Man: Brand New Day"),
                         "spidermanbrandnewday")

    def test_cache_key(self):
        self.assertEqual(tmdb._ck("奥德赛", "2026", False), "movie:奥德赛:2026")
        self.assertEqual(tmdb._ck("奥德赛", "2026", True), "tv:奥德赛:2026")


class PickTests(unittest.TestCase):
    def test_pick_exact_title_year(self):
        results = [
            {"id": 1, "title": "奥德赛", "release_date": "2016-10-12",
             "popularity": 10},
            {"id": 2, "title": "奥德赛", "release_date": "2026-07-15",
             "popularity": 50},
        ]
        best = tmdb._pick(results, "奥德赛", "2026", False)
        self.assertEqual(best["id"], 2)

    def test_pick_rejects_unrelated(self):
        results = [{"id": 9, "title": "完全不同的片", "release_date": "2020-01-01",
                    "popularity": 999}]
        self.assertIsNone(tmdb._pick(results, "奥德赛", "2026", False))

    def test_pick_empty(self):
        self.assertIsNone(tmdb._pick([], "奥德赛", "2026", False))

    def test_img_url(self):
        self.assertEqual(tmdb.img("/abc.jpg", "w500"),
                         "https://image.tmdb.org/t/p/w500/abc.jpg")
        self.assertEqual(tmdb.img(""), "")


class LibScopeTests(unittest.TestCase):
    """识别范围：默认只认热门四库(chart)+海量四库(catalog)，其余库先排除。"""

    def test_key_chart_marker(self):
        b = {"guid": "x", "chart": {"cat": "movie", "title": "T"}}
        self.assertEqual(tmdb.lib_key_of_brief(b), "chart:movie")

    def test_key_catalog_agg(self):
        b = {"guid": "x", "catalog_scope": "__agg__", "catalog_cat": "tv"}
        self.assertEqual(tmdb.lib_key_of_brief(b), "catalog:tv")
        # 无 cat 时退回库类型键
        self.assertEqual(tmdb.lib_key_of_brief(
            {"guid": "x", "catalog_scope": "__agg__"}), "catalog")

    def test_key_standalone_excluded_identity(self):
        b = {"guid": "x", "catalog_scope": "custom_abc", "catalog_cat": "movie"}
        self.assertEqual(tmdb.lib_key_of_brief(b), "standalone:custom_abc")

    def test_key_t4(self):
        self.assertEqual(tmdb.lib_key_of_brief(
            {"guid": "x", "locator": {"kind": "t4"}}), "t4")

    def test_key_unknown(self):
        self.assertIsNone(tmdb.lib_key_of_brief({"guid": "x", "title": "T"}))
        self.assertIsNone(tmdb.lib_key_of_brief(None))
        self.assertIsNone(tmdb.lib_key_of_brief({}))

    def test_key_chart_guid_fallback(self):
        """旧绑定/历史快照无 chart 标记时，用榜单缓存索引认 guid。"""
        fake = types.ModuleType("charts")

        class _CC:
            def is_chart_guid(self, g):
                return g == "chartguid"

            def cat_of(self, g):
                return "tv"

        fake.chart_cache = _CC()
        with mock.patch.dict(sys.modules, {"charts": fake}):
            self.assertEqual(tmdb.lib_key_of_brief({"guid": "chartguid"}),
                             "chart:tv")
            self.assertIsNone(tmdb.lib_key_of_brief({"guid": "other"}))

    def test_default_whitelist(self):
        with mock.patch.object(tmdb, "_lib_whitelist",
                               return_value=["chart", "catalog"]):
            self.assertTrue(tmdb.lib_allowed({"chart": {"cat": "movie"}}))
            self.assertTrue(tmdb.lib_allowed(
                {"catalog_scope": "__agg__", "catalog_cat": "tv"}))
            # 单源独立/自定义库、T4、无归属（搜索/本地）一律排除
            self.assertFalse(tmdb.lib_allowed({"catalog_scope": "custom_abc"}))
            self.assertFalse(tmdb.lib_allowed({"locator": {"kind": "t4"}}))
            self.assertFalse(tmdb.lib_allowed({"guid": "x", "title": "T"}))

    def test_whitelist_specific_key(self):
        """后期按库开关：具体库键优先于库类型。"""
        with mock.patch.object(tmdb, "_lib_whitelist",
                               return_value=["standalone:custom_abc"]):
            self.assertTrue(tmdb.lib_allowed({"catalog_scope": "custom_abc"}))
            self.assertFalse(tmdb.lib_allowed(
                {"catalog_scope": "__agg__", "catalog_cat": "movie"}))
            self.assertFalse(tmdb.lib_allowed({"chart": {"cat": "movie"}}))

    def test_whitelist_empty_disables_all(self):
        with mock.patch.object(tmdb, "_lib_whitelist", return_value=[]):
            self.assertFalse(tmdb.lib_allowed({"chart": {"cat": "movie"}}))
            self.assertFalse(tmdb.lib_allowed(
                {"catalog_scope": "__agg__", "catalog_cat": "tv"}))

    def test_enrich_out_of_scope_no_fetch_no_cache(self):
        """范围外：不触发后台富化、不读缓存，直接回退资源站数据。"""
        with mock.patch.object(tmdb, "prefetch") as pf, \
                mock.patch.object(tmdb, "lookup") as lk, \
                mock.patch.object(tmdb, "_lib_whitelist",
                                  return_value=["chart", "catalog"]):
            b = {"guid": "x", "title": "T", "year": "2026",
                 "catalog_scope": "custom_abc"}
            self.assertIsNone(tmdb.enrich(b))
            pf.assert_not_called()
            lk.assert_not_called()

    def test_enrich_in_scope_fetches(self):
        with mock.patch.object(tmdb, "prefetch") as pf, \
                mock.patch.object(tmdb, "lookup",
                                  return_value={"tmdb_id": 1}) as lk, \
                mock.patch.object(tmdb, "_lib_whitelist",
                                  return_value=["chart", "catalog"]):
            b = {"guid": "x", "title": "T", "year": "2026",
                 "catalog_scope": "__agg__", "catalog_cat": "movie"}
            self.assertEqual(tmdb.enrich(b), {"tmdb_id": 1})
            pf.assert_called_once()
            lk.assert_called_once()


class MetadataTypeTests(unittest.TestCase):
    def test_variety_snapshot_single_episode_is_tv(self):
        # 历史快照只有当前一集，也不能把综艺误判为电影
        b = {"type_name": "大陆综艺", "catalog_scope": "__agg__",
             "lines": {"lzi": [{}]}}
        self.assertTrue(tmdb._is_tv_brief(b))

    def test_chart_category_wins(self):
        self.assertTrue(tmdb._is_tv_brief({"chart": {"cat": "variety"}}))
        self.assertFalse(tmdb._is_tv_brief({"chart": {"cat": "movie"}}))

    def test_animation_movie_not_tv(self):
        self.assertFalse(tmdb._is_tv_brief({"type_name": "动画电影"}))

    def test_drama_name_is_tv(self):
        self.assertTrue(tmdb._is_tv_brief({"type_name": "大陆剧"}))

    def test_clean_title(self):
        self.assertEqual(tmdb._clean_title("【在线】奥德赛（2025） ·正片"), "奥德赛")
        self.assertEqual(tmdb._clean_title("蜘蛛侠：崭新之日普通话版 ·正片"),
                         "蜘蛛侠：崭新之日")
        self.assertEqual(tmdb._clean_title("生化危机：爆发夜"), "生化危机：爆发夜")


if __name__ == "__main__":
    unittest.main()
