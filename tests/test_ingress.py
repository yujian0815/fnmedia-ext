"""Public HTTPS ingress must preserve the existing virtual-library contract."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import fnmedia_authx


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        def official(request):
            self.requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": []})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(official))
        self.addAsyncCleanup(self.upstream.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="https://media.example.com")
        self.addAsyncCleanup(self.client.aclose)
        for name, value in (
            ("up_client", lambda request: self.upstream),
            ("charts_enabled", lambda: True),
            ("catalog_enabled", lambda: False),
            ("standalone_sources_active", lambda: []),
            ("chart_cache", SimpleNamespace(items=lambda cat: [])),
        ):
            item = patch.object(appmod, name, value)
            item.start()
            self.addCleanup(item.stop)

    async def test_https_and_client_profiles_keep_virtual_libraries(self):
        expected = set(appmod.CHART_LIB_GUIDS.values())
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                response = await self.client.get("/v/api/v1/mediadb/list", headers={
                    "User-Agent": ua, "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "media.example.com"})
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["code"], 0)
                self.assertEqual({row["guid"] for row in payload["data"]}, expected)
                for guid in expected:
                    detail = await self.client.get("/v/api/v1/mdb/" + guid,
                                                   headers={"User-Agent": ua})
                    self.assertEqual(detail.json()["code"], 0)
                    self.assertEqual(detail.json()["data"]["guid"], guid)

    async def test_proxy_preserves_signature_path_query_and_authentication(self):
        path = "/v/api/v1/mediadb/list"
        params = {"type": "Movie", "page": 1}
        signature, query = fnmedia_authx.make_authx("GET", path, params=params)
        response = await self.client.get(path + "?" + query, headers={
            "authx": signature, "Authorization": "fixture-session",
            "X-Forwarded-Proto": "https", "Trim-Host": "media.example.com"})
        self.assertEqual(response.status_code, 200)
        forwarded = self.requests[-1]
        self.assertEqual(forwarded.url.path, path)
        self.assertEqual(forwarded.url.query.decode(), query)
        self.assertEqual(forwarded.headers["authx"], signature)
        self.assertEqual(forwarded.headers["Authorization"], "fixture-session")
        self.assertEqual(forwarded.headers["Trim-Host"], "media.example.com")


if __name__ == "__main__":
    unittest.main()
