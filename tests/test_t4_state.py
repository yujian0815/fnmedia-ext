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
import config as configmod
import inject
import providers
import store as storemod
import t4
import t4_service

SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}

def detail_row(order=False):
    return {"vod_id": "movie-1", "vod_name": "Test Series", "type_name": "电视剧",
            "vod_play_from": "B$$$A" if order else "A$$$B",
            "vod_play_url": ("第02集$b2#第01集$b1$$$第01集$a1#第02集$a2" if order else
                             "第01集$a1#第02集$a2$$$预告$trailer#第02集$b2#第01集$b1")}



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
