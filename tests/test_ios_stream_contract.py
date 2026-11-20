"""iOS 在 stream/list、stream、play/play 的渐进式 MKV relay 契约。

新版 iOS 的 fncache HLS 本地代理对聚合 CMS 取清单后零分片（HAR + issue#98 +
安卓 fncache 反编译证实）；resolver 可播因其为原生 MP4。渐进式文件是 iOS 唯一
验证可播路径，故 iOS HLS 改走 ffmpeg -c copy MKV relay（相对 play_link），
Web/TV 仍走 HLS preset。relay 机制（单飞/Duration/Range）见 tests/test_ios_relay.py。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


IOS_UA = "1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2) Flutter/3.11.5"
TV_UA = "1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0"


def _target() -> dict:
    return {"url": "https://cdn.test/v.m3u8", "headers": {}, "line": "lzi::lzm3u8",
            "index": 0, "online_guid": "og", "m3u8": True, "_fixed_line": True,
            "ep_guid": "", "kind": "title"}


def _brief() -> dict:
    return {"guid": "og", "title": "契约影片", "runtime": 120, "type_name": "电影"}


def _stream_payload() -> dict:
    return {"files": [{"guid": "mg", "path": "在线/契约影片.m3u8",
                       "file_name": "契约影片.m3u8", "size": 0}],
            "video_streams": [{"guid": "vg", "media_guid": "mg", "duration": 0,
                               "wrapper": "HLS"}],
            "audio_streams": [], "subtitle_streams": [], "qualities": []}


def _request(ua: str, payload: dict | None = None, *, scheme: str = "http") -> Request:
    headers = [(b"user-agent", ua.encode()), (b"host", b"nas.local:8000")]
    scope = {"type": "http", "method": "POST" if payload is not None else "GET",
             "path": "/", "query_string": b"", "headers": headers,
             "scheme": scheme, "app": appmod.app}
    if payload is None:
        return Request(scope)
    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode()}
    return Request(scope, receive)


class IosStreamContractTests(unittest.IsolatedAsyncioTestCase):
    async def _play(self, ua: str) -> tuple[dict, AsyncMock]:
        target = _target()
        request = _request(ua, {"media_guid": "mg", "video_guid": "vg"})
        relay = AsyncMock()
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod, "_line_choices",
                             return_value=[{"line": target["line"], "label": "量子"}]), \
                patch.object(appmod.store, "target_lines", return_value=[]), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_ensure_relay_job", new=relay):
            response = await appmod.play_play(request)
            await asyncio.sleep(0)
        return json.loads(response.body)["data"], relay

    async def test_play_play_uses_relay_for_ios_hls_for_tv(self):
        ios, ios_relay = await self._play(IOS_UA)
        tv, tv_relay = await self._play(TV_UA)
        # iOS HLS(fncache) 零分片坏路径 → 渐进式 MKV relay（相对路径复用会话）。
        self.assertTrue(ios["play_link"].startswith("/v/media/online/mg/relay?"))
        self.assertNotIn("://", ios["play_link"])
        self.assertEqual(ios["wrapper"], "MKV")
        ios_relay.assert_awaited()  # 起播前预热 remux
        # Web/TV 仍 preset master 选线菜单，HLS 包装，不启动 relay。
        self.assertTrue(tv["play_link"].startswith("/v/media/online/mg/preset.m3u8?"))
        self.assertNotIn("://", tv["play_link"])
        self.assertNotIn("/relay", tv["play_link"])
        self.assertEqual(tv["wrapper"], "HLS")
        tv_relay.assert_not_awaited()

    async def test_stream_list_and_stream_post_use_ios_relay_contract(self):
        target = _target()
        relay = AsyncMock()
        with patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None)), \
                patch.object(appmod, "_xlib_on", return_value=False, create=True), \
                patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                patch.object(appmod.inj, "is_tv", return_value=False), \
                patch.object(appmod.inj, "stream_list", side_effect=lambda _: _stream_payload()), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_ios_stream_playlist_meta",
                             new=AsyncMock(return_value={})), \
                patch.object(appmod, "_ensure_relay_job", new=relay):
            listed = await appmod._stream_list_impl("mg", _request(IOS_UA))
            posted = await appmod.stream_post(
                _request(IOS_UA, {"media_guid": "mg", "video_guid": "vg"}))
            await asyncio.sleep(0)
        list_data = json.loads(listed.body)["data"]
        post_data = json.loads(posted.body)["data"]
        list_v = list_data["video_streams"][0]
        post_v = post_data["video_stream"]
        list_f = list_data["files"][0]
        post_f = post_data["file_stream"]
        # iOS relay 镜像本地契约：MKV 包装 + 非零 duration/size + .mkv 文件名/路径。
        # duration 无清单/relay 探测时兜底 brief.runtime(120min→7200s)。
        for v in (list_v, post_v):
            self.assertEqual(v["wrapper"], "MKV")
            self.assertEqual(v["duration"], 7200)
        for f in (list_f, post_f):
            self.assertEqual(f["file_name"], "契约影片.mkv")
            self.assertEqual(f["path"], "在线/契约影片.mkv")
            self.assertGreater(f["size"], 0)
        # 固定线路 stream/list 的 play_link 必须使用响应中的带线路 media guid，
        # 不能继续使用页面条目的 guid；stream POST 则保持请求 mg。均指向 relay。
        self.assertTrue(list_data["play_link"].startswith(
            f"/v/media/online/{list_v['media_guid']}/relay?"))
        self.assertTrue(post_data["play_link"].startswith(
            "/v/media/online/mg/relay?"))
        for data in (list_data, post_data):
            self.assertNotIn("://", data["play_link"])
        self.assertGreaterEqual(relay.await_count, 2)  # 两入口都预热 remux

    async def test_ios_stream_reads_ready_cache_without_waiting(self):
        target = _target()
        request = _request(IOS_UA, {"media_guid": "mg", "video_guid": "vg"})
        body = ("#EXTM3U\n#EXTINF:12.5,\na.ts\n"
                "#EXTINF:30.4,\nb.ts\n#EXT-X-ENDLIST\n")
        fetch = AsyncMock(side_effect=AssertionError("stream must not await playlist"))
        relay = AsyncMock()
        key = appmod._playlist_cache_key(target)
        appmod._PLAYLIST_CACHE[key] = (time.time(), body)
        appmod._PLAYLIST_FUT.pop(key, None)
        try:
            with patch.object(appmod, "_fixed_media_target", return_value=target), \
                    patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                    patch.object(appmod.inj, "stream_list", side_effect=lambda _: _stream_payload()), \
                    patch.object(appmod, "_qualities", return_value=[]), \
                    patch.object(appmod, "_warm_playlist") as warm, \
                    patch.object(appmod, "_ensure_relay_job", new=relay), \
                    patch.object(appmod, "_get_playlist_cached", new=fetch):
                response = await appmod.stream_post(request)
                await asyncio.sleep(0)
            data = json.loads(response.body)["data"]
            # relay duration 从已缓存清单 EXTINF 求和（12.5+30.4→42），MKV 包装。
            self.assertEqual(data["video_stream"]["duration"], 42)
            self.assertEqual(data["video_stream"]["wrapper"], "MKV")
            warm.assert_called_once_with("mg", target, request)
            fetch.assert_not_awaited()
            relay.assert_awaited()
        finally:
            appmod._PLAYLIST_CACHE.pop(key, None)

    async def test_stream_ready_makes_preset_main_a_cache_hit(self):
        target = _target()
        request = _request(IOS_UA)
        request.scope["path"] = "/v/media/online/mg/main.m3u8"
        request.scope["query_string"] = b"line=lzi%3A%3Alzm3u8&index=0&ph=1"
        body = ("#EXTM3U\n#EXT-X-TARGETDURATION:30\n"
                "#EXTINF:12.5,\na.ts\n#EXTINF:30.4,\nb.ts\n#EXT-X-ENDLIST\n")
        build = AsyncMock(return_value=(body, ""))
        key = appmod._playlist_cache_key(target)
        appmod._PLAYLIST_CACHE.pop(key, None)
        appmod._PLAYLIST_FUT.pop(key, None)
        try:
            with patch.object(appmod, "_build_playlist_body", new=build), \
                    patch.object(appmod, "_pick_episode", return_value=target):
                meta = await appmod._ios_stream_playlist_meta("mg", target, request)
                self.assertEqual(meta, {})  # pending：stream 不等待
                response = await appmod._hls_main_impl("mg", request)
                ready_meta = await appmod._ios_stream_playlist_meta("mg", target, request)
                await asyncio.sleep(0)
            self.assertEqual(ready_meta["duration"], 42)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"#EXTINF:12.5", response.body)
            self.assertEqual(build.await_count, 1)
        finally:
            appmod._PLAYLIST_CACHE.pop(key, None)
            appmod._PLAYLIST_FUT.pop(key, None)

    async def test_ios_stream_pending_playlist_returns_without_waiting(self):
        target = _target()
        request = _request(IOS_UA, {"media_guid": "mg", "video_guid": "vg"})
        fetch = AsyncMock(side_effect=AssertionError("stream must not await playlist"))
        relay = AsyncMock()
        key = appmod._playlist_cache_key(target)
        pending = asyncio.get_running_loop().create_future()
        appmod._PLAYLIST_CACHE.pop(key, None)
        appmod._PLAYLIST_FUT[key] = pending  # 真实表直接存 Future，不带时间戳
        try:
            with patch.object(appmod, "_fixed_media_target", return_value=target), \
                    patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                    patch.object(appmod.inj, "stream_list", side_effect=lambda _: _stream_payload()), \
                    patch.object(appmod, "_qualities", return_value=[]), \
                    patch.object(appmod, "_warm_playlist") as warm, \
                    patch.object(appmod, "_ensure_relay_job", new=relay), \
                    patch.object(appmod, "_get_playlist_cached", new=fetch):
                response = await appmod.stream_post(request)
                await asyncio.sleep(0)
            data = json.loads(response.body)["data"]
            # 清单未就绪：relay duration 兜底 brief.runtime(120min→7200s)，MKV 不等待。
            self.assertEqual(data["video_stream"]["duration"], 7200)
            self.assertEqual(data["video_stream"]["wrapper"], "MKV")
            warm.assert_called_once_with("mg", target, request)
            fetch.assert_not_awaited()
        finally:
            pending.cancel()
            appmod._PLAYLIST_FUT.pop(key, None)

    async def test_tv_stream_does_not_wait_for_playlist(self):
        target = _target()
        request = _request(TV_UA, {"media_guid": "mg", "video_guid": "vg"})
        fetch = AsyncMock(return_value=b"#EXTM3U\n#EXTINF:42,\na.ts\n")
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                patch.object(appmod.inj, "stream_list", side_effect=lambda _: _stream_payload()), \
                patch.object(appmod, "_qualities", return_value=[]), \
                patch.object(appmod, "_warm_playlist") as warm, \
                patch.object(appmod, "_get_playlist_cached", new=fetch):
            response = await appmod.stream_post(request)
        data = json.loads(response.body)["data"]
        self.assertEqual(data["video_stream"]["duration"], 0)
        warm.assert_not_called()
        fetch.assert_not_awaited()

    async def test_direct_file_does_not_wait_for_playlist(self):
        target = _target()
        target["url"] = "https://cdn.test/movie.mp4"
        fetch = AsyncMock()
        with patch.object(appmod, "_warm_playlist") as warm, \
                patch.object(appmod, "_get_playlist_cached", new=fetch):
            meta = await appmod._ios_stream_playlist_meta("mg", target, _request(IOS_UA))
        self.assertEqual(meta, {})
        warm.assert_not_called()
        fetch.assert_not_awaited()

    async def test_ios_play_info_prewarms_only_current_line(self):
        target = _target()
        request = _request(IOS_UA, {"item_guid": "og"})
        info = {"media_guid": "mg", "item": {}}
        with patch.object(appmod, "_load_t4_detail",
                          new=AsyncMock(return_value=None), create=True), \
                patch.object(appmod, "_xlib_on", return_value=False, create=True), \
                patch.object(appmod.store, "get_episode", return_value={"guid": "og"}), \
                patch.object(appmod.inj, "episode_play_info", return_value=info), \
                patch.object(appmod.history, "get", return_value=None), \
                patch.object(appmod, "_ensure_resume_duration", side_effect=lambda x: x), \
                patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ios_prepare_playlist") as prepare, \
                patch.object(appmod.clientmod, "normalize_detail",
                             side_effect=lambda value, client: value):
            response = await appmod.play_info(request)
        self.assertEqual(json.loads(response.body)["code"], 0)
        prepare.assert_called_once_with("mg", target, request)

    def test_ios_hls_play_link_is_relative_relay(self):
        link = appmod._stream_play_link(_target(), "mg", _request(IOS_UA))
        # iOS HLS(fncache) 零分片坏路径 → 渐进式 MKV relay，相对路径复用会话。
        self.assertTrue(link.startswith("/v/media/online/mg/relay?"))
        self.assertNotIn("://", link)
        self.assertNotIn("from=mr", link)
        proxy_request = _request(IOS_UA, scheme="http")
        proxy_request.scope["headers"].append((b"x-forwarded-proto", b"https"))
        proxied = appmod._stream_play_link(_target(), "mg", proxy_request)
        self.assertEqual(proxied, link)
        # 非 iOS（TV）仍走 preset master 选线菜单，不进 relay。
        tv = appmod._stream_play_link(_target(), "mg", _request(TV_UA))
        self.assertIn("/preset.m3u8?", tv)
        self.assertNotIn("/relay", tv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
