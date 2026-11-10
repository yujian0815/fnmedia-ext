"""混合直连（播放端直连源站分片，不经过 NAS 中转）回归。

约束：默认关闭=完全保持 v0.2.42 全量中转；仅普通无加密/无广告/非紧凑 HLS 且
裸连探测成功时才把分片改写为源站绝对地址；任何不确定都回退中转。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402

M3U8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:5.0,\nhttps://cdn.test/seg1.ts\n"
        "#EXTINF:5.0,\nhttps://cdn.test/seg2.ts\n#EXT-X-ENDLIST\n")

PICKED = {"url": "https://cdn.test/list.m3u8", "headers": {"Referer": "https://cdn.test"},
          "line": "lzi::lzm3u8", "index": 0, "m3u8": True}


def _req():
    return Request({"type": "http", "method": "GET",
                    "path": "/v/media/online/mg/main.m3u8",
                    "query_string": b"index=0&line=lzi%3A%3Alzm3u8&auto=1",
                    "headers": [], "app": appmod.app})


class DirectPlayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        appmod._DIRECT_HOST_CACHE.clear()

    async def _build(self, enabled, eligible):
        cfg = {"direct_play_enabled": enabled, "direct_play_probe": True,
               "direct_play_probe_ttl": 1800}
        with patch.object(appmod.config, "get", return_value=cfg), \
                patch.object(appmod, "_fetch_playlist",
                             new=AsyncMock(return_value=(M3U8, "https://cdn.test/list.m3u8"))), \
                patch.object(appmod, "_resolve_if_needed", new=AsyncMock()), \
                patch.object(appmod, "_direct_eligible", new=AsyncMock(return_value=eligible)):
            return await appmod._build_playlist_body("mg", dict(PICKED), _req())

    async def test_disabled_keeps_proxy_segments(self):
        body, _ = await self._build(enabled=False, eligible=True)
        # 默认关闭：分片必须仍是本服务 /seg 代理地址，不出现源站直连
        self.assertIn("/v/media/online/mg/seg?u=", body)
        self.assertNotIn("https://cdn.test/seg1.ts", body)

    async def test_enabled_and_eligible_emits_origin_segments(self):
        body, _ = await self._build(enabled=True, eligible=True)
        # 直连生效：分片直接是源站绝对地址，无 /seg 中转
        self.assertIn("https://cdn.test/seg1.ts", body)
        self.assertIn("https://cdn.test/seg2.ts", body)
        self.assertNotIn("/v/media/online/mg/seg?u=", body)

    async def test_enabled_but_ineligible_falls_back_to_proxy(self):
        body, _ = await self._build(enabled=True, eligible=False)
        # 探测失败：安全回退中转，保证可播
        self.assertIn("/v/media/online/mg/seg?u=", body)
        self.assertNotIn("\nhttps://cdn.test/seg1.ts", body)

    async def test_encrypted_playlist_never_goes_direct(self):
        # AES 加密清单（能解析出密钥）必须始终中转，由 /seg 解密
        key_m3u8 = M3U8.replace(
            "#EXT-X-VERSION:3\n",
            '#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.test/key",IV=0x0\n')
        cfg = {"direct_play_enabled": True, "direct_play_probe": True}
        key = b"\0" * 16
        with patch.object(appmod.config, "get", return_value=cfg), \
                patch.object(appmod, "_fetch_playlist",
                             new=AsyncMock(return_value=(key_m3u8, "https://cdn.test/list.m3u8"))), \
                patch.object(appmod, "_resolve_if_needed", new=AsyncMock()), \
                patch("quality._parse_hls_key", new=AsyncMock(return_value=(key, b"\0" * 16))):
            body, _ = await appmod._build_playlist_body("mg", dict(PICKED), _req())
        self.assertNotIn("\nhttps://cdn.test/seg1.ts", body)
        # 加密线移除了 EXT-X-KEY 并走 dec=1 中转
        self.assertNotIn("EXT-X-KEY", body)
        self.assertIn("dec=1", body)


if __name__ == "__main__":
    unittest.main()
