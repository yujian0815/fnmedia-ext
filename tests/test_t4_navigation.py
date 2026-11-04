"""Explicit search cards use native directories, never guessed playback."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

import test_t4_browse as fixtures
import inject
import t4
import t4_browser


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BrowseTests.asyncSetUp
    import_source = fixtures.BrowseTests.import_source
    handle = fixtures.BrowseTests.handle
    listing = fixtures.BrowseTests.listing
    plugin = fixtures.BrowseTests.plugin

    async def test_explicit_navigation_click_searches_without_detail_or_player(self):
        home = await self.plugin()
        raw = {"vod_id": "tmdb:1", "vod_name": "Film", "type_name": "电影",
               "t4_action": {"type": "search", "keyword": "Film"}}
        card = self.browser._rows([raw], self.source, home)[0]
        self.assertEqual(card["type"], "Directory")
        self.assertEqual(self.browser.node(card["guid"])["kind"], "search")
        self.assertEqual(card["can_play"], 0)
        results = {"list": [
            {"vod_id": "film-a", "vod_name": "Film", "vod_year": "2026",
             "type_name": "电视剧", "t4_source": "Source A"},
            {"vod_id": "film-b", "vod_name": "Film", "vod_year": "2025",
             "t4_source": "Source B"},
            {"vod_id": "folder", "vod_name": "资源", "vod_tag": "folder"},
        ], "t4_search": {"completed": 2, "failed": 0, "total": 2, "partial": False}}
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            self.client.headers["User-Agent"] = ua
            with patch.object(t4, "navigation_search", return_value=results) as search:
                self.browser._cache.clear()
                self.browser._cache_bytes = 0
                data = await self.listing(card["guid"])
                self.assertEqual(data["total"], 3)
                self.assertEqual([r["type"] for r in data["list"]],
                                 ["TV", "Directory", "Directory"])
                self.assertEqual([r["title"] for r in data["list"][:2]],
                                 ["Film · Source A", "Film · Source B"])
                self.assertEqual(len(data["jump_list"]), 3)
                await self.listing(card["guid"], offset=1, page_size=1)
                self.assertEqual(search.await_count, 1)
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params for r in self.calls))
        restored = t4_browser.T4Browser(self.store, self.config)
        self.assertEqual(restored.node(card["guid"])["keyword"], "Film")

    async def test_source_search_switch_does_not_disable_explicit_navigation(self):
        self.assertFalse(self.config.all_sources()[0]["searchable"])

        def respond(request):
            self.assertEqual(request.url.params["nav_search"], "true")
            self.assertEqual(request.url.params["wd"], "Film")
            self.assertEqual(request.url.params["token"], "fixture")
            return httpx.Response(200, json={"list": []})

        async with type(self.network)(transport=httpx.MockTransport(respond)) as client:
            self.assertEqual(await t4.navigation_search(client, self.source, "Film"), {"list": []})

    async def test_labels_and_folder_names_do_not_implicitly_trigger_search(self):
        home = await self.plugin()
        for value in (None, "search", {"type": "play", "keyword": "Film"},
                      {"type": "search", "keyword": 10}):
            row = {"vod_id": "film-" + str(value), "vod_name": "片单导航",
                   "type_name": "电影", "t4_action": value}
            card = self.browser._rows([row], self.source, home)[0]
            self.assertEqual(card["type"], "Movie")
        folder = self.browser._rows([{
            "vod_id": "folder", "vod_name": "Film", "vod_tag": "folder",
        }], self.source, home)[0]
        self.assertEqual(self.browser.node(folder["guid"])["kind"], "category")

    async def test_search_cache_singleflight_partial_retry_and_failure(self):
        self.browser._cache.clear()
        self.browser._cache_bytes = 0
        calls = 0

        async def search(*args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"list": []}

        with patch.object(t4, "navigation_search", side_effect=search):
            await asyncio.gather(*(self.browser._raw(
                self.network, self.source, keyword="Film") for _ in range(8)))
        self.assertEqual(calls, 1)
        self.assertFalse(self.browser._locks)
        self.browser._cache.clear()
        self.browser._cache_bytes = 0
        with patch.object(t4, "navigation_search", return_value={
                "list": [], "t4_search": {"partial": True}}) as partial:
            await self.browser._raw(self.network, self.source, keyword="Film")
            await self.browser._raw(self.network, self.source, keyword="Film")
            self.assertEqual(partial.await_count, 2)
        with patch.object(t4, "navigation_search", side_effect=t4.T4Error("搜索超时")):
            with self.assertRaises(t4.T4Error):
                await self.browser._raw(self.network, self.source, keyword="Film")
        self.assertFalse(self.browser._locks)

    async def test_partial_search_pages_share_snapshot_in_four_client_profiles(self):
        home = await self.plugin()
        card = self.browser._rows([{
            "vod_id": "tmdb:1", "vod_name": "Film", "type_name": "电影",
            "t4_action": {"type": "search", "keyword": "Film"},
        }], self.source, home)[0]
        results = {"list": [
            {"vod_id": f"film-{i}", "vod_name": f"Film {i}",
             "type_name": "电视剧", "t4_source": "Source A"}
            for i in range(66)
        ], "t4_search": {"completed": 16, "failed": 2, "total": 174,
                        "partial": True, "truncated": 125}}

        async def search(*args):
            await asyncio.sleep(0.01)
            return results

        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                self.browser._cache.clear()
                self.browser._cache_bytes = 0
                self.client.headers["User-Agent"] = ua
                with patch.object(t4, "navigation_search", side_effect=search) as lookup:
                    pages = await asyncio.gather(*(self.listing(
                        card["guid"], offset=offset, page_size=22)
                        for offset in (0, 22, 44)))
                    again = await self.listing(card["guid"], page_size=22)
                self.assertEqual(lookup.await_count, 1)
                self.assertTrue(all(page["total"] == 66 for page in pages))
                self.assertTrue(all(page["t4_search"] == results["t4_search"]
                                    for page in pages))
                self.assertEqual([row["guid"] for row in again["list"]],
                                 [row["guid"] for row in pages[0]["list"]])
                self.assertEqual(len({row["guid"] for page in pages
                                      for row in page["list"]}), 66)
                self.assertFalse(self.browser._locks)
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params
                             for r in self.calls))

    async def test_incomplete_snapshot_expires_and_does_not_cache_empty_results(self):
        for status in ({"partial": True}, {"partial": False, "failed": 1}):
            with self.subTest(status=status):
                self.browser._cache.clear()
                self.browser._cache_bytes = 0
                result = {"list": [{"vod_id": "film", "vod_name": "Film"}],
                          "t4_search": status}
                with patch.object(t4, "navigation_search", return_value=result) as lookup:
                    await self.browser._raw(self.network, self.source, keyword="Film")
                    await self.browser._raw(self.network, self.source, keyword="Film")
                    self.assertEqual(lookup.await_count, 1)
                    for key, (stamp, data, size) in list(self.browser._cache.items()):
                        self.browser._cache[key] = stamp - 61, data, size
                    await self.browser._raw(self.network, self.source, keyword="Film")
                    self.assertEqual(lookup.await_count, 2)
                    self.assertEqual(len(self.browser._cache), 1)
                    self.assertEqual(self.browser._cache_bytes,
                                     next(iter(self.browser._cache.values()))[2])
                self.browser._cache.clear()
                self.browser._cache_bytes = 0
                with patch.object(t4, "navigation_search", return_value={
                        "list": [], "t4_search": status}) as lookup:
                    await self.browser._raw(self.network, self.source, keyword="Film")
                    await self.browser._raw(self.network, self.source, keyword="Film")
                    self.assertEqual(lookup.await_count, 2)
                    self.assertFalse(self.browser._cache)

    async def test_browse_timeout_records_source_and_node_without_credentials(self):
        home = await self.plugin()
        card = self.browser._rows([{
            "vod_id": "tmdb:1", "vod_name": "Film",
            "t4_action": {"type": "search", "keyword": "Film"},
        }], self.source, home)[0]
        with patch.object(self.browser, "listing", side_effect=asyncio.TimeoutError), \
                self.assertLogs("fnmedia", level="WARNING") as logs:
            response = await self.client.post("/v/api/v1/item/list", json={
                "ancestor_guid": t4_browser.LIB_GUID,
                "parent_guid": card["guid"], "page": 2})
        self.assertEqual(response.status_code, 504)
        self.assertIn("浏览超时", response.json()["msg"])
        log = "\n".join(logs.output)
        for value in (self.source["id"], card["guid"], "kind=search", "page=2",
                      "elapsed_ms="):
            self.assertIn(value, log)
        self.assertNotIn("token=", log)
        self.assertNotIn("http://", log)

    async def test_navigation_card_from_global_search_keeps_explicit_intent(self):
        brief = inject.brief_of(t4.map_title({
            "vod_id": "tmdb:2", "vod_name": "Film", "type_name": "电影",
            "t4_action": {"type": "search", "keyword": "Film", "year": "2026"},
        }, self.source))
        self.store.remember_merged([brief])
        card = self.browser.media_card(brief)
        self.assertEqual(self.browser.node(card["guid"])["kind"], "search")
        self.assertEqual(self.browser.node(card["guid"])["year"], "2026")
        self.assertNotIn("token=", json.dumps(card))

    async def test_explicit_search_does_not_change_playable_plugins_or_mix_same_ids(self):
        home = await self.plugin()
        rows = [
            {"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
             "t4_source": "Source A"},
            {"vod_id": "other", "vod_name": "测试剧", "type_name": "电视剧",
             "t4_source": "Source B"},
        ]
        cards = self.browser._rows(rows, self.source, home)
        self.assertNotEqual(cards[0]["guid"], cards[1]["guid"])
        detail = await self.client.get("/v/api/v1/item/" + cards[0]["guid"])
        self.assertEqual(detail.status_code, 200)
        eps = self.store.tv_episode_guids(cards[0]["guid"])
        self.assertEqual(len(eps), 2)
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        info = (await self.client.post("/v/api/v1/play/info",
                                      json={"item_guid": eps[1]})).json()["data"]
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": info["media_guid"], "line": "B"})
        self.assertEqual(response.json()["data"]["play_link"], "https://media.test/b2.mp4")

    async def test_incremental_scroll_continues_same_session_in_four_profiles(self):
        home = await self.plugin()
        card = self.browser._rows([{
            "vod_id": "tmdb:1", "vod_name": "Film",
            "t4_action": {"type": "search", "keyword": "Film"},
        }], self.source, home)[0]
        rows = [{"vod_id": f"film-{i}", "vod_name": f"Film {i}",
                 "type_name": "电视剧", "t4_source": "Source A"} for i in range(45)]

        async def search(client, source, keyword, year, options):
            self.assertEqual(options["nav_incremental"], "true")
            offset, limit = options["nav_offset"], options["nav_limit"]
            self.assertEqual(options.get("nav_session", "test-session"), "test-session")
            return {"list": rows[offset:offset + limit], "total": 45,
                    "t4_search": {"incremental": True, "session": "test-session",
                                  "pending": False, "partial": False}}

        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                self.browser._cache.clear()
                self.browser._cache_bytes = 0
                self.browser._search_sessions.clear()
                self.client.headers["User-Agent"] = ua
                with patch.object(t4, "navigation_search", side_effect=search) as lookup:
                    pages = await asyncio.gather(*(self.listing(card["guid"],
                        offset=offset, page_size=22) for offset in (0, 22, 44)))
                    replay = await self.listing(card["guid"], page_size=22)
                self.assertEqual(lookup.await_count, 3)
                self.assertEqual([len(page["list"]) for page in pages], [22, 22, 1])
                self.assertTrue(all(page["total"] == 45 for page in pages))
                self.assertEqual(pages[0]["list"], replay["list"])
                self.assertEqual(len({r["guid"] for page in pages for r in page["list"]}), 45)

    async def test_sparse_incremental_page_exposes_native_continue_directory(self):
        home = await self.plugin()
        card = self.browser._rows([{
            "vod_id": "tmdb:1", "vod_name": "Film",
            "t4_action": {"type": "search", "keyword": "Film"},
        }], self.source, home)[0]
        rows = [{"vod_id": "first", "vod_name": "Film", "type_name": "电视剧"}]

        async def search(client, source, keyword, year, options):
            if options["nav_offset"] == 0:
                return {"list": rows, "total": 2, "t4_search": {
                    "incremental": True, "session": "sparse", "pending": True,
                    "partial": True, "completed": 1, "total": 2}}
            self.assertEqual(options["nav_offset"], 1)
            self.assertEqual(options["nav_session"], "sparse")
            return {"list": [{"vod_id": "late", "vod_name": "Film",
                              "type_name": "电视剧"}],
                    "total": 2, "t4_search": {"incremental": True, "session": "sparse",
                                            "pending": False, "partial": False}}

        with patch.object(t4, "navigation_search", side_effect=search) as lookup:
            first = await self.listing(card["guid"])
            self.assertEqual(len(first["list"]), 2)
            more = first["list"][-1]
            self.assertEqual(more["type"], "Directory")
            self.assertEqual(more["title"], "继续搜索剩余来源")
            self.assertEqual(self.browser.node(more["guid"])["search_offset"], 1)
            second = await self.listing(more["guid"])
            self.assertEqual(second["total"], 1)
            self.assertEqual(len(second["list"]), 1)
            self.assertNotEqual(first["list"][0]["guid"], second["list"][0]["guid"])
            self.assertEqual(lookup.await_count, 2)

    async def test_expired_session_prompts_reentry_without_starting_another_search(self):
        def respond(request):
            return httpx.Response(400, json={"detail": {
                "code": "t4_request_invalid", "message": "session expired"}})

        async with type(self.network)(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(t4.T4Error, "返回片单重新选片"):
                await t4.navigation_search(client, self.source, "Film", "", {
                    "nav_incremental": "true", "nav_session": "expired",
                    "nav_offset": 22, "nav_limit": 22})


if __name__ == "__main__":
    unittest.main()
