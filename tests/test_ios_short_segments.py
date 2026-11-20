"""iOS 短分片 ID 的映射边界和原端点无差异转发。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402
import hls_short  # noqa: E402


class ShortSegmentMappingTests(unittest.TestCase):
    def setUp(self):
        hls_short._SEGS.clear()

    def tearDown(self):
        hls_short._SEGS.clear()

    def test_mapping_is_deterministic_and_expires(self):
        sid = hls_short.put_segment("mg", "seg", "u=a&dec=1", now=100)
        self.assertRegex(sid, r"^[0-9a-f]{16}$")
        self.assertEqual(
            hls_short.put_segment("mg", "seg", "u=a&dec=1", now=101), sid)
        self.assertEqual(hls_short.get_segment(sid, now=102),
                         ("mg", "seg", "u=a&dec=1"))
        self.assertIsNone(hls_short.get_segment(sid, now=101 + hls_short.SEG_TTL))

    def test_capacity_evicts_oldest(self):
        with patch.object(hls_short, "SEG_MAX", 2):
            first = hls_short.put_segment("mg", "seg", "u=1", now=1)
            second = hls_short.put_segment("mg", "seg", "u=2", now=2)
            third = hls_short.put_segment("mg", "seg", "u=3", now=3)
        self.assertIsNone(hls_short.get_segment(first, now=4))
        self.assertEqual(hls_short.get_segment(second, now=4)[2], "u=2")
        self.assertEqual(hls_short.get_segment(third, now=4)[2], "u=3")

    def test_rewrite_preserves_original_media_guid(self):
        body = b"#EXTM3U\n/v/media/online/original/seg?u=a&line=l\n"
        out = hls_short.shorten_playlist(body, "request-mg").decode()
        uri = out.splitlines()[1]
        self.assertTrue(uri.startswith("/v/media/online/seg/"))
        sid = uri.rsplit("/", 1)[1].removesuffix(".ts")
        self.assertEqual(hls_short.get_segment(sid),
                         ("original", "seg", "u=a&line=l"))


class ShortSegmentRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hls_short._SEGS.clear()

    def tearDown(self):
        hls_short._SEGS.clear()

    @staticmethod
    def _request(path: str) -> Request:
        return Request({
            "type": "http", "method": "GET", "scheme": "https",
            "server": ("media.test", 443), "path": path,
            "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"host", b"media.test"),
                        (b"user-agent", b"Runner/1.13.0"),
                        (b"range", b"bytes=0-1")],
            "state": {"_play_mg": "mg"},
        })

    async def test_short_route_restores_segment_query_headers_and_state(self):
        sid = hls_short.put_segment("mg", "seg", "u=abc&dec=1&k=kk")
        expected = Response(b"ts", status_code=206)
        handler = AsyncMock(return_value=expected)
        request = self._request(f"/v/media/online/seg/{sid}.ts")
        with patch.object(appmod, "hls_seg", new=handler):
            response = await appmod.hls_short_seg_root(sid, request)
        self.assertIs(response, expected)
        mg, forwarded = handler.await_args.args
        self.assertEqual(mg, "mg")
        self.assertEqual(forwarded.url.path, "/v/media/online/mg/seg")
        self.assertEqual(forwarded.scope["query_string"], b"u=abc&dec=1&k=kk")
        self.assertEqual(forwarded.headers["range"], "bytes=0-1")
        self.assertEqual(forwarded.state._play_mg, "mg")

    async def test_short_route_restores_compact_endpoint(self):
        sid = hls_short.put_segment("mg", "hls", "r=rid&i=7")
        handler = AsyncMock(return_value=Response(b"compact"))
        request = self._request(f"/v/media/online/seg/{sid}.ts")
        with patch.object(appmod, "hls_compact", new=handler):
            response = await appmod.hls_short_seg_root(sid, request)
        self.assertEqual(response.status_code, 200)
        _, forwarded = handler.await_args.args
        self.assertEqual(forwarded.url.path, "/v/media/online/mg/hls")
        self.assertEqual(forwarded.query_params["r"], "rid")
        self.assertEqual(forwarded.query_params["i"], "7")

    async def test_unknown_expired_and_wrong_media_return_404(self):
        valid = hls_short.put_segment("mg", "seg", "u=valid")
        for mg, sid in (("mg", "0" * 16), ("other", valid)):
            with self.subTest(mg=mg, sid=sid):
                response = await appmod.hls_short_seg(
                    mg, sid, self._request(f"/v/media/online/{mg}/seg/{sid}.ts"))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await appmod.hls_short_seg_root(
            "0" * 16, self._request(f"/v/media/online/seg/{'0' * 16}.ts"))
        self.assertEqual(response.status_code, 404)
        sid = hls_short.put_segment("mg", "seg", "u=old", now=0)
        response = await appmod.hls_short_seg_root(
            sid, self._request(f"/v/media/online/seg/{sid}.ts"))
        self.assertEqual(response.status_code, 404)

    async def test_short_routes_are_registered_with_legacy_alias(self):
        sid = hls_short.put_segment("mg", "seg", "u=abc")
        with patch.object(appmod, "hls_seg",
                          new=AsyncMock(side_effect=[Response(b"ok"), Response(b"ok")])):
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=appmod.app, raise_app_exceptions=False),
                    base_url="http://test") as client:
                for path in (f"/v/media/online/seg/{sid}.ts",
                             f"/v/media/online/mg/seg/{sid}.ts"):
                    with self.subTest(path=path):
                        response = await client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.content, b"ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
