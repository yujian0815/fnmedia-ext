"""海报代理回源前的 URL 规范化契约。

红果 human/comic/ai 分类封面是 reading-sign.fqnovelpic.com 的签名 HEIC
（x-expires/x-signature）：Web 端无法渲染 HEIC，签名到期还会 403。海报代理
须在回源前把这类 novel-pic 封面改写为无签名、永久有效的 byteimg JPEG，使
已缓存 brief（token 内仍是旧 URL）也立即生效，且对 Web/iOS/TV 统一。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


FQ_HEIC = ("https://p3-reading-sign.fqnovelpic.com/novel-pic/"
           "263a36941b6075493785aeff4a2c14c8~tplv-81nmtwyey9-superreso-aifit:600:0.heic"
           "?lk3s=ae45642e&x-expires=1800349873&x-signature=AZ9%2F04eKFhivEA8LyUJKspDOmYU%3D")
BYTEIMG = ("https://p6-novel.byteimg.com/novel-pic/"
           "899ba52ee34a534d9be948a093468ed1~tplv-shrink:640:0.image")


def _tok(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/online/x.jpg",
                    "query_string": b"", "headers": []})


class NormalizePosterUrlTests(unittest.TestCase):
    def test_fqnovelpic_signed_heic_rewritten_to_byteimg_jpeg(self):
        out = appmod._normalize_poster_url(FQ_HEIC)
        self.assertEqual(
            out,
            "https://p3-novel.byteimg.com/novel-pic/"
            "263a36941b6075493785aeff4a2c14c8~tplv-shrink:640:0.image")
        self.assertNotIn("x-signature", out)
        self.assertNotIn("x-expires", out)
        self.assertNotIn(".heic", out)

    def test_fqnovelpic_id_without_tplv_suffix(self):
        u = "https://p3-reading-sign.fqnovelpic.com/novel-pic/abc123DEF?x-expires=1"
        self.assertEqual(
            appmod._normalize_poster_url(u),
            "https://p3-novel.byteimg.com/novel-pic/abc123DEF~tplv-shrink:640:0.image")

    def test_fqnovelpic_bare_id_no_query(self):
        u = "https://p3-reading-sign.fqnovelpic.com/novel-pic/abc123DEF"
        self.assertEqual(
            appmod._normalize_poster_url(u),
            "https://p3-novel.byteimg.com/novel-pic/abc123DEF~tplv-shrink:640:0.image")

    def test_byteimg_unchanged(self):
        self.assertEqual(appmod._normalize_poster_url(BYTEIMG), BYTEIMG)

    def test_other_hosts_unchanged(self):
        for u in ("https://img3.doubanio.com/view/photo/m_ratio_poster/public/p1.webp",
                  "https://image.tmdb.org/t/p/w1280/abc.jpg",
                  "https://example.com/cover.png"):
            self.assertEqual(appmod._normalize_poster_url(u), u)

    def test_fqnovelpic_non_novel_pic_unchanged(self):
        u = "https://p3-reading-sign.fqnovelpic.com/other/path.heic?x=1"
        self.assertEqual(appmod._normalize_poster_url(u), u)

    def test_empty_and_non_http_unchanged(self):
        self.assertEqual(appmod._normalize_poster_url(""), "")
        self.assertEqual(appmod._normalize_poster_url("/local/cover.jpg"), "/local/cover.jpg")


class ServeOnlinePosterNormalizationTests(unittest.TestCase):
    def test_serve_rewrites_fqnovelpic_before_fetch(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        fetch = AsyncMock(return_value=httpx.Response(
            200, content=jpeg, headers={"content-type": "image/jpeg"}))
        with patch.object(appmod, "_poster_fetch", fetch):
            resp = asyncio.run(appmod._serve_online_poster(_tok(FQ_HEIC) + ".jpg", _request()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "image/jpeg")
        self.assertEqual(resp.body, jpeg)
        # 回源收到的必须是规范化后的 byteimg URL，而非原签名 HEIC。
        fetched_url = fetch.await_args.args[1]
        self.assertEqual(
            fetched_url,
            "https://p3-novel.byteimg.com/novel-pic/"
            "263a36941b6075493785aeff4a2c14c8~tplv-shrink:640:0.image")

    def test_serve_byteimg_token_fetches_original(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        fetch = AsyncMock(return_value=httpx.Response(
            200, content=jpeg, headers={"content-type": "image/jpeg"}))
        with patch.object(appmod, "_poster_fetch", fetch):
            resp = asyncio.run(appmod._serve_online_poster(_tok(BYTEIMG) + ".jpg", _request()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fetch.await_args.args[1], BYTEIMG)


if __name__ == "__main__":
    unittest.main()