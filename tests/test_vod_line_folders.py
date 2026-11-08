"""TVBox 式二级线路海报墙与固定线路 VOD 播放。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
from store import episode_guid, ep_media_guid


def brief(tv=False):
    lines = {
        "baofeng::bfzym3u8": [{"title": "第01集" if tv else "正片",
                                "url": "https://baofeng.test/v.m3u8", "direct": True}],
        "lzi::lzm3u8": [{"title": "第01集" if tv else "正片",
                          "url": "https://lzi.test/v.m3u8", "direct": True}],
    }
    return {"guid": "og", "title": "蜘蛛侠：崭新之日", "year": "2026",
            "type_name": "电视剧" if tv else "电影", "poster": "https://img.test/p.jpg",
            "overview": "测试简介", "lines": lines}


def target(line="baofeng::bfzym3u8", index=0):
    return {"url": "https://baofeng.test/v.m3u8", "headers": {},
            "line": line, "index": index, "total": 1,
            "ep_title": "第01集", "m3u8": True}


def multi_episode_brief(ep_count=8):
    def ep(i):
        return {"title": f"第{i+1:02d}集", "url": f"https://lzi.test/v{i}.m3u8",
                "direct": True}
    return {"guid": "og", "title": "多集内容", "year": "2026",
            "type_name": "日韩动漫", "poster": "https://img.test/p.jpg",
            "overview": "", "lines": {"lzi::lzm3u8": [ep(i) for i in range(ep_count)]}}


class VodResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.brief = multi_episode_brief(8)

    def tearDown(self):
        guids = [episode_guid("og", "", i) for i in range(8)]
        appmod.history.delete(guids)

    def test_resume_index_and_play_info_locate_exact_episode_and_ts(self):
        all_eg = [episode_guid("og", "", i) for i in range(8)]
        for i in range(3):
            appmod.history.set_position(all_eg[i], 1000, 1000)
        appmod.history.set_position(all_eg[3], 600, 1200)
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.brief), \
                patch.object(appmod.store, "tv_episode_guids", return_value=all_eg), \
                patch.object(appmod.store, "get_episode",
                             side_effect=lambda g: {
                                 "index": all_eg.index(g), "ep_guid": g,
                                 "online_guid": "og", "season_guid": "season",
                                 "title": self.brief["title"],
                                 "ep_title": f"第{all_eg.index(g)+1}集"}
                             if g in all_eg else None), \
                patch.object(appmod.store, "resolve_episode",
                             side_effect=lambda og, line, idx: target(line, idx)), \
                patch.object(appmod.store, "list_episodes",
                             side_effect=lambda og, line=None: self.brief["lines"][line]):
            idx = appmod._vod_resume_index(self.brief)
            self.assertEqual(idx, 3)
            line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", idx)
            response = appmod._vod_play_info(
                line_guid, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["guid"], all_eg[3])
        self.assertEqual(data["ts"], 600)
        self.assertEqual(data["type"], "TV")

    def test_history_guid_normalization_collapses_line_identity(self):
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.brief):
            line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 3)
            normalized = appmod._normalize_history_guid(line_guid)
        self.assertEqual(normalized, episode_guid("og", "", 3))
        movie_guid = appmod._line_item_guid("movie-og", "lzi::lzm3u8", 0)
        with patch.object(appmod.store, "get_title_by_guid", return_value=None):
            self.assertEqual(appmod._normalize_history_guid(movie_guid), "movie-og")
        self.assertTrue(appmod._is_online_item_guid(line_guid))

    def test_anime_variety_and_tv_all_classify_as_multi_episode(self):
        for type_name in ("国产剧", "日韩动漫", "港台综艺", "欧美综艺", "日本动画番剧"):
            b = dict(self.brief, type_name=type_name)
            self.assertTrue(appmod.inj.is_tv(b), type_name)


class VodLineFolderTests(unittest.IsolatedAsyncioTestCase):
    def test_first_level_chart_is_directory(self):
        row = appmod.chart_card({"guid": "og", "cat": "movie", "title": "蜘蛛侠：崭新之日",
                                  "year": "2026", "rating": 8.0, "cover": "https://img.test/p.jpg",
                                  "tags": [], "desc": ""})
        self.assertEqual(row["type"], "Directory")
        self.assertEqual(row["can_play"], 0)

    def test_movie_filter_does_not_hide_directory(self):
        item = {"guid": "og", "cat": "movie", "title": "蜘蛛侠：崭新之日",
                "year": "2026", "rating": 8.0, "cover": "https://img.test/p.jpg",
                "tags": [], "desc": ""}
        with patch.object(appmod.chart_cache, "items", return_value=[item]):
            rows = appmod._chart_rows_all({"tags": {"type": ["Movie"]}}, "movie")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "Directory")

    def test_folder_guid_is_idempotent_and_detail_is_directory(self):
        self.assertEqual(appmod._line_folder_guid("og"),
                         appmod._line_folder_guid(appmod._line_folder_guid("og")))
        b = brief()
        folder_guid = appmod._line_folder_guid("og")
        with patch.object(appmod.store, "get_title_by_guid", return_value=b):
            response = appmod._vod_folder_detail(folder_guid, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["guid"], folder_guid)
        self.assertEqual(data["type"], "Directory")
        self.assertEqual(data["can_play"], 0)
        self.assertEqual(data["play_item_guid"], "")
        self.assertEqual(data["ancestor_category"], "Mix")

    async def test_tv_folder_keeps_tv_model_and_episode_count(self):
        b = brief(tv=True)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "target_lines", return_value=[
                    target("baofeng::bfzym3u8"), target("lzi::lzm3u8")]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode", return_value=(None, 0)):
            response = await appmod._vod_line_folder("og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["mdb_category"], "TV")
        self.assertTrue(all(row["type"] == "TV" for row in data["list"]))
        self.assertTrue(all(row["number_of_episodes"] == 1 for row in data["list"]))
        self.assertTrue(data["list"][0]["title"].startswith("暴风资源｜"))
        parsed = appmod._parse_line_item_guid(data["list"][0]["guid"])
        self.assertEqual(parsed, ("og", "baofeng::bfzym3u8", 0))
        self.assertEqual(data["list"][0]["parent_guid"], appmod._line_folder_guid("og"))
        self.assertEqual(data["list"][0]["ancestor_guid"], appmod._line_folder_guid("og"))

    async def test_folder_aggregates_technical_lines_by_source(self):
        b = brief(tv=True)
        m3u8_line = dict(target("dytt::dyttm3u8"), resolve=False, m3u8=True)
        resolve_line = dict(target("dytt::dytt"), resolve=True, m3u8=False)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "target_lines", return_value=[
                    resolve_line, m3u8_line]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode", return_value=(None, 0)):
            response = await appmod._vod_line_folder("og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(
            appmod._parse_line_item_guid(data["list"][0]["guid"])[1], "dytt::dyttm3u8")

    def test_fixed_media_target_uses_exact_line(self):
        b = brief()
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()):
            result = appmod._fixed_media_target(appmod._line_item_guid("og", "baofeng::bfzym3u8", 0))
        self.assertEqual(result["line"], "baofeng::bfzym3u8")
        self.assertTrue(result["_fixed_line"])

    async def test_vod_play_info_keeps_original_history_identity(self):
        b = brief(tv=True)
        eg = episode_guid("og", "", 0)
        ep = {"ep_guid": eg, "online_guid": "og", "season_guid": "season-og",
              "index": 0, "title": b["title"], "ep_title": "第01集"}
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()), \
                patch.object(appmod.store, "tv_episode_guids", return_value=[eg]), \
                patch.object(appmod.store, "list_episodes", return_value=[target()]), \
                patch.object(appmod.store, "get_episode", return_value=ep):
            response = appmod._vod_play_info(
                appmod._line_item_guid("og", "lzi::lzm3u8", 0), clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["type"], "TV")
        self.assertEqual(data["guid"], eg)
        self.assertEqual(data["item"]["guid"], eg)
        self.assertEqual(data["media_guid"], ep_media_guid(eg, "lzi::lzm3u8"))
        self.assertEqual(data["item"]["number_of_episodes"], 1)
        self.assertEqual(data["item"]["type"], "TV")

    async def test_cold_folder_waits_for_binding_to_avoid_empty_page(self):
        b = brief()
        async def slow_bind(*args, **kwargs):
            await asyncio.sleep(0.05)
        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod.chart_cache, "is_chart_guid", return_value=True), \
                patch.object(appmod, "start_resolve_chart", return_value=None), \
                patch.object(appmod, "_chart_resolving", {"chart-og": slow_bind()}), \
                patch.object(appmod, "chart_card", return_value={"title": "测试"}):
            store_mock.get_title_by_guid.side_effect = [None, b]
            store_mock.target_lines.return_value = [target("lzi::lzm3u8")]
            response = await appmod._vod_line_folder(
                "chart-og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)

    async def test_fixed_line_tv_exposes_seasons_and_line_scoped_episodes(self):
        b = brief(tv=True)
        folder_guid = appmod._line_folder_guid("og")
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "get_season", return_value={
                    "online_guid": "og", "season": 1}), \
                patch.object(appmod.inj, "season_list", return_value=[{
                    "guid": season_guid, "type": "Season", "title": "第1季",
                    "parent_guid": "og"}]), \
                patch.object(appmod.inj, "episode_list", return_value=[{
                    "guid": "ep1", "type": "Episode", "title": "第01集",
                    "episode_number": 1, "media_guid": "old-media",
                    "video_guid": "old-video"}]), \
                patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None)):
            request = Request({"type": "http", "method": "GET",
                               "path": f"/v/api/v1/season/list/{line_guid}",
                               "query_string": b"", "headers": [], "app": appmod.app})
            seasons = json.loads((await appmod.season_list_route(line_guid, request)).body)["data"]
            episodes = json.loads((await appmod.episode_list_route(line_guid, request)).body)["data"]
        self.assertEqual(seasons[0]["guid"], season_guid)
        self.assertEqual(episodes[0]["parent_guid"], season_guid)
        self.assertEqual(episodes[0]["guid"],
                         appmod._line_item_guid("og", "lzi::lzm3u8", 0))
        self.assertEqual(episodes[0]["media_guid"],
                         ep_media_guid("ep1", "lzi::lzm3u8"))

    async def test_post_stream_list_supports_fixed_line_card(self):
        b = brief(tv=True)
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        ep = {"ep_guid": "ep1", "online_guid": "og", "season_guid": "season-og",
              "index": 0, "title": b["title"], "ep_title": "第01集"}

        async def receive():
            return {"type": "http.request", "body": json.dumps(
                {"guid": line_guid}).encode()}

        request = Request({"type": "http", "method": "POST",
                           "path": "/v/api/v1/stream/list", "query_string": b"",
                           "headers": [], "app": appmod.app}, receive)
        fixed_target = dict(target("lzi::lzm3u8"), online_guid="og",
                            ep_guid="ep1", kind="episode", _fixed_line=True)
        with patch.object(appmod, "_fixed_media_target", return_value=fixed_target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]), \
                patch.object(appmod.store, "get_episode", return_value=ep), \
                patch.object(appmod.inj, "episode_stream_list", return_value={
                    "files": [{"guid": "file1", "can_play": 1}],
                    "video_streams": [{"media_guid": "media1"}],
                    "audio_streams": [], "subtitle_streams": []}), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.stream_list_post(request)
        data = json.loads(response.body)["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["files"][0]["can_play"], 1)
        self.assertEqual(data["video_streams"][0]["media_guid"], "media1")

    async def test_stream_list_supports_fixed_line_card(self):
        b = brief(tv=True)
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        ep = {"ep_guid": "ep1", "online_guid": "og", "season_guid": "season-og",
              "index": 0, "title": b["title"], "ep_title": "第01集"}
        fixed_target = dict(target("lzi::lzm3u8"), online_guid="og",
                            ep_guid="ep1", kind="episode", _fixed_line=True)
        with patch.object(appmod, "_fixed_media_target", return_value=fixed_target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]), \
                patch.object(appmod.store, "get_episode", return_value=ep), \
                patch.object(appmod.inj, "episode_stream_list", return_value={
                    "files": [{"guid": "file1", "can_play": 1}],
                    "video_streams": [{"media_guid": "media1"}],
                    "audio_streams": [], "subtitle_streams": []}), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.stream_list(line_guid, Request(
                {"type": "http", "method": "GET", "path": "/v/api/v1/stream/list/x",
                 "query_string": b"", "headers": [], "app": appmod.app}))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["files"][0]["can_play"], 1)
        self.assertEqual(data["video_streams"][0]["media_guid"], "media1")

    async def test_play_play_fixed_line_has_no_auto_and_ignores_other_bitrate(self):
        b = brief()
        guid = appmod._line_item_guid("og", "baofeng::bfzym3u8", 0)
        async def receive():
            return {"type": "http.request", "body": json.dumps(
                {"media_guid": guid, "bitrate": 1}).encode()}
        request = Request({"type": "http", "method": "POST", "path": "/v/api/v1/play/play",
                           "query_string": b"", "headers": [], "app": appmod.app}, receive)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()), \
                patch.object(appmod, "_line_choices", return_value=[target(), target("lzi::lzm3u8")]), \
                patch.object(appmod, "_guess_wrapper", return_value=("HLS", True)), \
                patch.object(appmod, "_warm_preset_lines"), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.play_play(request)
        data = json.loads(response.body)["data"]
        self.assertIn("line=baofeng%3A%3Abfzym3u8", data["play_link"])
        self.assertNotIn("auto=1", data["play_link"])


if __name__ == "__main__":
    unittest.main()
