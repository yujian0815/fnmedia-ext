"""CMS/T4 isolation gates. No NAS, official socket, or production credentials."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))
import app as appmod
import config as configmod
import history as historymod
import inject
import providers
import store as storemod
import t4


class CoexistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_application_import_does_not_construct_t4_runtime(self):
        code = """
import sys
sys.path.insert(0, 'proxy')
from unittest.mock import patch
with patch('t4_service.T4Service', side_effect=AssertionError('T4 service')), \
     patch('t4_media.T4Media', side_effect=AssertionError('T4 media')), \
     patch('t4_browser.T4Browser', side_effect=AssertionError('T4 state read')):
    import app
    assert app.t4_service is app.t4_media is app.t4_browser is None
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    async def test_plain_cms_keeps_upstream_unbounded_fanout(self):
        active = maximum = 0

        async def search(*_):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return []

        sources = [{"id": str(i), "enabled": True} for i in range(12)]
        with patch.object(providers, "search_source", side_effect=search), \
                patch.object(t4, "search", side_effect=AssertionError("T4 request")):
            self.assertEqual(await providers.search_all("Fixture", sources), [])
        self.assertEqual(maximum, 12)

    async def test_disabled_t4_does_not_select_mixed_scheduler(self):
        sources = [{"id": "cms", "enabled": True},
                   {"id": "t4", "kind": "t4", "enabled": False}]
        with patch.object(providers, "_search_cms", new_callable=AsyncMock,
                          return_value=[]) as cms:
            await providers.search_all("Fixture", sources, soft_timeout=1.2)
        cms.assert_awaited_once_with("Fixture", sources, 1.2)

    async def test_plain_cms_preserves_soft_timeout_and_partial_results(self):
        cancelled = asyncio.Event()
        marker = object()

        async def search(_client, source, _keyword):
            if source["id"] == "fast":
                return [marker]
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with patch.object(providers, "search_source", side_effect=search):
            result = await providers.search_all("Fixture", [
                {"id": "fast", "enabled": True}, {"id": "slow", "enabled": True}],
                soft_timeout=0.6)
        self.assertEqual(result, [marker])
        self.assertTrue(cancelled.is_set())

    async def test_cms_routes_and_absent_t4_endpoints_never_create_t4(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(configmod, "_STATE", Path(temp) / "config.json"), \
                    patch.object(storemod, "_STATE", Path(temp) / "titles.json"):
                config = configmod.Config()
                config.save({"sources": [],
                             "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
                store = storemod.Store()
                self.addCleanup(store.close)
                title = providers._map_vod({
                    "vod_id": "cms-1", "vod_name": "Fixture", "type_name": "\u7535\u89c6\u5267",
                    "vod_play_from": "A", "vod_play_url":
                    "01$https://media.test/1.mp4#02$https://media.test/2.mp4"},
                    {"id": "cms", "api": "https://cms.test/api"})
                brief = inject.brief_of(title)
                store.remember_merged([brief])
                with patch.object(appmod, "config", config), \
                        patch.object(appmod, "store", store), \
                        patch.object(storemod, "store", store), \
                        patch.object(appmod, "t4_service", None), \
                        patch.object(appmod, "t4_media", None), \
                        patch.object(appmod, "t4_browser", None), \
                        patch.object(appmod, "T4Service", side_effect=AssertionError("service")), \
                        patch.object(appmod, "T4Media", side_effect=AssertionError("media")), \
                        patch.object(appmod, "T4Browser", side_effect=AssertionError("state")), \
                        patch.object(appmod, "_viewer_can_open_lib", return_value=True), \
                        patch.object(appmod, "_warm_playlist") as warm:
                    async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=appmod.app),
                            base_url="http://fixture.test") as client:
                        self.assertIsNone(await appmod._load_t4_detail(brief["guid"], None))
                        self.assertEqual(appmod._t4_sources(), [])
                        self.assertIsNone(appmod._t4_node("unknown-guid"))
                        for path in (
                            f"/v/api/v1/mdb/{appmod.T4_LIB_GUID}",
                            f"/v/api/v1/item/{appmod.T4_LIB_GUID}",
                        ):
                            response = await client.get(path)
                            self.assertEqual(response.json()["code"], -6)
                        response = await client.get("/v/media/t4-resource/expired")
                        self.assertEqual(response.status_code, 410)
                        response = await client.get(f"/v/api/v1/item/{brief['guid']}")
                        self.assertEqual(response.json()["data"]["title"], brief["title"])
                    warm.assert_not_called()
                    self.assertIsNone(appmod.t4_service)
                    self.assertIsNone(appmod.t4_media)
                    self.assertIsNone(appmod.t4_browser)

    def test_cms_season_and_episode_payloads_keep_original_shape(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(storemod, "_STATE", Path(temp) / "titles.json"):
            store = storemod.Store()
            self.addCleanup(store.close)
            title = providers._map_vod({
                "vod_id": "1", "vod_name": "Fixture", "vod_year": "2024",
                "vod_play_from": "A", "vod_play_url":
                "01$https://media.test/1.mp4#02$https://media.test/2.mp4"},
                {"id": "cms", "api": "https://cms.test/api"})
            brief = inject.brief_of(title)
            store.remember_merged([brief])
            with patch.object(storemod, "store", store):
                season = inject.season_list(brief)[0]
                # 普通 CMS 合成季保持包装形状：集数报 0、can_play=None，
                # 客户端从 episode/list 自行统计（v1.0.0 行为）。
                self.assertEqual(season["number_of_episodes"], 0)
                self.assertIsNone(season["can_play"])
                episodes = inject.episode_list(brief)
                self.assertEqual(episodes[0]["overview"], "")
                self.assertNotIn("posters", episodes[0])
                self.assertEqual(episodes[1]["guid"],
                                 storemod.episode_guid(brief["guid"], "", 1))
                info = inject.episode_play_info(store.get_episode(episodes[1]["guid"]))
                self.assertNotIn("posters", info["item"])

    def test_t4_warmup_never_enters_cms_playlist_path(self):
        with patch.object(appmod.asyncio, "create_task",
                          side_effect=AssertionError("CMS background work")):
            appmod._warm_playlist("fixture", {"t4": True}, None)

    def test_cms_resume_order_is_not_changed_by_t4_rewatch_support(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(historymod, "_STATE", Path(temp) / "history.json"):
            history = historymod.History()
            history._users = {"": {
                "e1": {"ts": 10, "duration": 100, "updated_at": 20},
                "e2": {"ts": 15, "duration": 100, "updated_at": 10}}}
            self.assertEqual(history.resume_episode(["e1", "e2"], user=""), ("e2", 15))
            self.assertEqual(history.resume_episode(["e1", "e2"], user="",
                                                    latest_interaction=True), ("e1", 10))
