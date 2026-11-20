"""新版 iOS fncache：preset 直接媒体清单 + 短 .ts 分片的原生 HLS 契约。"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


IOS_UA = "1.12.2 (com.trim.media.ios; build:1122011; iOS 27.2) Flutter/3.11.5"
TV_UA = "1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0"


def _request(ua: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/v/api/v1/media/range/mg",
                    "query_string": b"", "headers": [(b"user-agent", ua.encode())]})


def _playlist_request(ua: str = IOS_UA) -> Request:
    return Request({
        "type": "http", "method": "GET", "scheme": "http",
        "server": ("internal", 80), "path": "/v/media/online/mg/main.m3u8",
        "query_string": b"index=0&ph=1",
        "headers": [(b"user-agent", ua.encode()), (b"host", b"media.test"),
                    (b"x-forwarded-proto", b"https")],
    })


def _target() -> dict:
    return {"url": "https://cdn.test/v.m3u8", "headers": {}, "line": "lzi::lzm3u8",
            "index": 0, "online_guid": "og", "m3u8": True, "_fixed_line": True}


class IosMediaRangeTests(unittest.IsolatedAsyncioTestCase):
    async def _location(self, ua: str) -> str:
        target = _target()
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None), \
                patch.object(appmod.store, "target_lines", return_value=[target]), \
                patch.object(appmod, "_warm_preset_lines"), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "start_fill_lines"):
            response = await appmod.media_range("mg", _request(ua))
        self.assertEqual(response.status_code, 302)
        return response.headers["location"]

    async def test_ios_hls_302_to_relay(self):
        # iOS 1.13 的 HLS(fncache) 是零分片坏路径（issue#98 官方已知；resolver 能播
        # 实为 MP4）→ 真机唯一可播的渐进式 MKV relay，不再进 preset/main。
        location = await self._location(IOS_UA)
        self.assertIn("/relay?", location)
        self.assertNotIn("/preset.m3u8?", location)
        self.assertNotIn("/main.m3u8?", location)
        self.assertIn("line=lzi%3A%3Alzm3u8", location)

    async def test_tv_hls_keeps_preset_master(self):
        location = await self._location(TV_UA)
        self.assertIn("/preset.m3u8?", location)
        self.assertNotIn("/main.m3u8?", location)
        self.assertNotIn("from=mr", location)

    async def test_pending_chart_ios_keeps_placeholder_preset(self):
        with patch.object(appmod, "_fixed_media_target", return_value=None), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None), \
                patch.object(appmod, "chart_guid_by_media", return_value="chart-og"), \
                patch.object(appmod, "start_resolve_chart"):
            response = await appmod.media_range("mg", _request(IOS_UA))
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertIn("/preset.m3u8?", location)
        self.assertIn("from=mr", location)
        self.assertIn("phwait=1", location)

    async def test_ios_mp4_still_uses_direct(self):
        target = dict(_target(), url="https://cdn.test/v.mp4", m3u8=False)
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None):
            response = await appmod.media_range("mg", _request(IOS_UA))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/direct?", response.headers["location"])

    async def test_preset_route_serves_real_placeholder_not_range_helper(self):
        # 回归：装饰器曾误挂在 _m3u8_resp 上，Location 虽正确，跟随请求却恒 422。
        placeholder = appmod._placeholder_playlist_response("mg")
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=appmod.app, raise_app_exceptions=False),
                base_url="http://test") as client:
            response = await client.get(
                "/v/media/online/mg/preset.m3u8?index=0&ph=1",
                headers={"User-Agent": IOS_UA})
            self.assertEqual(response.status_code, 200)
            self.assertIn("#EXTM3U", response.text)
            variant = next(line for line in response.text.splitlines()
                           if line and not line.startswith("#"))
            self.assertTrue(variant.startswith("main.m3u8?"))
            self.assertEqual(str(response.url.join(variant)),
                             "http://test/v/media/online/mg/" + variant)
            preset_probe = await client.get(
                "/v/media/online/mg/preset.m3u8?index=0&ph=1",
                headers={"User-Agent": IOS_UA, "Range": "bytes=0-1"})
            self.assertEqual(preset_probe.status_code, 200)
            self.assertEqual(preset_probe.content, response.content)
            self.assertEqual(preset_probe.headers["Content-Length"],
                             str(len(response.content)))
            self.assertNotIn("Content-Range", preset_probe.headers)
            self.assertNotIn("Accept-Ranges", preset_probe.headers)
            with patch.object(appmod, "_hls_main_impl",
                              new=AsyncMock(return_value=placeholder)):
                main = await client.get(response.url.join(variant),
                                        headers={"User-Agent": IOS_UA})
                main_probe = await client.get(response.url.join(variant), headers={
                    "User-Agent": IOS_UA, "Range": "bytes=0-1"})
        for fetched in (main, main_probe):
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.content, placeholder.body)
            self.assertEqual(fetched.headers["Content-Length"], str(len(fetched.content)))
            self.assertNotIn("Content-Range", fetched.headers)
            self.assertNotIn("Accept-Ranges", fetched.headers)
            for line in fetched.text.splitlines():
                if line and not line.startswith("#"):
                    self.assertTrue(line.startswith("_placeholder/placeholder.ts?"))
                    self.assertEqual(fetched.url.join(line).path,
                                     "/v/media/online/mg/_placeholder/placeholder.ts")

    async def test_non_ios_real_preset_keeps_relative_main_variant(self):
        target = _target()
        request = _playlist_request(TV_UA)
        request.scope["path"] = "/v/media/online/mg/preset.m3u8"
        request.scope["query_string"] = b"index=0&line=lzi%3A%3Alzm3u8&auto=1"
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "target_lines", return_value=[]), \
                patch.object(appmod, "_pansou_available", return_value=False), \
                patch.object(appmod, "_warm_preset_lines"):
            response = await appmod.hls_preset("mg", request)
        self.assertEqual(response.status_code, 200)
        variant = next(line for line in response.body.decode().splitlines()
                       if line and not line.startswith("#"))
        self.assertTrue(variant.startswith("main.m3u8?"), variant)
        self.assertNotIn("/v/media/online/", variant)
        self.assertIn("line=lzi%3A%3Alzm3u8", variant)

    async def test_ios_from_mr_delegates_directly_to_main(self):
        request = _playlist_request(IOS_UA)
        request.scope["path"] = "/v/media/online/mg/preset.m3u8"
        query = urllib.parse.urlencode(
            {"index": 0, "line": "lzi::lzm3u8", "auto": 1, "from": "mr"})
        request.scope["query_string"] = query.encode()
        expected = Response("#EXTM3U\n#EXTINF:2.5,\nsegment.ts\n",
                            media_type="application/vnd.apple.mpegurl")
        main = AsyncMock(return_value=expected)
        with patch.object(appmod, "hls_main", new=main), \
                patch.object(appmod, "_pick_episode",
                             side_effect=AssertionError("不得再生成 master")):
            response = await appmod.hls_preset("mg", request)
        self.assertIs(response, expected)
        main.assert_awaited_once()
        forwarded_mg, forwarded = main.await_args.args
        self.assertEqual(forwarded_mg, "mg")
        self.assertIsNot(forwarded, request)
        self.assertEqual(forwarded.scope["path"],
                         "/v/media/online/mg/main.m3u8")
        params = urllib.parse.parse_qs(forwarded.scope["query_string"].decode())
        self.assertEqual(params["line"], ["lzi::lzm3u8"])
        self.assertEqual(params["index"], ["0"])
        self.assertEqual(params["ph"], ["1"])
        self.assertNotIn("auto", params)
        self.assertNotIn("from", params)

    async def test_ios_from_mr_preserves_pansou_user_pick(self):
        request = _playlist_request(IOS_UA)
        request.scope["path"] = "/v/media/online/mg/preset.m3u8"
        request.scope["query_string"] = urllib.parse.urlencode(
            {"line": appmod.PANSOU_LINE, "from": "mr"}).encode()
        main = AsyncMock(return_value=Response("ok"))
        with patch.object(appmod, "hls_main", new=main), \
                patch.object(appmod, "_pansou_mark_user_pick") as mark:
            await appmod.hls_preset("mg", request)
        mark.assert_called_once_with("mg")
        main.assert_awaited_once()
        forwarded = main.await_args.args[1]
        self.assertEqual(forwarded.scope["path"],
                         "/v/media/online/mg/main.m3u8")
        params = urllib.parse.parse_qs(forwarded.scope["query_string"].decode())
        self.assertEqual(params["line"], [appmod.PANSOU_LINE])
        self.assertEqual(params["ph"], ["1"])

    async def test_ios_ph_returns_ready_playlist_via_shared_singleflight(self):
        request = _playlist_request(IOS_UA)
        request.scope["query_string"] = urllib.parse.urlencode(
            {"line": "lzi::lzm3u8", "index": 0, "ph": 1}).encode()
        target = _target()
        body = ("#EXTM3U\n#EXTINF:2.5,\n"
                "/v/media/online/mg/seg?i=1\n#EXT-X-ENDLIST\n")
        fetch = AsyncMock(return_value=body)
        with patch.object(appmod, "_await_pending_chart",
                          new=AsyncMock(return_value=None)), \
                patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_dodge_known_bad", side_effect=lambda p, r: p), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_get_playlist_cached", new=fetch):
            response = await appmod._hls_main_impl("mg", request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Connection"], "close")
        self.assertRegex(response.body.decode(),
                         r"/v/media/online/seg/[0-9a-f]{16}\.ts")
        fetch.assert_awaited_once_with("mg", target, request)

    async def test_ios_ph_timeout_returns_placeholder_without_cancelling_build(self):
        request = _playlist_request(IOS_UA)
        request.scope["query_string"] = urllib.parse.urlencode(
            {"line": "lzi::lzm3u8", "index": 0, "ph": 1}).encode()
        target = _target()
        pending = asyncio.get_running_loop().create_future()

        async def slow_fetch(*args):
            return await pending

        with patch.object(appmod, "_await_pending_chart",
                          new=AsyncMock(return_value=None)), \
                patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_dodge_known_bad", side_effect=lambda p, r: p), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "_get_playlist_cached", side_effect=slow_fetch), \
                patch.object(appmod, "_IOS_READY_BUDGET", 0.01):
            response = await appmod._hls_main_impl("mg", request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-fnmedia-Placeholder"], "1")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Connection"], "close")
        text = response.body.decode()
        self.assertIn("_placeholder/placeholder.ts?", text)
        self.assertNotIn("#EXT-X-ENDLIST", text)
        self.assertFalse(pending.cancelled())
        pending.set_result("#EXTM3U\n#EXTINF:2,\na.ts\n")
        await asyncio.sleep(0)

    @staticmethod
    def _playlist_body() -> bytes:
        return (
            b"\xef\xbb\xbf#EXTM3U\r\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000000\r\n"
            b"main.m3u8?index=0\r\n"
            b"#EXT-X-KEY:METHOD=AES-128,URI=\"/v/media/online/mg/key?k=1\"\r\n"
            b"#EXT-X-MAP:URI=\"hls?r=r1&i=init\"\r\n"
            b"/v/media/online/mg/seg?i=1&dec=1\r\n"
            b"hls?r=r1&i=2\r\n"
            b"https://cdn.test/remote.ts\r\n"
            b"#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI=\"skd://key\"\r\n"
            b"#EXT-X-ENDLIST\r\n"
        )

    def test_non_ios_playlist_bytes_stay_unchanged(self):
        body = self._playlist_body()
        response = appmod._m3u8_resp(
            body, _playlist_request(TV_UA),
            headers={"Content-Length": "999", "Content-Range": "bytes 0-1/999",
                     "Cache-Control": "no-cache"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, body)
        self.assertEqual(response.headers["Content-Length"], str(len(body)))
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertNotIn("Connection", response.headers)
        self.assertNotIn("Content-Range", response.headers)

    def test_ios_shortens_only_proxy_segment_uris(self):
        body = self._playlist_body()
        response = appmod._m3u8_resp(
            body, _playlist_request(IOS_UA),
            headers={"Content-Length": "999", "Content-Range": "bytes 0-1/999",
                     "Cache-Control": "no-cache"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Length"], str(len(response.body)))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Connection"], "close")
        self.assertNotIn("Content-Range", response.headers)
        text = response.body.decode()
        self.assertTrue(text.startswith("\ufeff#EXTM3U\r\n"))
        self.assertIn("main.m3u8?index=0\r\n", text)
        # iOS 响应把本代理 URI（含 KEY）改写成外部绝对地址，避免 fncache 本地
        # 缓存后根相对分片失去远端 base；源站绝对 URL 与非 HTTP scheme 不动。
        self.assertIn('URI="https://media.test/v/media/online/mg/key?k=1"', text)
        self.assertIn("https://cdn.test/remote.ts\r\n", text)
        self.assertIn('URI="skd://key"', text)
        self.assertNotIn("/seg?", text)
        self.assertNotIn("hls?r=", text)
        short = [line for line in text.splitlines() if "/seg/" in line]
        self.assertEqual(len(short), 3)  # MAP、普通 seg、紧凑 hls
        for line in short:
            self.assertRegex(
                line, r"/v/media/online/seg/[0-9a-f]{16}\.ts")

    def test_ios_segment_uris_are_absolute_with_request_host_port(self):
        # fncache 本地缓存会使根相对分片失去远端 base；iOS 分片必须是带当前
        # Host 端口的绝对地址（社区 fork 曾因丢端口回退相对而重新引入零分片）。
        body = (b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:3,\n"
                b"/v/media/online/mg/seg?i=1\n#EXT-X-ENDLIST\n")
        req = Request({
            "type": "http", "method": "GET", "scheme": "http",
            "path": "/v/media/online/mg/preset.m3u8", "query_string": b"from=mr",
            "headers": [(b"user-agent", IOS_UA.encode()),
                        (b"host", b"jan0815.fnos.net:5666"),
                        (b"x-forwarded-proto", b"https")],
        })
        text = appmod._m3u8_resp(body, req).body.decode()
        seg = [l for l in text.splitlines() if l and not l.startswith("#")][0]
        self.assertTrue(seg.startswith("https://jan0815.fnos.net:5666/v/media/online/seg/"))
        self.assertRegex(seg, r"/seg/[0-9a-f]{16}\.ts$")
        # 绝对地址的路径部分仍命中短分片路由（去掉 origin 即可路由）。
        self.assertIn("/v/media/online/seg/", seg)

    def test_ios_media_playlist_has_terminated_final_line(self):
        # RFC 8216：最后的 #EXT-X-ENDLIST 或分片 URI 也必须以换行结束。
        body = (b"#EXTM3U\n#EXTINF:2.0,\n"
                b"/v/media/online/mg/seg?i=1\n#EXT-X-ENDLIST")
        response = appmod._m3u8_resp(body, _playlist_request(IOS_UA))
        self.assertTrue(response.body.endswith(b"#EXT-X-ENDLIST\n"))
        self.assertEqual(response.headers["Content-Length"], str(len(response.body)))
        self.assertRegex(response.body.decode(),
                         r"/v/media/online/seg/[0-9a-f]{16}\.ts\n")

        # 非 iOS 清单继续逐字节保持原样，不为此次兼容修复改其缓存契约。
        tv = appmod._m3u8_resp(body, _playlist_request(TV_UA))
        self.assertEqual(tv.body, body)

    def test_ios_placeholder_connection_close(self):
        ios = appmod._placeholder_ts_response(_playlist_request(IOS_UA))
        self.assertEqual(ios.headers.get("Connection"), "close")
        tv = appmod._placeholder_ts_response(_playlist_request(TV_UA))
        self.assertNotIn("Connection", tv.headers)

    async def test_ios_segment_conditions_and_response_headers(self):
        import base64
        token = base64.urlsafe_b64encode(b"https://cdn.test/a.ts").decode().rstrip("=")
        request = _playlist_request(IOS_UA)
        request.scope["path"] = "/v/media/online/mg/seg"
        request.scope["query_string"] = f"u={token}".encode()
        request.scope["headers"].extend([
            (b"range", b"bytes=0-1"), (b"if-range", b'"abc"'),
            (b"if-none-match", b'"old"')])
        upstream = types.SimpleNamespace(
            status_code=206, content=b"TS",
            headers={"content-type": "video/mp2t", "content-range": "bytes 0-1/2",
                     "accept-ranges": "bytes", "etag": '"abc"',
                     "x-upstream": "remove"}, aread=AsyncMock(), aclose=AsyncMock())
        with patch.object(appmod, "_await_pending_chart", new=AsyncMock()), \
                patch.object(appmod, "_pick_episode", return_value={"headers": {}}), \
                patch.object(appmod, "ext_get_stream_retry",
                             new=AsyncMock(return_value=upstream)) as fetch:
            response = await appmod.hls_seg("mg", request)
        fwd = fetch.await_args.args[2]
        self.assertEqual(fwd["Range"], "bytes=0-1")
        self.assertEqual(fwd["if-range"], '"abc"')
        self.assertEqual(fwd["if-none-match"], '"old"')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=3600")
        self.assertEqual(response.headers["Connection"], "close")
        self.assertEqual(response.headers["etag"], '"abc"')
        self.assertEqual(response.headers["content-range"], "bytes 0-1/2")
        self.assertNotIn("x-upstream", response.headers)
        self.assertEqual(response.headers["Content-Length"], "2")

        tv_headers = appmod._segment_response_headers(
            _playlist_request(TV_UA), upstream.headers)
        self.assertNotIn("Cache-Control", tv_headers)
        self.assertNotIn("Connection", tv_headers)
        self.assertIn("x-upstream", tv_headers)

    async def test_ios_stream_preserves_length_and_png_gets_new_entity(self):
        def upstream_with(chunks):
            def aiter_raw(_size):
                async def generate():
                    for chunk in chunks:
                        yield chunk
                return generate()
            return types.SimpleNamespace(
                status_code=206, content=b"".join(chunks), aiter_raw=aiter_raw,
                headers={"content-type": "video/mp2t", "content-length": "2",
                         "content-range": "bytes 0-1/2", "accept-ranges": "bytes",
                         "etag": '"abc"'}, aread=AsyncMock(), aclose=AsyncMock())

        request = _playlist_request(IOS_UA)
        request.scope["query_string"] = (
            "u=" + __import__("base64").urlsafe_b64encode(
                b"https://cdn.test/a.ts").decode().rstrip("=")).encode()
        for chunks, transformed in (([b"T", b"S"], False),
                                    ([b"\x89PNG\r\n\x1a\npayload", b"tail"], True)):
            with self.subTest(transformed=transformed):
                upstream = upstream_with(chunks)
                with patch.object(appmod, "_await_pending_chart", new=AsyncMock()), \
                        patch.object(appmod, "_pick_episode", return_value={"headers": {}}), \
                        patch.object(appmod, "ext_get_stream_retry",
                                     new=AsyncMock(return_value=upstream)), \
                        patch("quality._unwrap_segment", return_value=(b"TS", None)):
                    response = await appmod.hls_seg("mg", request)
                body = getattr(response, "body", None)
                if body is None:
                    body = b"".join([part async for part in response.body_iterator])
                self.assertEqual(body, b"TS")
                self.assertEqual(response.headers["Content-Length"], "2")
                self.assertEqual(response.headers["Connection"], "close")
                if transformed:
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("etag", response.headers)
                    self.assertNotIn("content-range", response.headers)
                    self.assertNotIn("accept-ranges", response.headers)
                else:
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.headers["etag"], '"abc"')
                    self.assertEqual(response.headers["content-range"], "bytes 0-1/2")

    async def test_ios_decrypted_segment_drops_upstream_etag(self):
        import base64
        query = urllib.parse.urlencode({
            "u": base64.urlsafe_b64encode(b"https://cdn.test/a.ts").decode().rstrip("="),
            "dec": "1", "k": base64.urlsafe_b64encode(b"00" * 16).decode().rstrip("="),
            "iv": base64.urlsafe_b64encode(b"01" * 16).decode().rstrip("=")})
        request = _playlist_request(IOS_UA)
        request.scope["query_string"] = query.encode()
        upstream = types.SimpleNamespace(
            status_code=200, content=b"cipher",
            headers={"content-type": "video/mp2t", "etag": '"cipher"'})
        plain = b"\x47" + b"\x00" * 187
        with patch.object(appmod, "_await_pending_chart", new=AsyncMock()), \
                patch.object(appmod, "_pick_episode", return_value={"headers": {}}), \
                patch.object(appmod, "ext_get_retry",
                             new=AsyncMock(return_value=upstream)), \
                patch("quality._aes_cbc_decrypt", new=AsyncMock(return_value=plain)):
            response = await appmod.hls_seg("mg", request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, plain)
        self.assertNotIn("etag", response.headers)
        self.assertNotIn("content-range", response.headers)

    async def test_ios_compact_segment_conditions_and_response_headers(self):
        request = _playlist_request(IOS_UA)
        request.scope["path"] = "/v/media/online/mg/hls"
        request.scope["query_string"] = b"r=r1&i=2"
        request.scope["headers"].extend([
            (b"range", b"bytes=0-1"), (b"if-range", b'"abc"'),
            (b"if-none-match", b'"old"')])
        upstream = types.SimpleNamespace(
            status_code=206, content=b"TS",
            headers={"content-type": "video/mp2t", "etag": '"abc"'})
        with patch.object(appmod, "_hls_reg_get", return_value="https://cdn.test/a.ts"), \
                patch.object(appmod, "ext_get_retry",
                             new=AsyncMock(return_value=upstream)) as fetch:
            response = await appmod.hls_compact("mg", request)
        fwd = fetch.await_args.args[2]
        self.assertEqual(fwd["if-range"], '"abc"')
        self.assertEqual(fwd["if-none-match"], '"old"')
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=3600")
        self.assertEqual(response.headers["Connection"], "close")
        self.assertEqual(response.headers["etag"], '"abc"')

    def test_hls_range_requests_receive_complete_playlist(self):
        # 即使 Range 合法、越界或格式错误，文本清单都不能被切成 #E 等不可解析片段。
        for value in ("bytes=0-1", "bytes=-5", "bytes=7-", "bytes=99999-",
                      "bytes=5-2", "bytes=-0", "bytes=bad"):
            with self.subTest(value=value):
                request = _request(IOS_UA)
                request.scope["headers"].append((b"range", value.encode()))
                response = appmod._m3u8_resp(
                    "0123456789", request,
                    headers={"Content-Range": "bytes 0-1/10",
                             "Accept-Ranges": "bytes"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.body, b"0123456789")
                self.assertEqual(response.headers["Content-Length"], "10")
                self.assertNotIn("Content-Range", response.headers)
                self.assertNotIn("Accept-Ranges", response.headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
