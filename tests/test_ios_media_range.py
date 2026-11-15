"""新版 iOS media/range：HLS 必须进入单线路 main，而非停在 preset 主清单。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


IOS_UA = "1.12.2 (com.trim.media.ios; build:1122011; iOS 27.2) Flutter/3.11.5"
TV_UA = "1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0"


def _request(ua: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/v/api/v1/media/range/mg",
                    "query_string": b"", "headers": [(b"user-agent", ua.encode())]})


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

    async def test_ios_hls_serves_single_line_main_directly(self):
        target = _target()
        main = AsyncMock(return_value=Response(
            "#EXTM3U\n#EXT-X-ENDLIST\n", media_type="application/vnd.apple.mpegurl"))
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None), \
                patch.object(appmod.store, "target_lines", return_value=[target]), \
                patch.object(appmod, "_warm_preset_lines"), \
                patch.object(appmod, "_warm_playlist"), \
                patch.object(appmod, "start_fill_lines"), \
                patch.object(appmod, "hls_main", main):
            response = await appmod.media_range("mg", _request(IOS_UA))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"#EXTM3U", response.body)
        main_request = main.await_args.args[1]
        self.assertEqual(main_request.url.path, "/v/media/online/mg/main.m3u8")
        self.assertIn("line=lzi%3A%3Alzm3u8", main_request.url.query)

    async def test_tv_hls_keeps_preset_master(self):
        location = await self._location(TV_UA)
        self.assertIn("/preset.m3u8?", location)
        self.assertNotIn("/main.m3u8?", location)

    async def test_pending_chart_ios_keeps_placeholder_preset(self):
        with patch.object(appmod, "_fixed_media_target", return_value=None), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None), \
                patch.object(appmod, "chart_guid_by_media", return_value="chart-og"), \
                patch.object(appmod, "start_resolve_chart"):
            response = await appmod.media_range("mg", _request(IOS_UA))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/preset.m3u8?", response.headers["location"])

    async def test_ios_mp4_still_uses_direct(self):
        target = dict(_target(), url="https://cdn.test/v.mp4", m3u8=False)
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=None):
            response = await appmod.media_range("mg", _request(IOS_UA))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/direct?", response.headers["location"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
