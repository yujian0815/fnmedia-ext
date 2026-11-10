"""panqr：把网盘登录二维码烧录进 HLS 占位 TS 分片的契约。"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import panqr
from PIL import Image


def _png(w: int = 200, h: int = 200) -> bytes:
    im = Image.new("1", (w, h), 255)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipUnless(panqr.ffmpeg_available(), "ffmpeg 不可用")
class PanQrTsTests(unittest.TestCase):
    def test_build_returns_valid_mpegts(self):
        ts = panqr.build_login_ts(_png(), "夸克网盘登录", "扫码后自动播放")
        self.assertIsNotNone(ts)
        # MPEG-TS 同步字节 0x47
        self.assertEqual(ts[0], 0x47)
        self.assertGreater(len(ts), 1000)

    def test_cached_result_identical(self):
        png = _png()
        a = panqr.build_login_ts(png, "t", "s")
        b = panqr.build_login_ts(png, "t", "s")
        self.assertEqual(a, b)

    def test_empty_input_returns_none(self):
        self.assertIsNone(panqr.build_login_ts(b"", "t", "s"))

    def test_async_wrapper(self):
        import asyncio
        ts = asyncio.run(panqr.build_login_ts_async(_png(), "t", "s"))
        self.assertIsNotNone(ts)
        self.assertEqual(ts[0], 0x47)


if __name__ == "__main__":
    unittest.main()
