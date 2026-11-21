"""海量四库成人分类隔离回归。

普通 CMS 的“伦理片”曾因以“片”结尾被误归为 movie；扩展源也只能进入
自己的独立库，不能通过普通分类或旧路由进入跨源聚合。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog
import providers


class AdultCategoryTests(unittest.TestCase):
    def test_regular_sources_never_classify_adult_categories_as_movies(self):
        for name in ("伦理片", "伦理电影", "情色片", "成人影片", "三级片",
                     "限制级电影", "风月片", "福利片", "写真", "午夜场"):
            with self.subTest(name=name):
                self.assertTrue(catalog._is_adult_category(name))
                self.assertIsNone(catalog.classify(name))
                self.assertIsNone(catalog.classify_source_class(name, False))

    def test_authorized_standalone_source_uses_adult_cat_only_in_full_build(self):
        with patch.object(catalog, "_adult_on", return_value=True):
            self.assertEqual(
                catalog.classify_source_class("伦理片", True), catalog.ADULT_CAT)
            self.assertEqual(
                catalog.classify_source_class("福利", True), catalog.ADULT_CAT)
        with patch.object(catalog, "_adult_on", return_value=False):
            self.assertIsNone(catalog.classify_source_class("伦理片", True))

    def test_regular_categories_are_unchanged(self):
        expected = {
            "动作片": "movie", "喜剧片": "movie", "纪录片": "movie",
            "国产剧": "tv", "连续剧": "tv", "动漫": "anime", "综艺": "variety",
        }
        for name, cat in expected.items():
            with self.subTest(name=name):
                self.assertFalse(catalog._is_adult_category(name))
                self.assertEqual(catalog.classify_source_class(name, False), cat)
                with patch.object(catalog, "_adult_on", return_value=True):
                    self.assertEqual(catalog.classify_source_class(name, True), cat)

    def test_hard_redline_still_wins_over_adult_authorization(self):
        with patch.object(catalog, "_adult_on", return_value=True):
            self.assertIsNone(catalog.classify_source_class("AI换脸", True))


class AggregateBriefFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity_patch = patch.object(
            catalog, "IDENTITY_STATE", Path(self.tmp.name) / "identity.json")
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.c = catalog.Catalog()
        self.source = {"id": "regular", "api": "https://cms.test/api.php/provide/vod/at/json"}
        self.episode = providers.Episode("正片", "https://cdn.test/v.m3u8", direct=True)

    def _title(self, vod_id: str, type_name: str, provider: str = "regular"):
        return providers.OnlineTitle(
            provider=provider, vod_id=vod_id, title=f"测试影片{vod_id}",
            type_name=type_name, year="2026", area="", updated="",
            remarks="正片", score="", poster="", overview="", actor="",
            director="", sources={"线路": [self.episode]})

    def test_adult_source_is_rejected_even_with_regular_route(self):
        source = dict(self.source, adult=True)
        briefs = self.c._briefs_from_vods(
            [{"id": "1"}], source,
            {"source_id": "regular", "type_id": 1, "type_name": "动作片"},
            catalog.AGG_SCOPE, "movie")
        self.assertEqual(briefs, [])

    def test_adult_route_is_rejected_before_mapping_vods(self):
        with patch.object(catalog.providers, "_map_vod") as mapper:
            briefs = self.c._briefs_from_vods(
                [{"id": "1"}], self.source,
                {"source_id": "regular", "type_id": 25, "type_name": "伦理片"},
                catalog.AGG_SCOPE, "movie")
        self.assertEqual(briefs, [])
        mapper.assert_not_called()

    def test_item_type_is_a_second_line_of_defense(self):
        titles = [self._title("adult", "伦理片"), self._title("normal", "动作片")]
        with patch.object(catalog.providers, "_map_vod", side_effect=titles):
            briefs = self.c._briefs_from_vods(
                [{"id": "adult"}, {"id": "normal"}], self.source,
                {"source_id": "regular", "type_id": 1, "type_name": "动作片"},
                catalog.AGG_SCOPE, "movie")
        self.assertEqual([b["title"] for b in briefs], ["测试影片normal"])

    def test_authorized_standalone_adult_item_is_not_deleted(self):
        source = dict(self.source, adult=True, loose_filter=True)
        with patch.object(catalog.providers, "_map_vod",
                          return_value=self._title("adult", "伦理片")):
            briefs = self.c._briefs_from_vods(
                [{"id": "adult"}], source,
                {"source_id": "regular", "type_id": 25, "type_name": "伦理片"},
                "regular", catalog.ADULT_CAT)
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["catalog_scope"], "regular")
        self.assertEqual(briefs[0]["catalog_cat"], catalog.ADULT_CAT)


class AggregateRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_exclude_adult_categories_and_adult_sources(self):
        regular = {"id": "regular", "kind": "cms",
                   "api": "https://regular.test/api.php/provide/vod/at/json"}
        adult = {"id": "adult", "kind": "cms", "adult": True, "standalone": True,
                 "api": "https://adult.test/api.php/provide/vod/at/json"}
        by_id = {"regular": regular, "adult": adult}
        classes = {
            "regular": [
                {"type_id": 1, "type_pid": 0, "type_name": "动作片"},
                {"type_id": 25, "type_pid": 0, "type_name": "伦理片"},
            ],
            "adult": [
                {"type_id": 1, "type_pid": 0, "type_name": "动作片"},
                {"type_id": 25, "type_pid": 0, "type_name": "伦理片"},
            ],
        }

        async def class_rows(_client, source):
            return classes[source["id"]]

        async def page_rows(_client, _source, _type_id, _page, timeout=0):
            return [{"vod_id": 1}], 10

        with tempfile.TemporaryDirectory() as td, \
                patch.object(catalog, "IDENTITY_STATE", Path(td) / "identity.json"), \
                patch.object(catalog, "_adult_on", return_value=True), \
                patch.object(catalog, "_fetch_class_list", side_effect=class_rows), \
                patch.object(catalog, "_fetch_category_page", side_effect=page_rows):
            c = catalog.Catalog()
            with patch.object(c, "_source_by_id", side_effect=by_id.get), \
                    patch.object(c, "standalone_scopes", return_value=["adult"]), \
                    patch.object(c, "warm_standalone", new=AsyncMock()):
                await c.refresh_routes([regular, adult])

        aggregate = c._routes[c._scope_key(catalog.AGG_SCOPE, "movie")]
        self.assertEqual([(r["source_id"], r["type_id"]) for r in aggregate],
                         [("regular", 1)])
        self.assertFalse(any(r["type_name"] == "伦理片" for r in aggregate))
        standalone_movie = c._routes[c._scope_key("adult", "movie")]
        standalone_adult = c._routes[c._scope_key("adult", catalog.ADULT_CAT)]
        self.assertEqual([r["type_id"] for r in standalone_movie], [1])
        self.assertEqual([r["type_id"] for r in standalone_adult], [25])


class AdjacentSurfaceFilterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _title(vod_id: str, title: str, type_name: str,
               provider: str = "regular", kind: str = ""):
        episode = providers.Episode(
            "正片", "https://cdn.test/v.m3u8", direct=True)
        return providers.OnlineTitle(
            provider=provider, vod_id=vod_id, title=title, type_name=type_name,
            year="2026", area="", updated="", remarks="正片", score="",
            poster="", overview="", actor="", director="",
            sources={"线路": [episode]},
            locator={"kind": kind} if kind else {})

    async def test_search_and_chart_binding_drop_adult_item_types(self):
        import app

        sources = [
            {"id": "regular", "kind": "cms", "enabled": True},
            {"id": "remote", "kind": "t4", "enabled": True},
        ]
        titles = [
            self._title("normal", "普通影片", "动作片"),
            self._title("adult", "成人影片", "伦理片"),
            self._title("remote-normal", "普通远程", "动作片", "remote", "t4"),
            self._title("remote-adult", "成人远程", "伦理片", "remote", "t4"),
        ]
        with patch.object(app.config, "enabled_sources", return_value=sources), \
                patch.object(app.providers, "search_all",
                             new=AsyncMock(return_value=titles)), \
                patch.object(app.config, "is_junk", return_value=False):
            briefs = await getattr(app, "__aggregate_briefs_inner")(
                "影片", 12, False, None, True)
        self.assertEqual({b["title"] for b in briefs}, {"普通影片", "普通远程"})

    async def test_hot_aggregation_filters_adult_sources_and_item_types(self):
        import hot

        regular = {"id": "regular", "kind": "cms", "enabled": True}
        adult_source = {"id": "adult", "kind": "cms", "enabled": True,
                        "adult": True}
        titles = [
            self._title("normal", "普通热门", "动作片"),
            self._title("adult", "成人热门", "伦理片"),
        ]
        with patch.object(hot, "fetch_source_hot",
                          new=AsyncMock(return_value=titles)) as fetch:
            data = await hot.collect_hot(
                [regular, adult_source], 10, 20, lambda *_: False, 1)
        self.assertEqual(data["total_sources"], 1)
        self.assertEqual(data["count"], 1)
        self.assertEqual([b["title"] for b in data["items"]], ["普通热门"])
        self.assertEqual(fetch.await_args.args[1]["id"], "regular")


if __name__ == "__main__":
    unittest.main()
