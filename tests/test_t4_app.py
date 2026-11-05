"""Exercise real FastAPI routes with in-process T4/official/media fixtures."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import store as storemod
import t4_media
import t4_service


API = "http://t4.test/api/demo?token=fixture"
MEDIA_BYTES = b"\x00\x00\x00\x18ftypisom" + (bytes(range(256)) * 4)[12:]


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE", Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [], "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        self.calls = []
        self.protected = False
        self.changed = False
        self.parse = 0
        self.wrapper = "mp4"
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (storemod, "store", self.store),
            (appmod, "t4_service", t4_service.T4Service(self.store, self.config)),
            (appmod, "t4_media", t4_media.T4Media()),
            (appmod, "_schedule_routes_refresh", lambda: None),
        ]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        item = patch.object(appmod.vlibs, "config", self.config)
        item.start()
        self.addCleanup(item.stop)
        self.network = httpx.AsyncClient(transport=httpx.MockTransport(self.handle),
                                        follow_redirects=True)
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.network
        self.addAsyncCleanup(appmod.t4_media.metadata.close)
        # Do not run lifespan: it starts production workers and config polling.
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                        base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        constructor = httpx.AsyncClient

        def network_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handle)
            return constructor(*args, **kwargs)

        item = patch("providers.httpx.AsyncClient", side_effect=network_client)
        item.start()
        self.addCleanup(item.stop)
        await self.import_source()

    def handle(self, request):
        self.calls.append(request)
        if request.url.host == "localhost":
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.url.host == "media.test":
            if self.protected:
                self.assertEqual(request.headers["Cookie"], "fixture-secret")
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(200, text=(
                    '#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MAP:URI="clip.mp4",BYTERANGE="16@0"\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16@16\nclip.mp4\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16\nclip.mp4\n#EXT-X-ENDLIST\n'),
                    headers={"Content-Type": "application/octet-stream"})
            data = MEDIA_BYTES
            if self.wrapper == "mkv":
                data = b"\x1a\x45\xdf\xa3\x80matroska" + data[13:]
            start, end = 0, len(data) - 1
            status = 200
            if "range" in request.headers:
                span = request.headers["range"].removeprefix("bytes=").split("-")
                start, end = int(span[0]), min(int(span[1]), len(data) - 1)
                status = 206
            return httpx.Response(status, stream=BytesStream(
                b"" if request.method == "HEAD" else data[start:end + 1]),
                                  headers={"Content-Type": "video/mp4", "Content-Length": str(end - start + 1),
                                           "Accept-Ranges": "bytes",
                                           "Content-Range": f"bytes {start}-{end}/{len(data)}"})
        self.assertEqual(request.url.host, "t4.test")
        self.assertEqual(request.url.params["token"], "fixture")
        params = request.url.params
        if "wd" in params:
            return httpx.Response(200, json={"list": [
                {"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
                 "vod_pic": "https://img.test/poster.jpg"}]})
        if "ids" in params:
            self.assertEqual(params["ids"], "movie-1")
            if hasattr(self, "detail_override"):
                return httpx.Response(200, json={"list": [self.detail_override]})
            return httpx.Response(200, json={"list": [{
                "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
                "vod_play_from": "A$$$B",
                "vod_play_url": ("第01集$a1#第02集$a2$$$第02集$b2#第01集$b1" if not self.changed else
                                 "第02集$a2-new#第01集$a1-new$$$第01集$b1-new#第02集$b2-new")
            }]})
        if "play" in params:
            return httpx.Response(200, json={
                "url": f"https://media.test/{params['play']}.{self.wrapper}",
                "parse": self.parse,
                "header": {"Cookie": "fixture-secret"} if self.protected else {}})
        return httpx.Response(200, json={"class": [], "list": []})

    async def import_source(self):
        response = await self.client.post("/v/api/ext/source/import-subscription",
                                          json={"text": json.dumps({"sites": [
                                              {"type": 4, "key": "demo", "name": "Test T4", "api": API},
                                              {"type": 3, "name": "Push", "api": "csp_Push"}]})})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["t4"]["candidates"], 1)

    async def search(self):
        response = await self.client.get("/v/api/v1/search/list", params={"q": "测试剧"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1)
        return rows[0]["guid"]

    async def load_episode(self):
        guid = await self.search()
        response = await self.client.get(f"/v/api/v1/item/{guid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["type"], "TV")
        seasons = (await self.client.get(f"/v/api/v1/season/list/{guid}")).json()["data"]
        episodes = (await self.client.get(f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
        return guid, next(e["guid"] for e in episodes if e["title"] == "第02集")

    async def test_search_detail_selection_and_public_direct(self):
        await self.search()
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params for r in self.calls))
        _, eg = await self.load_episode()
        self.assertEqual(len([r for r in self.calls if "ids" in r.url.params]), 1)
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        info = (await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})).json()["data"]
        stream = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertEqual(len(stream["qualities"]), 2)
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": info["media_guid"], "bitrate": stream["qualities"][1]["bitrate"]})
        data = response.json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["play_link"], "https://media.test/b2.mp4")
        self.assertEqual(data["wrapper"], "MP4")
        media_calls = [r for r in self.calls if r.url.host == "media.test"]
        self.assertEqual(len(media_calls), 1)
        self.assertEqual(media_calls[0].headers["Range"], "bytes=0-4095")
        local = await self.client.get("/v/api/v1/item/official-guid")
        self.assertEqual(local.json(), {"code": 0, "data": []})

    async def test_app_range_header_and_first_middle_last(self):
        self.protected = True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        for start, end in [(0, 15), (504, 519), (1008, 1023)]:
            with self.subTest(start=start):
                response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                                  headers={"Range": f"bytes={start}-{end}"},
                                                  follow_redirects=True)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/1024")
                self.assertEqual(len(response.content), 16)
                self.assertNotIn("fixture-secret", str(response.headers))
        response = await self.client.head(f"/v/api/v1/media/range/{mg}", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "1024")

    async def test_history_restart_reloads_ids_and_does_not_switch_episode(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        await self.client.post("/v/api/v1/play/record", json={
            "item_guid": eg, "media_guid": mg, "ts": 123, "duration": 3600})
        self.history.flush()
        self.store.flush()
        self.changed = True
        restored = storemod.Store()
        self.addCleanup(restored.close)
        history = histmod.History()
        with patch.object(appmod, "store", restored), patch.object(storemod, "store", restored), \
                patch.object(appmod, "history", history), \
                patch.object(appmod, "t4_service", t4_service.T4Service(restored, self.config)):
            response = await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})
            self.assertEqual(response.json()["data"]["ts"], 123)
            response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.json()["data"]["play_link"], "https://media.test/a2-new.mp4")

    async def test_explicit_unsupported_parse_and_dash(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        self.parse = 1
        response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.status_code, 502)
        self.assertIn("解析", response.json()["msg"])
        self.parse, self.wrapper = 0, "mpd"
        response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.status_code, 502)
        self.assertIn("DASH", response.json()["msg"])

    async def test_t4_stream_metadata_uses_native_raw_contract(self):
        _, eg = await self.load_episode()
        menu = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertEqual(menu["video_streams"][0]["wrapper"], "")
        self.assertEqual(menu["video_streams"][0]["codec_name"], "")
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        mg = storemod.ep_media_guid(eg)
        for wrapper in ("mp4", "mkv"):
            self.wrapper = wrapper
            appmod.t4_service._playbacks.clear()
            response = await self.client.post("/v/api/v1/stream", json={"media_guid": mg})
            data = response.json()["data"]
            self.assertEqual(data["video_stream"]["wrapper"], wrapper.upper())
            self.assertEqual(data["video_stream"]["codec_name"], "")
            self.assertEqual(data["cloud_storage_info"]["cloud_storage_type"], 9001)
            self.assertEqual(len(data["direct_link_qualities"]), 2)
            self.assertTrue(all(not q["is_m3u8"] for q in data["direct_link_qualities"]))
            self.assertTrue(all(q["url"].startswith("http://fnmedia.test/v/media/online/")
                                for q in data["direct_link_qualities"]))
            self.assertTrue(data["file_stream"]["file_name"].endswith("." + wrapper))

    async def test_native_strm_urls_are_absolute_and_preserve_proxy_origin_and_headers(self):
        self.protected = True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        profiles = ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                    "com.trim.media.ios")
        origins = (
            ({}, "http://fnmedia.test"),
            ({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "public.test:15666"},
             "https://public.test:15666"),
            ({"X-Forwarded-Proto": "https", "Host": "public.test:15666"},
             "https://public.test:15666"),
            ({"X-Forwarded-Proto": "https, http",
              "X-Forwarded-Host": "[2001:db8::1]:15666, upstream.test"},
             "https://[2001:db8::1]:15666"),
        )
        for ua in profiles:
            for headers, origin in origins:
                with self.subTest(ua=ua, origin=origin):
                    headers = dict(headers, **{"User-Agent": ua})
                    response = await self.client.post("/v/api/v1/stream",
                        json={"media_guid": mg}, headers=headers)
                    links = response.json()["data"]["direct_link_qualities"]
                    self.assertEqual(len(links), 2)
                    for quality in links:
                        self.assertTrue(quality["url"].startswith(
                            f"{origin}/v/media/online/{mg}/t4?"))
                        self.assertNotIn("fixture-secret", quality["url"])
                        response = await self.client.get(quality["url"],
                            headers={"Range": "bytes=504-519"}, follow_redirects=True)
                        self.assertEqual(response.status_code, 206)
                        self.assertEqual(response.content, MEDIA_BYTES[504:520])
                    played = await self.client.post("/v/api/v1/play/play",
                        json={"media_guid": mg}, headers=headers)
                    self.assertTrue(played.json()["data"]["play_link"].startswith(
                        f"{origin}/v/media/online/"))

    async def test_t4_hls_stream_stays_manifest_delivery(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/stream", json={
            "media_guid": storemod.ep_media_guid(eg)})
        data = response.json()["data"]
        self.assertEqual(data["video_stream"]["wrapper"], "HLS")
        self.assertIsNone(data["cloud_storage_info"])
        self.assertNotIn("direct_link_qualities", data)

    async def test_single_line_stream_is_ready_for_every_client_and_container(self):
        self.protected = True
        self.detail_override = {
            "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
            "vod_play_from": "A", "vod_play_url": "第01集$a1#第02集$a2"}
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        metadata = {"size": 12345, "video": {
            "codec_name": "hevc", "resolution_type": "4K", "bps": 9208081},
            "audio": []}
        for wrapper in ("mp4", "mkv", "m3u8"):
            self.wrapper = wrapper
            appmod.t4_service._playbacks.clear()
            for ua in ("com.trim.media.ios", "com.trim.tv",
                       "Dalvik/2.1.0 Android", "trimemedia-web"):
                with self.subTest(wrapper=wrapper, ua=ua), patch.object(
                        appmod.t4_media.metadata, "probe", return_value=metadata):
                    response = await self.client.post("/v/api/v1/stream",
                        json={"media_guid": mg}, headers={"User-Agent": ua})
                    self.assertEqual(response.status_code, 200)
                    data = response.json()["data"]
                    self.assertEqual(len(data["qualities"]), 1)
                    quality = data["qualities"][0]
                    self.assertEqual(quality["resolution"],
                                     "原画" if wrapper == "m3u8" else "4K")
                    self.assertEqual(quality["bitrate"], data["video_stream"]["bps"])
                    self.assertTrue(quality["progressive"])
                    if wrapper != "m3u8":
                        direct = data["direct_link_qualities"][0]
                        self.assertEqual({k: direct[k] for k in quality}, quality)
                        response = await self.client.get(direct["url"],
                            headers={"Range": "bytes=0-15"}, follow_redirects=True)
                        self.assertEqual(response.status_code, 206)

    async def test_slow_optional_metadata_cannot_hold_native_stream_response(self):
        self.protected = True
        self.detail_override = {
            "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
            "vod_play_from": "A", "vod_play_url": "第01集$a1#第02集$a2"}
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        unblock = asyncio.Event()

        async def slow(*_):
            await unblock.wait()
            return {}

        with patch.object(appmod, "T4_STREAM_READY_BUDGET", 0.02), \
                patch.object(appmod.t4_media.metadata, "probe",
                             new=AsyncMock(side_effect=slow)) as probe:
            with self.assertLogs("fnmedia", level="INFO") as logs:
                response = await asyncio.wait_for(self.client.post("/v/api/v1/stream",
                    json={"media_guid": mg}, headers={"User-Agent": "com.trim.media.ios"}),
                    timeout=0.5)
            data = response.json()["data"]
            self.assertEqual(data["video_stream"]["codec_name"], "")
            self.assertEqual(data["file_stream"]["size"], len(MEDIA_BYTES))
            self.assertEqual(len(data["qualities"]), 1)
            self.assertEqual(len(appmod.t4_media.metadata._pending), 1)
            self.assertTrue(any("elapsed_ms=" in row for row in logs.output))
            await self.client.post("/v/api/v1/stream", json={"media_guid": mg})
            probe.assert_awaited_once()
            response = await self.client.get(data["direct_link_qualities"][0]["url"],
                headers={"Range": "bytes=0-15"}, follow_redirects=True)
            self.assertEqual(response.status_code, 206)
            unblock.set()
            await appmod.t4_media.metadata.close()

    async def test_multiseason_first_middle_last_switch_next_and_resume_profiles(self):
        self.detail_override = {
            "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
            "vod_play_from": "A$$$B",
            "vod_play_url": (
                "S01E03(3 GB)$a3#S01E01(1 GB)$a1#S01E02(2 GB)$a2#"
                "S02E01(1 GB)$a21#S02E02(2 GB)$a22$$$"
                "第1集$b1#第3集$b3#S02E01$b21#S02E02$b22"),
        }
        guid = await self.search()
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                self.client.headers["User-Agent"] = ua
                seasons = (await self.client.get(f"/v/api/v1/season/list/{guid}")).json()["data"]
                self.assertEqual([s["season_number"] for s in seasons], [1, 2])
                self.assertEqual([s["number_of_episodes"] for s in seasons], [3, 2])
                for season in seasons:
                    detail = (await self.client.get(f"/v/api/v1/item/{season['guid']}")).json()["data"]
                    self.assertEqual(detail["season_number"], season["season_number"])
                    episodes = (await self.client.get(
                        f"/v/api/v1/episode/list/{season['guid']}")).json()["data"]
                    for i, ep in enumerate(episodes):
                        self.assertEqual(ep["episode_number"], i + 1)
                        info = (await self.client.post("/v/api/v1/play/info", json={
                            "item_guid": ep["guid"]})).json()["data"]
                        self.assertEqual(info["item"]["season_number"], season["season_number"])
                        mg = info["media_guid"]
                        stream = (await self.client.get(
                            f"/v/api/v1/stream/list/{ep['guid']}")).json()["data"]
                        choices = stream.get("qualities") or [{"resolution": "A", "bitrate": 90000000}]
                        self.assertEqual(len(choices), 1 if (
                            season["season_number"] == 1 and i == 1) else 2)
                        for quality in choices:
                            body = {"media_guid": mg, "bitrate": quality["bitrate"]}
                            played = (await self.client.post("/v/api/v1/play/play", json=body)).json()
                            prefix = "a" if quality["resolution"] == "A" else "b"
                            number = str(i + 1) if season["season_number"] == 1 else f"2{i + 1}"
                            self.assertEqual(played["data"]["play_link"],
                                             f"https://media.test/{prefix}{number}.mp4")
                # Completing an episode selects its actual successor, across a season boundary.
                s1 = (await self.client.get(
                    f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
                s2 = (await self.client.get(
                    f"/v/api/v1/episode/list/{seasons[1]['guid']}")).json()["data"]
                self.history.delete([row["item_guid"] for row in self.history.all()])
                await self.client.post("/v/api/v1/play/record", json={
                    "item_guid": s1[-1]["guid"], "ts": 990, "duration": 1000})
                info = (await self.client.post("/v/api/v1/play/info",
                                               json={"item_guid": guid})).json()["data"]
                self.assertEqual(info["guid"], s2[0]["guid"])
                self.assertEqual(info["ts"], 0)

    async def test_resume_latest_rewatched_episode_not_highest_episode_number(self):
        guid, second = await self.load_episode()
        first = self.store.tv_episode_guids(guid)[0]
        for ep, ts, updated in ((second, 300, 100), (first, 120, 200)):
            with patch("history.time.time", return_value=updated):
                await self.client.post("/v/api/v1/play/record", json={
                    "item_guid": ep, "ts": ts, "duration": 3600})
        data = (await self.client.post("/v/api/v1/play/info",
                                       json={"item_guid": guid})).json()["data"]
        self.assertEqual((data["guid"], data["ts"]), (first, 120))

    async def test_import_legacy_entry_and_source_test(self):
        response = await self.client.post("/v/api/ext/source/import-tvbox", json={
            "text": json.dumps({"sites": [{"type": 4, "name": "Other T4",
                                          "api": API + "&site=other"}]})})
        self.assertEqual(response.json()["data"]["t4"]["added"], 1)
        source = next(s for s in self.config.all_sources() if s.get("kind") == "t4")
        response = await self.client.post("/v/api/ext/source/test", json={"id": source["id"]})
        self.assertTrue(response.json()["data"]["ok"])
        self.assertFalse(any("wd" in r.url.params for r in self.calls))

    async def test_status_and_admin_entry(self):
        response = await self.client.get("/v/api/ext/status")
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/v/online-admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("TVBox / T4", response.text)

    async def test_season_episode_and_play_info_retain_title_metadata_all_clients(self):
        self.detail_override = {
            "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
            "vod_year": "2024", "vod_content": "原有简介", "vod_actor": "原有主演",
            "vod_director": "原有导演", "vod_area": "中国大陆", "vod_score": "8.5",
            "vod_play_from": "A", "vod_play_url": "第01集$a1#第02集$a2"}
        guid, eg = await self.load_episode()
        season = self.store.get_episode(eg)["season_guid"]
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            self.client.headers["User-Agent"] = ua
            for item in (eg, season):
                data = (await self.client.get(f"/v/api/v1/item/{item}")).json()["data"]
                self.assertIn("原有简介", data["overview"])
                self.assertIn("原有主演", data["overview"])
                self.assertTrue(data["posters"].startswith("/online/"))
                self.assertEqual(data["vote_average"], "8.5")
                self.assertTrue(data["release_date"].startswith("2024"))
            info = (await self.client.post("/v/api/v1/play/info",
                                           json={"item_guid": eg})).json()["data"]
            self.assertIn("原有导演", info["item"]["overview"])
            self.assertEqual(info["item"]["tv_title"], "测试剧")
        # A sparse subsequent detail must not erase good metadata from the same ID.
        self.detail_override = {
            "vod_id": "movie-1", "vod_name": "测试剧", "vod_play_from": "A",
            "vod_play_url": "第01集$a1#第02集$a2"}
        await appmod.t4_service.ensure_detail(self.network, guid, force=True)
        self.assertEqual(self.store.get_title_by_guid(guid)["actor"], "原有主演")

    async def test_stream_exposes_exact_metadata_without_fabricating_defaults(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        before = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertEqual(before["video_streams"][0]["bit_depth"], 0)
        self.assertEqual(before["video_streams"][0]["color_range_type"], "")
        metadata = {"size": 12345678, "video": {"codec_name": "hevc", "profile": "Main",
                    "width": 3840, "height": 2024, "duration": 2860.698, "bps": 4588470},
                    "audio": [{"index": 1, "codec_name": "aac", "channels": 2}]}
        with patch.object(appmod.t4_media.metadata, "probe", return_value=metadata):
            data = (await self.client.post("/v/api/v1/stream",
                                           json={"media_guid": mg})).json()["data"]
        self.assertEqual(data["file_stream"]["size"], metadata["size"])
        self.assertEqual(data["video_stream"]["codec_name"], "hevc")
        self.assertEqual(data["video_stream"]["width"], 3840)
        self.assertEqual(data["audio_streams"][0]["codec_name"], "aac")
        # Inspection remains network/container-only; no transcoded delivery.
        self.assertEqual(data["cloud_storage_info"]["cloud_storage_type"], 9001)
        self.assertNotIn("ffmpeg", str(data))

    async def test_cms_search_and_play_regression(self):
        self.config.save({"sources": [{
            "id": "cms", "name": "Fixture CMS", "api": "https://cms.test/provide/vod/at/json",
            "enabled": True}]})
        title = providers._map_vod({
            "vod_id": "1", "vod_name": "测试电影", "type_name": "电影",
            "vod_play_from": "A", "vod_play_url": "正片$https://media.test/movie.mp4"},
            self.config.all_sources()[0])
        with patch.object(providers, "search_all", return_value=[title]):
            response = await self.client.get("/v/api/v1/search/list", params={"q": "测试电影"})
        guid = response.json()["data"][0]["guid"]
        response = await self.client.get(f"/v/api/v1/item/{guid}")
        self.assertEqual(response.json()["data"]["type"], "Movie")
        response = await self.client.post("/v/api/v1/play/info", json={"item_guid": guid})
        info = response.json()["data"]
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": info["media_guid"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["wrapper"], "MP4")
        self.assertIn("/direct?", response.json()["data"]["play_link"])

    async def test_chart_search_does_not_bind_lazy_t4_cards(self):
        with patch.object(providers, "search_all", return_value=[]) as search:
            await appmod._aggregate_briefs("测试剧", include_t4=False)
        self.assertFalse(any(s.get("kind") == "t4" for s in search.call_args.args[1]))

    async def test_all_client_profiles(self):
        for ua in ("trimemedia-web", "1.4.2 (com.trim.tv; Android 35) okHttp/4.9.0",
                   "Dalvik/2.1.0 Android", "1.12.2 (com.trim.media.ios) Flutter/3.11"):
            with self.subTest(ua=ua):
                self.client.headers["User-Agent"] = ua
                _, eg = await self.load_episode()
                response = await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})
                info = response.json()["data"]
                self.assertEqual(response.status_code, 200)
                response = await self.client.post("/v/api/v1/play/play", json={
                    "media_guid": info["media_guid"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["wrapper"], "MP4")

    async def test_native_range_retains_each_official_line_selector(self):
        self.protected = True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        link = f"/v/media/online/{mg}/t4?line=B"
        for ua in ("com.trim.tv", "Dalvik/2.1.0 Android", "com.trim.media.ios"):
            for query, headers in [
                ({"direct_link_quality_index": "1"}, {}),
                ({"bitrate": "90010001"}, {}),
                ({"playlink": link}, {}),
                ({}, {"Play-Link": link}),
                ({"line": "B"}, {}),
            ]:
                with self.subTest(ua=ua, query=query, headers=headers):
                    self.client.headers["User-Agent"] = ua
                    appmod.t4_service._playbacks.clear()
                    response = await self.client.get(
                        f"/v/api/v1/media/range/{mg}", params=query,
                        headers={"Range": "bytes=32-47", **headers},
                        follow_redirects=True)
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.history, [])
                    self.assertEqual(response.content, MEDIA_BYTES[32:48])
                    played = [r for r in self.calls if "play" in r.url.params][-1]
                    self.assertEqual(played.url.params["play"], "b2")
                    self.assertEqual(played.url.params["flag"], "B")

    async def test_stream_and_play_use_same_selected_t4_line(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        # 新编码：bitrate 按线路名稳定哈希（跨片恒定），不再依赖菜单位置序号
        from proxy.app import _line_bitrate, _line_of_bitrate, _line_choices
        target = storemod.store.resolve_media_guid(mg)
        names = [c["line"] for c in _line_choices(target)]
        line_b = next(n for n in names if n.endswith("B") or n == "B") if \
            any(n.endswith("B") or n == "B" for n in names) else names[-1]
        bitrate_b = _line_bitrate(line_b)
        # 哈希编码在任意菜单里都能稳定解回同一条线
        self.assertEqual(_line_of_bitrate(bitrate_b, names), line_b)
        response = await self.client.post("/v/api/v1/stream", json={
            "media_guid": mg, "bitrate": bitrate_b})
        self.assertEqual(response.status_code, 200)
        played = [r for r in self.calls if "play" in r.url.params][-1]
        self.assertEqual(played.url.params["play"], "b2")
        data = response.json()["data"]
        self.assertEqual(data["video_stream"]["resolution_type"], "B")
        # 回显 bps 必须与清晰度菜单里该线路的 bitrate 一致（客户端据此正确高亮）
        menu_bps = {q["resolution"]: q["bitrate"] for q in data["qualities"]}
        self.assertEqual(data["video_stream"]["bps"], menu_bps["B"])
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "bitrate": bitrate_b})
        self.assertEqual(response.json()["data"]["play_link"], "https://media.test/b2.mp4")

    async def test_invalid_t4_line_does_not_silently_play_default(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        for path, query in [
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "99"}),
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "-1"}),
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "bad"}),
            (f"/v/media/online/{mg}/t4", {"line": "missing"}),
        ]:
            with self.subTest(query=query):
                response = await self.client.get(path, params=query, follow_redirects=True)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_native_range_does_not_follow_an_unowned_playlink(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": "https://unowned.test/video.mp4"},
                                         follow_redirects=False)
        self.assertNotIn("unowned.test", response.headers.get("location", ""))

    async def test_native_public_playlink_keeps_line_and_control_rpc(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "bitrate": 90010001})
        link = response.json()["data"]["play_link"]
        self.assertEqual(link, "https://media.test/b2.mp4")
        self.assertEqual(len([r for r in self.calls if r.url.host == "media.test"]), 1)
        self.calls.clear()
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": link})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], link)
        response = await self.client.post("/v/api/v1/media/p", json={
            "playLink": link, "reqid": "fixture", "method": "checkPlayLink"})
        self.assertEqual(response.json()["data"], {"reqid": "fixture", "result": "succ"})
        response = await self.client.post("/v/api/v1/media/p", json={
            "playLink": "https://unowned.test/video.mp4", "reqid": "unknown"})
        self.assertEqual(response.json(), {"code": 0, "data": []})
        self.assertFalse(any(r.url.host == "media.test" for r in self.calls))

    async def test_t4_range_rejects_playlink_for_another_episode(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": "/v/media/online/other/t4?line=B"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_hls_quality_rpc_switches_only_its_playback_session(self):
        self.wrapper, self.protected = "m3u8", True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        first = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        second = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        self.assertNotEqual(first, second)
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "reqid": "switch-b", "playLink": first,
            "quality": {"resolution": "B", "bitrate": 90010001}})
        self.assertEqual(response.json()["data"]["result"], "succ")
        for link, expected in ((first, "b2"), (second, "a2")):
            self.calls.clear()
            response = await self.client.get(link)
            self.assertEqual(response.status_code, 200)
            media = [r for r in self.calls if r.url.host == "media.test"][-1]
            self.assertEqual(media.url.path, f"/{expected}.m3u8")
        self.calls.clear()
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         headers={"Play-Link": first})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r for r in self.calls if r.url.host == "media.test"][-1].url.path,
                         "/b2.m3u8")
        # Switching back uses the same menu codes after selecting B.
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": first,
            "quality": {"resolution": "A", "bitrate": 90000000}})
        self.assertEqual(response.json()["data"]["result"], "succ")
        self.calls.clear()
        await self.client.get(first)
        self.assertEqual([r for r in self.calls if r.url.host == "media.test"][-1].url.path,
                         "/a2.m3u8")

    async def test_public_hls_session_redirects_without_proxying_media(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        self.assertEqual(len([r for r in self.calls if r.url.host == "media.test"]), 1)
        self.calls.clear()
        response = await self.client.get(response.json()["data"]["play_link"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://media.test/a2.m3u8")
        self.assertFalse(any(r.url.host == "media.test" for r in self.calls))

    async def test_hls_session_format_change_is_not_reported_as_success(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        link = response.json()["data"]["play_link"]
        self.wrapper = "mp4"
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": link,
            "quality": {"resolution": "B", "bitrate": 90010001}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["data"]["result"], "fail")

    async def test_hls_quality_rpc_rejects_unknown_transcoding_quality(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        link = response.json()["data"]["play_link"]
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": link,
            "quality": {"resolution": "720p", "bitrate": 2000000}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["data"]["result"], "fail")

    async def test_hls_quit_reclaims_session_without_affecting_other_player(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        first = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        second = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.quit", "playLink": first})
        self.assertEqual(response.json()["data"]["result"], "succ")
        self.assertEqual(len(appmod.t4_service._sessions), 1)
        response = await self.client.get(first)
        self.assertEqual(response.status_code, 410)
        response = await self.client.get(second)
        self.assertEqual(response.status_code, 302)

    async def test_access_log_names_client_without_signed_playlink(self):
        with self.assertLogs(appmod._log, level="INFO") as captured:
            await self.client.get("/v/api/v1/media/range/unknown", params={
                "playlink": "https://media.test/v.mp4?secret=private-signed-value"},
                headers={"User-Agent": "com.trim.media.ios"})
        output = "\n".join(captured.output)
        self.assertIn("client=iOS", output)
        self.assertNotIn("private-signed-value", output)
        self.assertNotIn("media.test", output)
        self.assertNotIn("private-signed-value", json.dumps(appmod.livemod.snapshot()))

    async def test_each_client_hls_byteranges_and_resume_contract(self):
        self.wrapper, self.protected = "m3u8", True
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                self.client.headers["User-Agent"] = ua
                guid, eg = await self.load_episode()
                mg = storemod.ep_media_guid(eg)
                info = (await self.client.post("/v/api/v1/play/info", json={
                    "item_guid": eg})).json()["data"]
                self.assertEqual(info["media_guid"], mg)
                head = await self.client.head(f"/v/api/v1/media/range/{mg}")
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.headers["content-type"], "application/vnd.apple.mpegurl")
                self.assertNotIn("content-length", head.headers)
                response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                                 follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                lines = response.text.splitlines()
                uris = [line for line in lines if line and not line.startswith("#")]
                self.assertEqual(uris[0], uris[1])
                for start, end in ((16, 31), (32, 47)):
                    part = await self.client.get(uris[0], headers={
                        "Range": f"bytes={start}-{end}"})
                    self.assertEqual(part.status_code, 206)
                    self.assertEqual(part.content, MEDIA_BYTES[start:end+1])
                await self.client.post("/v/api/v1/play/record", json={
                    "item_guid": eg, "media_guid": mg, "ts": 123, "duration": 3600})
                history = (await self.client.get("/v/api/v1/play/list")).json()["data"]
                self.assertEqual(history[0]["guid"], eg)
                info = (await self.client.post("/v/api/v1/play/info", json={
                    "item_guid": guid})).json()["data"]
                self.assertEqual(info["guid"], eg)
                self.assertEqual(info["ts"], 123)


if __name__ == "__main__":
    unittest.main()
