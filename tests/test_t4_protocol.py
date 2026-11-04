"""Offline module contracts; no production services or credentials."""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import providers
import t4
import tvbox

SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}


class T4ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_uses_stable_identity_and_preserves_media_type(self):
        card = t4.map_title({"vod_id": "series-1", "vod_name": "Series"}, SOURCE)

        def handler(request):
            self.assertEqual(request.url.params["ids"], "series-1")
            return httpx.Response(200, json={"list": [{
                "vod_id": "series-1", "vod_name": "Series", "type_name": "Cloud",
                "vod_play_from": "A", "vod_play_url": "S01E01$opaque-id"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            title = await t4.detail(client, SOURCE, card.locator,
                                    fallback={"type_name": "\u7535\u89c6\u5267"})
        self.assertEqual(title.type_name, "\u7535\u89c6\u5267")
        self.assertTrue(title.sources["A"][0].t4)
        self.assertEqual(title.sources["A"][0].url, "opaque-id")

    def test_new_model_fields_do_not_change_cms_defaults(self):
        episode = providers.Episode("01", "https://media.test/v.mp4", True)
        self.assertFalse(episode.t4)
        self.assertEqual(episode.episode_key, "")

    def test_upstream_source_priorities_survive_t4_integration(self):
        self.assertEqual(providers.source_priority({"id": "ffzy"}), 45)
        self.assertEqual(providers.source_priority({"id": "ffzy"}, {"ffzy": 7}), 7)
        self.assertEqual(providers.source_priority(SOURCE), 0)


    async def test_search_is_lazy_and_preserves_api_query(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.params["token"], "test")
            self.assertNotIn("ac", request.url.params)
            return httpx.Response(200, json={"list": [
                {"vod_id": "movie-1", "vod_name": "Test"},
                {"vod_id": "folder-1", "vod_name": "Folder", "vod_tag": "folder"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            titles = await t4.search(client, SOURCE, "Test")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].sources, {})
        self.assertEqual(titles[0].locator["vod_id"], "movie-1")


    async def test_post_and_structured_ext(self):
        source = dict(SOURCE, method="POST", ext={"site": "test"})

        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.params["token"], "test")
            body = json.loads(request.content)
            self.assertEqual(json.loads(body["ext"]), {"site": "test"})
            self.assertEqual(body["play"], "opaque&id=1")
            self.assertEqual(body["flag"], "A & B")
            return httpx.Response(200, json={"url": "https://media.test/v.mp4",
                                             "parse": "0", "header": '{"Referer":"test"}'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await t4.player(client, source, "opaque&id=1", "A & B")
        self.assertEqual(result.wrapper, "MP4")
        self.assertEqual(result.headers, {"Referer": "test"})


    async def test_nonsearchable_does_not_request(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: self.fail("must not search"))) as client:
            self.assertEqual(await t4.search(client, dict(SOURCE, searchable="0"), "Test"), [])


    async def test_errors_are_redacted(self):
        def handler(request):
            return httpx.Response(401, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(t4.T4Error) as raised:
                await t4.request(client, SOURCE, {})
        self.assertNotIn("token=", str(raised.exception))
        self.assertNotIn("http://", str(raised.exception))


    def test_playback_contracts(self):
        for suffix, wrapper in [("m3u8", "HLS"), ("mp4", "MP4"), ("mkv", "MKV"),
                                ("mpd", "DASH")]:
            with self.subTest(suffix=suffix):
                self.assertEqual(t4.playback({"url": f"https://media.test/v.{suffix}"},
                                            SOURCE["api"]).wrapper, wrapper)
        self.assertEqual(t4.playback({"url": "/media/1", "type": "video/mp4"},
                                    SOURCE["api"]).url, "http://t4.test/media/1")
        for result in [
            {"url": "https://media.test/share/x", "parse": 1},
            {"url": "https://media.test/v.mp4", "jx": 1},
            {"url": "push://share"},
            {"url": "<MPD></MPD>"},
            {"url": ["1080p", "https://media.test/v.m3u8"]},
            {"url": "https://media.test/v.mp4", "header": '{"bad":"a\\nb"}'},
        ]:
            with self.subTest(result=result), self.assertRaises(t4.T4Error):
                t4.playback(result, SOURCE["api"])


    def test_cms_regression(self):
        status, source = tvbox.classify_site({"type": 1, "name": "CMS",
                                               "api": "https://cms.test/api.php/provide/vod"})
        self.assertEqual(status, "add_xml")
        self.assertTrue(source["api"].endswith("/at/json"))
        episodes = providers._parse_play("A", "", "01$https://media.test/1.m3u8#"
                                         "02$https://media.test/2.mp4", "https://cms.test/")
        self.assertEqual(len(episodes["A"]), 2)
        self.assertTrue(all(e.direct for e in episodes["A"]))
        self.assertEqual(providers._parse_play("A", "", "01$opaque", ""), {})
