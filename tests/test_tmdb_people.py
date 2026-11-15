"""TMDB 演员表与历史横幅回归（不打真实 API）。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import inject
import tmdb
from starlette.requests import Request


def _req():
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [], "query_string": b""})


def _data(resp):
    return json.loads(resp.body)["data"]


class PersonListTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_returns_native_people(self):
        brief = {"guid": "kg", "title": "海量片", "year": "2026",
                 "catalog_scope": "__agg__", "catalog_cat": "movie"}
        meta = {"cast": [
            {"id": 11, "name": "演员甲", "original_name": "Actor A",
             "character": "角色甲", "profile": "https://image.tmdb.org/t/p/w185/a.jpg"},
            {"id": 22, "name": "演员乙", "character": "角色乙", "profile": ""},
        ]}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod, "_inherit_lib_identity", return_value=None), \
                patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=meta):
            data = _data(await appmod.person_list("kg", _req()))
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["list"][0]["person_guid"], "tmdb-person-11")
        self.assertEqual(data["list"][0]["job"], "Actor")
        self.assertEqual(data["list"][0]["role"], "角色甲")
        self.assertTrue(data["list"][0]["profile_path"].startswith("/online/"))

    async def test_chart_unbound_also_supported(self):
        chart = {"cat": "movie", "title": "热门片", "year": "2026"}
        meta = {"cast": [{"id": 33, "name": "演员丙", "profile": ""}]}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.chart_cache, "is_chart_guid", return_value=True), \
                patch.object(appmod.chart_cache, "get_guid", return_value=chart), \
                patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=meta) as en:
            data = _data(await appmod.person_list("cg", _req()))
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["name"], "演员丙")
        self.assertEqual(en.call_args[0][0]["chart"], chart)

    async def test_line_folder_guid_resolves_base_title(self):
        brief = {"guid": "kg", "title": "海量片", "year": "2026",
                 "catalog_scope": "__agg__", "catalog_cat": "movie"}
        meta = {"cast": [{"id": 44, "name": "演员丁", "profile": ""}]}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief) as gt, \
                patch.object(appmod, "_inherit_lib_identity", return_value=None), \
                patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=meta):
            data = _data(await appmod.person_list(
                appmod._line_folder_guid("kg"), _req()))
        self.assertEqual(data["total"], 1)
        gt.assert_called_with("kg")

    async def test_line_season_guid_resolves_base_title(self):
        brief = {"guid": "kg", "title": "剧集", "year": "2026",
                 "catalog_scope": "__agg__", "catalog_cat": "tv"}
        meta = {"cast": [{"id": 55, "name": "演员戊", "profile": ""}]}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief) as gt, \
                patch.object(appmod, "_inherit_lib_identity", return_value=None), \
                patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=meta):
            data = _data(await appmod.person_list(
                appmod._line_season_guid("kg", "lzi::lzm3u8", 1), _req()))
        self.assertEqual(data["total"], 1)
        gt.assert_called_with("kg")

    async def test_cold_composite_guid_triggers_binding(self):
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.chart_cache, "is_chart_guid", return_value=False), \
                patch.object(appmod.catalog, "find_brief_by_guid", return_value=None), \
                patch.object(appmod.history, "get_snapshot", return_value=None), \
                patch.object(appmod, "prefetch_folder_binding") as bind:
            data = _data(await appmod.person_list(
                appmod._line_folder_guid("cold"), _req()))
        self.assertEqual(data["total"], 0)
        bind.assert_called_once_with("cold")

    async def test_custom_library_empty(self):
        brief = {"guid": "sg", "title": "自定义片",
                 "catalog_scope": "custom_abc"}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod, "_inherit_lib_identity", return_value=None), \
                patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=None):
            data = _data(await appmod.person_list("sg", _req()))
        self.assertEqual(data, {"total": 0, "list": []})


class DetailPayloadTests(unittest.TestCase):
    def test_genre_names_never_break_native_detail_model(self):
        brief = {"guid": "cg", "title": "黑球", "year": "2026",
                 "type_name": "剧情片", "poster": "", "overview": "简介",
                 "chart": {"cat": "movie"}}
        meta = {"genres": ["剧情", "同性", "历史"],
                "overview": "豆瓣简介", "cast": [], "poster": ""}
        with patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value=meta):
            data = inject.detail_item(brief)
        self.assertEqual(data["genres"], [])
        self.assertIn("类型：剧情 / 同性 / 历史", data["overview"])

    def test_integer_genre_ids_preserved(self):
        brief = {"guid": "cg", "title": "影片", "type_name": "电影",
                 "poster": "", "chart": {"cat": "movie"}}
        with patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value={"genres": [12, 28]}):
            data = inject.detail_item(brief)
        self.assertEqual(data["genres"], [12, 28])


class HistoryBackdropTests(unittest.TestCase):
    def test_tmdb_backdrop_used_without_crop(self):
        brief = {"guid": "cg", "title": "热门片", "year": "2026",
                 "poster": "https://src/poster.jpg", "type_name": "电影",
                 "chart": {"cat": "movie"}}
        url = "https://image.tmdb.org/t/p/w1280/backdrop.jpg"
        with patch.object(tmdb, "enabled", return_value=True), \
                patch.object(tmdb, "enrich", return_value={"backdrop": url}):
            card = inject.history_item(brief, {"ts": 1, "duration": 100})
        expected = inject._poster(url)
        self.assertEqual(card["backdrops"], expected)
        self.assertEqual(card["poster"], expected)
        self.assertNotIn("crop=16x9", card["backdrops"])


if __name__ == "__main__":
    unittest.main()
