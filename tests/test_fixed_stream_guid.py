"""固定线路 play/info 与 stream/list 的 media/video guid 必须一致。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
import tmdb
from starlette.requests import Request
from store import episode_guid, ep_media_guid, ep_video_guid


def _brief(tv=False):
    return {"guid": "og", "title": "可能的爱", "type_name": "电视剧" if tv else "电影",
            "year": "2026", "poster": "", "overview": "",
            "lines": {"lzi::lzm3u8": [{"title": "第01集" if tv else "正片",
                                       "url": "https://lzi.test/v.m3u8", "direct": True}]}}


def _target(tv=False):
    return {"online_guid": "og", "index": 0, "line": "lzi::lzm3u8",
            "ep_guid": episode_guid("og", "", 0) if tv else "",
            "url": "https://lzi.test/v.m3u8", "headers": {}, "direct": True}


class FixedStreamGuidTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_play_info_and_stream_share_line_guid(self):
        brief = _brief()
        target = _target()
        pi_request = Request({"type": "http", "method": "POST", "path": "/",
                              "headers": [(b"user-agent", b"Runner/1.13.0 ios")],
                              "query_string": b""})
        with patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None), create=True), \
                patch.object(appmod, "_xlib_on", return_value=False, create=True), \
                patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod.store, "resolve_episode", return_value=target), \
                patch.object(appmod.store, "tv_episode_guids", return_value=[]), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_ios_prepare_playlist") as prepare, \
                patch.object(tmdb, "enabled", return_value=False):
            sl_resp = await appmod._stream_list_impl(
                appmod._line_item_guid("og", "lzi::lzm3u8", 0),
                Request({"type": "http", "method": "GET", "path": "/",
                         "headers": [], "query_string": b""}))
            pi_resp = appmod._vod_play_info(
                appmod._line_item_guid("og", "lzi::lzm3u8", 0),
                clientmod.Client(kind="ios"), pi_request)
        sl = json.loads(sl_resp.body)["data"]
        pi = json.loads(pi_resp.body)["data"]
        eg = episode_guid("og", "", 0)
        mg = ep_media_guid(eg, "lzi::lzm3u8")
        vg = ep_video_guid(eg, "lzi::lzm3u8")
        self.assertEqual(sl["files"][0]["guid"], mg)
        self.assertEqual(sl["video_streams"][0]["media_guid"], mg)
        self.assertEqual(sl["video_streams"][0]["guid"], vg)
        self.assertEqual(pi["media_guid"], mg)
        self.assertEqual(pi["video_guid"], vg)
        prepare.assert_called_once_with(mg, target, pi_request)
        self.assertTrue(sl["play_link"].startswith(
            f"/v/media/online/{mg}/preset.m3u8?"))
        self.assertNotIn("fnmedia-line-item", sl["play_link"])

    async def test_movie_stream_post_preserves_line_video_guid(self):
        """电影没有 ep_guid，也不能把带线路 media guid 错配成基础 video guid。"""
        brief, target = _brief(), _target()
        eg = episode_guid("og", "", 0)
        mg = ep_media_guid(eg, "lzi::lzm3u8")
        expected_vg = ep_video_guid(eg, "lzi::lzm3u8")
        payload = json.dumps({"media_guid": mg}).encode()

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request({
            "type": "http", "method": "POST", "path": "/v/api/v1/stream",
            "headers": [(b"user-agent", b"1.13.0 (com.trim.media.ios; build:113)")],
            "query_string": b"",
        }, receive)
        # 真实哈希 media guid 无法按 line-item 文本反解，会落到 resolve_media_guid。
        with patch.object(appmod, "_xlib_on", return_value=False, create=True), \
                patch.object(appmod, "_fixed_media_target", return_value=None), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_ios_stream_playlist_meta",
                             new=AsyncMock(return_value={})):
            response = await appmod.stream_post(request)
        data = json.loads(response.body)["data"]
        self.assertEqual(data["file_stream"]["guid"], mg)
        self.assertEqual(data["video_stream"]["guid"], expected_vg)
        self.assertEqual(data["video_stream"]["media_guid"], mg)

    @unittest.skipUnless(hasattr(appmod, "_pansou_register_media"),
                         "公开构建不含 PanSou 注册逻辑")
    async def test_pansou_still_uses_dedicated_guid_and_registration(self):
        brief = _brief()
        target = _target()
        target["line"] = appmod.PANSOU_LINE
        with patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None), create=True), \
                patch.object(appmod, "_xlib_on", return_value=False, create=True), \
                patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod.store, "tv_episode_guids", return_value=[]), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_pansou_register_media") as reg:
            resp = await appmod._stream_list_impl(
                appmod._line_item_guid("og", appmod.PANSOU_LINE, 0),
                Request({"type": "http", "method": "GET", "path": "/",
                         "headers": [], "query_string": b""}))
        sl = json.loads(resp.body)["data"]
        eg = episode_guid("og", "", 0)
        self.assertEqual(sl["files"][0]["guid"], ep_media_guid(eg, appmod.PANSOU_LINE))
        self.assertEqual(sl["video_streams"][0]["guid"], ep_video_guid(eg, appmod.PANSOU_LINE))
        reg.assert_called_once_with("og", 0)


if __name__ == "__main__":
    unittest.main()
