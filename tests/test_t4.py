"""Offline contracts: no NAS, socket takeover, credentials, or public sources."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import config as configmod
import inject
import providers
import store as storemod
import t4
import t4_media
import t4_service
import tvbox


SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}


def detail_row(order=False):
    return {"vod_id": "movie-1", "vod_name": "Test Series", "type_name": "电视剧",
            "vod_play_from": "B$$$A" if order else "A$$$B",
            "vod_play_url": ("第02集$b2#第01集$b1$$$第01集$a1#第02集$a2" if order else
                             "第01集$a1#第02集$a2$$$预告$trailer#第02集$b2#第01集$b1")}


class T4ProtocolTests(unittest.IsolatedAsyncioTestCase):
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
            titles = await providers.search_source(client, SOURCE, "Test")
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

    async def test_empty_source_list_does_not_enable_defaults(self):
        with patch.object(providers, "search_source", new_callable=AsyncMock) as search:
            self.assertEqual(await providers.search_all("Test", []), [])
            search.assert_not_awaited()

    async def test_bounded_search(self):
        active = high = 0

        async def search(*_):
            nonlocal active, high
            active += 1
            high = max(high, active)
            await asyncio.sleep(0.001)
            active -= 1
            return []

        with patch.object(providers, "search_source", side_effect=search):
            await providers.search_all("Test", [dict(SOURCE, id=str(i)) for i in range(35)])
        self.assertLessEqual(high, 8)

    async def test_search_budget_keeps_fast_results_and_cancels_slow(self):
        cancelled = asyncio.Event()

        async def search(_client, source, _keyword):
            if source["id"] == "fast":
                return [t4.map_title({"vod_id": "1", "vod_name": "Fast"}, source)]
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with patch.object(providers, "search_source", side_effect=search):
            results = await providers.search_all("Test", [
                dict(SOURCE, id="fast"), dict(SOURCE, id="slow")], budget=0.02)
        self.assertEqual([t.title for t in results], ["Fast"])
        self.assertTrue(cancelled.is_set())

    async def test_mixed_search_does_not_starve_either_source_type(self):
        started = []

        async def search(_client, source, _keyword):
            started.append(source["id"])
            await asyncio.sleep(10)

        sources = [dict(SOURCE, id=f"cms-{i}", kind="cms") for i in range(12)]
        sources += [dict(SOURCE, id=f"t4-{i}") for i in range(12)]
        with patch.object(providers, "search_source", side_effect=search):
            await providers.search_all("Test", sources, budget=0.02)
        self.assertEqual(len(started), 8)
        self.assertEqual(sum(sid.startswith("t4-") for sid in started), 4)
        self.assertEqual(sum(sid.startswith("cms-") for sid in started), 4)

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

    async def test_remote_subscription_wrapper_and_relative_api(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def client():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"sites": [
                        {"type": 4, "name": "Relative", "api": "../api/demo"}]}))) as value:
                yield value

        with patch("subfetch.async_sub_client", client):
            sites, subs = await tvbox.load_remote("https://t4.test/t4/config.json")
        self.assertEqual(sites[0]["api"], "https://t4.test/api/demo")
        self.assertEqual(subs, 0)


class T4StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            patch.object(configmod, "_STATE", Path(self.temp.name) / "config.json"),
            patch.object(storemod, "_STATE", Path(self.temp.name) / "titles.json"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.service = t4_service.T4Service(self.store, self.config)
        self.source = dict(SOURCE)
        cfg = self.config.get()
        cfg["sources"] = [self.source]
        self.config.save({"sources": cfg["sources"]})

    def remember_card(self):
        card = t4.map_title({"vod_id": "movie-1", "vod_name": "Test Series"}, self.source)
        brief = inject.brief_of(card)
        self.store.remember_merged([brief])
        return brief["guid"]

    def test_classification_and_import_persistence(self):
        sites = [
            {"type": 4, "key": "pan", "name": "115云盘", "api": SOURCE["api"],
             "searchable": 0, "filterable": 1, "indexs": 0},
            {"type": 4, "key": "off", "name": "Disabled", "api": SOURCE["api"] + "&v=2",
             "enabled": False},
            {"type": 3, "key": "push", "name": "Push", "api": "csp_Push"},
            {"type": 4, "name": "Bad", "api": "file://tmp"}]
        groups = tvbox.classify_subscription(sites)
        self.assertEqual(len(groups["t4"]), 2)
        self.assertFalse(groups["t4"][0]["searchable"])
        self.assertFalse(groups["t4"][1]["enabled"])
        self.assertFalse(groups["cms"])
        report = self.config.import_t4_sources(groups["t4"])
        self.assertEqual(report["added"], 2)
        self.assertEqual(self.config.import_t4_sources(groups["t4"])["duplicate"], 2)
        reloaded = configmod.Config()
        imported = [s for s in reloaded.all_sources() if s.get("kind") == "t4"]
        self.assertEqual(len(imported), 3)
        self.assertTrue(next(s for s in imported if s.get("site_key") == "pan")["filterable"])
        self.assertFalse(next(s for s in imported if s.get("site_key") == "pan")["indexs"])

    def test_full_subscription_import_preserves_existing_sources_and_switches(self):
        for count in (172, 179):
            with self.subTest(count=count):
                self.config.save({"sources": [self.source]})
                sources = [tvbox.classify_site({
                    "type": 4, "key": f"plugin-{i}", "name": f"Plugin {i}",
                    "api": f"http://t4.test/t4/fm/env-at/plugin-{i}/",
                    "searchable": 0 if i % 7 == 0 else 1,
                    "indexs": 0 if i % 11 == 0 else 1,
                })[1] for i in range(count)]
                first = self.config.import_t4_sources(sources[:8])
                self.assertEqual(first["added"], 8)
                old = self.config.all_sources()
                existing = next(s for s in old if s.get("site_key") == "plugin-2")
                existing["enabled"] = False
                existing["name"] = "My existing name"
                self.config.save({"sources": old})
                report = self.config.import_t4_sources(sources)
                self.assertEqual(report["added"], count - 8)
                self.assertEqual(report["duplicate"], 8)
                repeated = self.config.import_t4_sources(sources)
                self.assertEqual((repeated["added"], repeated["duplicate"]), (0, count))
                reloaded = configmod.Config()
                rows = [s for s in reloaded.all_sources() if s.get("site_key")]
                self.assertEqual(len(rows), count)
                self.assertEqual(len({s["id"] for s in rows}), count)
                self.assertEqual([s["site_key"] for s in rows],
                                 [f"plugin-{i}" for i in range(count)])
                self.assertFalse(rows[2]["enabled"])
                self.assertEqual(rows[2]["name"], "My existing name")
                self.assertFalse(rows[0]["searchable"])
                self.assertFalse(rows[0]["indexs"])
                self.assertTrue(any(s["id"] == self.source["id"]
                                    for s in reloaded.all_sources()))

    async def test_lazy_singleflight_refresh_restart_and_episode_identity(self):
        guid = self.remember_card()
        calls = []
        changed = False

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0.002)
            return httpx.Response(200, json={"list": [detail_row(changed)]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(*(self.service.ensure_detail(client, guid) for _ in range(10)))
            self.assertEqual(len(calls), 1)
            eg = next(eg for eg in self.store.tv_episode_guids(guid)
                      if self.store.get_episode(eg)["ep_title"] == "第02集")
            self.assertEqual(self.store.get_episode_target(eg, "B")["url"], "b2")
            self.assertEqual(self.store.get_episode_target(eg, "A")["url"], "a2")
            self.assertIsNone(self.store.get_episode_target(eg, "missing"))
            changed = True
            await self.service.ensure_detail(client, guid, force=True)
            self.assertEqual(self.store.get_episode_target(eg, "B")["url"], "b2")
            self.assertEqual(self.store.get_episode_target(eg, "A")["url"], "a2")
            self.store.flush()
            restored = storemod.Store()
            self.addCleanup(restored.close)
            new_service = t4_service.T4Service(restored, self.config)
            await new_service.ensure_detail(client, guid)
        self.assertEqual(len(calls), 3)
        self.assertEqual(restored.get_episode_target(eg, "A")["url"], "a2")
        self.assertFalse(self.service._locks)

    async def test_player_only_on_selection_and_not_persisted(self):
        guid = self.remember_card()
        calls = []

        def handler(request):
            calls.append(dict(request.url.params))
            if "play" in request.url.params:
                return httpx.Response(200, json={"url": "https://media.test/v.mp4?secret=runtime",
                                                 "header": {"Cookie": "runtime-secret"}})
            return httpx.Response(200, json={"list": [detail_row()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await self.service.ensure_detail(client, guid)
            self.assertEqual(len(calls), 1)
            eg = self.store.tv_episode_guids(guid)[1]
            result = await self.service.resolve(client, self.store.get_episode_target(eg, "B"))
        self.assertEqual(calls[-1]["play"], "b2")
        self.assertEqual(calls[-1]["flag"], "B")
        self.assertEqual(result.headers, {"Cookie": "runtime-secret"})
        self.store.flush()
        state = storemod._STATE.read_text(encoding="utf-8")
        self.assertNotIn("runtime-secret", state)
        self.assertNotIn("secret=runtime", state)

    async def test_player_singleflight_short_cache_and_invalidation(self):
        guid = self.remember_card()
        played = []

        async def handler(request):
            await asyncio.sleep(0.002)
            if "play" in request.url.params:
                played.append(request)
                return httpx.Response(200, json={"url": "https://media.test/v.mp4"})
            return httpx.Response(200, json={"list": [detail_row()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await self.service.ensure_detail(client, guid)
            target = self.store.get_episode_target(self.store.tv_episode_guids(guid)[0])
            await asyncio.gather(*(self.service.resolve(client, target) for _ in range(15)))
            self.assertEqual(len(played), 1)
            self.service.invalidate_playback(target)
            await self.service.resolve(client, target)
            self.assertEqual(len(played), 2)
        self.assertFalse(self.service._play_locks)

    async def test_expired_player_id_refreshes_detail_once(self):
        guid = self.remember_card()
        details, plays = 0, 0

        def handler(request):
            nonlocal details, plays
            if "play" in request.url.params:
                plays += 1
                if plays == 1:
                    return httpx.Response(400)
                return httpx.Response(200, json={"url": "https://media.test/fresh.mp4"})
            details += 1
            return httpx.Response(200, json={"list": [detail_row()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await self.service.ensure_detail(client, guid)
            target = self.store.get_episode_target(self.store.tv_episode_guids(guid)[0])
            result = await self.service.resolve(client, target)
        self.assertEqual((details, plays), (2, 2))
        self.assertTrue(result.url.endswith("fresh.mp4"))

    async def test_detail_refresh_invalidates_cached_playback(self):
        guid = self.remember_card()
        changed = False
        plays = []

        def handler(request):
            if "play" in request.url.params:
                play_id = request.url.params["play"]
                plays.append(play_id)
                return httpx.Response(200, json={"url": f"https://media.test/{play_id}.mp4"})
            row = detail_row()
            if changed:
                row["vod_play_url"] = row["vod_play_url"].replace("$a1", "$a1-new")
            return httpx.Response(200, json={"list": [row]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await self.service.ensure_detail(client, guid)
            target = self.store.get_episode_target(
                self.store.t4_episode_guid(guid, "episode:1"), "A")
            await self.service.resolve(client, target)
            changed = True
            await self.service.ensure_detail(client, guid, force=True)
            result = await self.service.resolve(client, target)
        self.assertEqual(plays, ["a1", "a1-new"])
        self.assertTrue(result.url.endswith("a1-new.mp4"))

    async def test_incomplete_detail_keeps_card_metadata(self):
        guid = self.remember_card()
        row = dict(detail_row())
        row.pop("vod_name")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"list": [row]}))) as client:
            brief = await self.service.ensure_detail(client, guid)
        self.assertEqual(brief["title"], "Test Series")

    def test_thousand_episode_index_is_not_rebuilt_for_each_guid(self):
        row = dict(detail_row(), vod_play_from="A",
                   vod_play_url="#".join(f"第{i}集$id-{i}" for i in range(1000, 0, -1)))
        title = t4.map_title(row, self.source, detail=True)
        guid = storemod.make_guid(title)
        self.store.remember_merged([inject.brief_of(title)])
        with patch.object(self.store, "_t4_rows", side_effect=AssertionError("no rescan")):
            guids = self.store.tv_episode_guids(guid)
            self.assertEqual(len(guids), 1000)
            self.assertEqual(self.store.get_episode(guids[0])["ep_title"], "第1集")
            self.assertEqual(self.store.episode_guid_at(guid, 999), guids[-1])

    async def test_cancelled_detail_does_not_leak_lock(self):
        guid = self.remember_card()
        entered = asyncio.Event()

        async def handler(_request):
            entered.set()
            await asyncio.sleep(10)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(self.service.ensure_detail(client, guid))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.service._locks)

    async def test_disabled_source_rejects_old_history(self):
        guid = self.remember_card()
        cfg = self.config.get()
        cfg["sources"][0]["enabled"] = False
        self.config.save({"sources": cfg["sources"]})
        with self.assertRaises(t4.T4Error):
            await self.service.ensure_detail(None, guid)

    async def test_stale_opaque_never_recovers_by_title(self):
        card = t4.map_title({"vod_id": "t4_expired", "vod_name": "Same Name"}, self.source)
        self.store.remember_merged([inject.brief_of(card)])
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(400, json={"detail": "expired"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(t4.T4Error):
                await self.service.ensure_detail(client, storemod.make_guid(card))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("wd", calls[0].url.params)

    async def test_wrong_title_and_folders_rejected(self):
        for row in [
            dict(detail_row(), vod_id="another-movie"),
            dict(detail_row(), vod_tag="folder"),
        ]:
            with self.subTest(row=row):
                guid = self.remember_card()
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json={"list": [row]}))) as client:
                    with self.assertRaises(t4.T4Error):
                        await self.service.ensure_detail(client, guid, force=True)

    def test_duplicate_labels_not_aligned_by_position(self):
        row = dict(detail_row(), vod_play_from="A",
                   vod_play_url="视频$one#视频$two")
        title = t4.map_title(row, self.source, detail=True)
        guid = storemod.make_guid(title)
        self.store.remember_merged([inject.brief_of(title)])
        self.assertEqual(self.store.tv_episode_guids(guid), [])

    def test_playback_sessions_are_bounded_expiring_and_media_scoped(self):
        target = {"online_guid": "title", "line": "A"}
        with patch("t4_service.time.monotonic", return_value=0):
            first = self.service.create_session("media-a", target)
            self.assertIsNone(self.service.session_target("media-b", first))
            self.assertEqual(self.service.session_target("media-a", first), target)
            self.service.set_session_target("media-a", first, dict(target, line="B"))
            self.assertEqual(self.service.session_target("media-a", first)["line"], "B")
            for _ in range(128):
                latest = self.service.create_session("media-a", target)
        self.assertEqual(len(self.service._sessions), 128)
        self.assertNotIn(first, self.service._sessions)
        with patch("t4_service.time.monotonic", return_value=21601):
            self.assertIsNone(self.service.session_target("media-a", latest))


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_private_media_stays_server_side(self):
        for url in ("http://127.0.0.1/a.mp4", "http://192.168.1.2/a.mp4",
                    "http://[::1]/a.mp4", "http://nas.local/a.mp4"):
            with self.subTest(url=url):
                self.assertTrue(t4.Playback(url, {}, "MP4").needs_proxy)
        self.assertFalse(t4.Playback("https://media.test/a.mp4", {}, "MP4").needs_proxy)

    async def test_public_media_redirect_without_fetch(self):
        media = t4_media.T4Media()
        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: self.fail("must not proxy a public URL"))) as client:
            response = await media.deliver(client, "https://media.test/v.mkv",
                                          t4.Playback("https://media.test/v.mkv", {}, "MKV"), request)
        self.assertEqual(response.status_code, 302)

    async def test_range_head_and_stream_cleanup(self):
        calls = []

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"abcd"

            async def aclose(self):
                self.closed = True

        stream = Stream()

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Cookie"], "session")
            return httpx.Response(206, headers={
                "Content-Range": "bytes 100-103/1000", "Content-Length": "4",
                "Content-Type": "video/mp4", "Accept-Ranges": "bytes"}, stream=stream)

        playback = t4.Playback("http://media.test/v.mp4",
                               {"Cookie": "session", "Range": "bytes=0-"}, "MP4")
        media = t4_media.T4Media()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="GET", headers={"range": "bytes=100-103"}))
            content = b"".join([chunk async for chunk in response.body_iterator])
            self.assertEqual(content, b"abcd")
            self.assertTrue(stream.closed)
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="HEAD", headers={}))
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 100-103/1000")
        self.assertEqual(calls[0].headers["Range"], "bytes=100-103")
        self.assertEqual(calls[0].headers.get_list("Range"), ["bytes=100-103"])
        self.assertEqual(calls[1].method, "HEAD")

    async def test_cancelled_stream_closes_upstream(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.sleep(10)
                yield b"data"

            async def aclose(self):
                closed.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Stream()))) as client:
            response = await t4_media.T4Media().deliver(
                client, "http://media.test/v.mp4",
                t4.Playback("http://media.test/v.mp4", {"Referer": "test"}, "MP4"),
                SimpleNamespace(method="GET", headers={}))
            task = asyncio.create_task(anext(response.body_iterator))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())

    async def test_identify_uses_bounded_get_and_closes_media(self):
        calls, streams = [], []

        class PrefixStream(httpx.AsyncByteStream):
            closed = False
            requested_more = False

            async def __aiter__(self):
                yield b"\x00\x00\x00\x18ftypisom" + bytes(4096 - 12)
                self.requested_more = True
                raise AssertionError("format identification must not read the video")

            async def aclose(self):
                self.closed = True

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Referer"], "needed")
            if request.method == "HEAD":
                return httpx.Response(405)
            self.assertEqual(request.headers["Range"], "bytes=0-4095")
            stream = PrefixStream()
            streams.append(stream)
            return httpx.Response(206, stream=stream,
                                  headers={"Content-Type": "application/octet-stream"})

        playback = t4.Playback("https://media.test/signed", {"Referer": "needed"}, "")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertIs(await t4_media.T4Media().identify(client, playback), playback)
        self.assertEqual(playback.wrapper, "MP4")
        self.assertEqual([r.method for r in calls], ["GET"])
        self.assertTrue(streams[0].closed)
        self.assertFalse(streams[0].requested_more)
        self.assertEqual(playback.headers, {"Referer": "needed"})

    async def test_identify_generic_head_checks_real_get_prefix_without_guessing(self):
        for prefix, wrapper in (
            (b"#EXTM3U\n#EXTINF:6,\nsegment.ts\n", "HLS"),
            (b"\x1a\x45\xdf\xa3\x80matroska", "MKV"),
            (b"<html>upstream error</html>", ""),
            (b'{"error":"no media"}', ""),
        ):
            with self.subTest(wrapper=wrapper, prefix=prefix[:8]):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(200, content=prefix if request.method == "GET" else b"",
                                          headers={"Content-Type": "application/octet-stream"})

                playback = t4.Playback("https://media.test/signed", {}, "")
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    if wrapper:
                        await t4_media.T4Media().identify(client, playback)
                    else:
                        with self.assertRaises(t4.T4Error):
                            await t4_media.T4Media().identify(client, playback)
                self.assertEqual(playback.wrapper, wrapper)
                self.assertEqual([r.method for r in calls], ["GET"])

    async def test_identify_get_still_rejects_real_http_failures(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403)

        playback = t4.Playback("https://media.test/signed", {}, "")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(t4.T4Error):
                await t4_media.T4Media().identify(client, playback)
        self.assertEqual(playback.wrapper, "")
        self.assertEqual([r.method for r in calls], ["GET"])

    async def test_identify_checks_actual_container_not_url_mime_or_declared_type(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(206, content=b"\x1a\x45\xdf\xa3\x80matroska",
                                  headers={"Content-Type": "video/mp4"})

        playback = t4.Playback("https://media.test/wrong.mp4", {}, "MP4")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = t4_media.T4Media()
            await media.identify(client, playback)
            await media.identify(client, playback)
        self.assertEqual(playback.wrapper, "MKV")
        self.assertEqual(len(calls), 1)

    async def test_identify_rejects_html_even_with_mp4_filename_and_mime(self):
        playback = t4.Playback("https://media.test/error.mp4", {}, "MP4")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:
                httpx.Response(200, content=b"<html>error</html>",
                               headers={"Content-Type": "video/mp4"}))) as client:
            with self.assertRaises(t4.T4Error):
                await t4_media.T4Media().identify(client, playback)
        self.assertFalse(playback.identified)

    async def test_hls_nested_and_key_network_only(self):
        media = t4_media.T4Media()
        manifest = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nchild/list.m3u8\n'
        child = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="../key"\n#EXTINF:6,\npart.ts\n'

        def handler(request):
            self.assertEqual(request.headers["Referer"], "needed")
            body = child if "/child/" in request.url.path else manifest
            return httpx.Response(200, text=body,
                                   headers={"Content-Type": "application/vnd.apple.mpegurl"})

        playback = t4.Playback("http://media.test/root.m3u8", {"Referer": "needed"}, "HLS")
        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback, request)
            token = response.body.decode().strip().split("/")[-1]
            response = await media.resource_response(client, token, request)
        text = response.body.decode()
        self.assertIn("METHOD=AES-128", text)
        self.assertNotIn("../key", text)
        self.assertIn('URI="/v/media/t4-resource/', text)
        self.assertEqual(len(media._resources), 3)

    async def test_extensionless_hls_preserves_playlist_roles(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/master", {"Referer": "needed"}, "HLS")
        manifest = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio"\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=1000\nchild\n')
        child = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:6,\nsegment\n'
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Referer"], "needed")
            body = {"/master": manifest.encode(), "/child": child.encode(),
                    "/audio": b"#EXTM3U\n#EXTINF:6,\nsegment\n",
                    "/segment": b"synthetic-media", "/key": b"0123456789abcdef"}[request.url.path]
            return httpx.Response(200, stream=FixtureStream(body), headers={
                "Content-Type": "application/octet-stream"})

        class FixtureStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield self.body

            def __init__(self, body):
                self.body = body

        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback, request)
            self.assertIsInstance(response, t4_media.Response)
            self.assertTrue(response.headers["content-type"].startswith("application/vnd.apple"))
            lines = response.body.decode().splitlines()
            audio_token = lines[1].split('URI="', 1)[1].split('"')[0].rsplit("/", 1)[-1]
            child_token = lines[-1].rsplit("/", 1)[-1]
            audio = await media.resource_response(client, audio_token, request)
            self.assertIn("/v/media/t4-resource/", audio.body.decode())
            child_response = await media.resource_response(client, child_token, request)
            segment_token = child_response.body.decode().strip().splitlines()[-1].rsplit("/", 1)[-1]
            segment = await media.resource_response(client, segment_token, request)
            self.assertEqual(b"".join([part async for part in segment.body_iterator]),
                             b"synthetic-media")
        self.assertEqual(len(calls), 4)

    async def test_hls_repeated_byterange_uri_keeps_one_resource_identity(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/video.m3u8",
                               {"Referer": "needed"}, "HLS")
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MAP:URI="video.mp4",BYTERANGE="16@0"\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16@16\nvideo.mp4\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16\nvideo.mp4\n#EXT-X-ENDLIST\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=manifest))) as client:
            first = await media.deliver(client, playback.url, playback,
                                        SimpleNamespace(method="GET", headers={}))
            second = await media.deliver(client, playback.url, playback,
                                         SimpleNamespace(method="GET", headers={}))
        uris = [line for line in first.body.decode().splitlines()
                if line and not line.startswith("#")]
        self.assertEqual(uris[0], uris[1])
        self.assertIn(f'URI="{uris[0]}"', first.body.decode())
        self.assertIn("#EXT-X-BYTERANGE:16\n", first.body.decode())
        self.assertEqual(first.body, second.body)
        self.assertEqual(len(media._resources), 1)

    def test_hls_resource_identity_includes_headers_and_role(self):
        media = t4_media.T4Media()
        url = "https://media.test/resource"
        first = t4.Playback(url, {"Cookie": "session-a"}, "HLS")
        second = t4.Playback(url, {"Cookie": "session-b"}, "HLS")
        self.assertEqual(media.resource(url, first), media.resource(url, first))
        self.assertNotEqual(media.resource(url, first), media.resource(url, second))
        self.assertNotEqual(media.resource(url, first),
                            media.resource(url, first, manifest=True))

    async def test_hls_head_exposes_playlist_mime_for_native_players(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/manifest",
                               {"Referer": "needed"}, "HLS")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "100"}))) as client:
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="HEAD", headers={}))
        self.assertTrue(response.headers["content-type"].startswith(
            "application/vnd.apple.mpegurl"))
        self.assertNotIn("content-length", response.headers)
        self.assertEqual(response.body, b"")

    async def test_resource_expiration_and_limit(self):
        media = t4_media.T4Media()
        playback = t4.Playback("http://media.test/v.mp4", {}, "MP4")
        with patch("t4_media.time.monotonic", return_value=0):
            first = media.resource(playback.url, playback)
            for i in range(4100):
                media.resource(f"http://media.test/{i}.mp4", playback)
        self.assertEqual(len(media._resources), 4096)
        with patch("t4_media.time.monotonic", return_value=21601):
            response = await media.resource_response(None, first.rsplit("/", 1)[-1], None)
        self.assertEqual(response.status_code, 410)

    async def test_oversized_hls_rejected(self):
        data = b"#EXTM3U\n" + b"x" * 1024
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=data, headers={
                    "Content-Type": "application/vnd.apple.mpegurl"}))) as client:
            with patch.object(t4_media, "MAX_MANIFEST", 100):
                result = await t4_media.T4Media().deliver(
                    client, "http://media.test/v.m3u8",
                    t4.Playback("http://media.test/v.m3u8", {"Referer": "test"}, "HLS"),
                    SimpleNamespace(method="GET", headers={}))
        self.assertEqual(result.status_code, 502)


if __name__ == "__main__":
    unittest.main()
