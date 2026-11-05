"""Native browsing contracts, with isolated state and no production workers."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import inject
import t4
import t4_browser
import test_t4_app as fixtures


class BrowseTests(unittest.IsolatedAsyncioTestCase):
    import_source = fixtures.AppTests.import_source

    async def asyncSetUp(self):
        await fixtures.AppTests.asyncSetUp(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        item = patch.object(t4_browser, "_STATE", Path(temp.name) / "browse.json")
        item.start()
        self.addCleanup(item.stop)
        self.browser = t4_browser.T4Browser(self.store, self.config)
        item = patch.object(appmod, "t4_browser", self.browser)
        item.start()
        self.addCleanup(item.stop)
        self.source = next(s for s in self.config.all_sources() if s.get("kind") == "t4")
        self.config.save({"sources": [dict(self.source, searchable=False, filterable=True)]})
        # 隔离环境不接小雅 / redroid 加固：关掉 0.2.68 之后新增的虚拟库注入，
        # 让 mediadb/list 的断言只反映热门/海量/T4 三类开关。
        for name, value in (("guard_native_enabled", lambda *a, **k: False),
                            ("guard_libs_active", lambda *a, **k: []),
                            ("_xlib_active_cats", lambda *a, **k: [])):
            p = patch.object(appmod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.filters = {}
        self.no_metadata = False
        self.broken = False

    def handle(self, request):
        params = (json.loads(request.content) if request.method == "POST"
                  and request.url.host == "t4.test" else dict(request.url.params))
        if request.url.host != "t4.test" or any(k in params for k in ("wd", "ids", "play")):
            return fixtures.AppTests.handle(self, request)
        self.calls.append(request)
        self.assertEqual(request.url.params["token"], "fixture")
        if self.broken:
            return httpx.Response(503)
        if "t" not in params:
            return httpx.Response(200, json={
                "class": [{"type_id": "series", "type_name": "电视剧"}],
                "filters": {"series": [{"key": "year", "name": "年份", "value": [
                    {"n": "全部", "v": ""}, {"n": "2026", "v": "2026"}]}]},
                "list": [{"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧"},
                         {"vod_id": "pan", "vod_name": "网盘目录", "vod_tag": "folder"},
                         {"vod_id": "action:login", "vod_name": "Login"}]})
        self.assertEqual(params["ac"], "detail")
        self.filters = json.loads(params["extend"])
        if params["t"] == "pan":
            return httpx.Response(200, json={"list": [
                {"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧"}],
                "pagecount": 1, "total": 1, "limit": 20})
        page = int(params.get("pg") or 1)
        rows = [{"vod_id": f"item-{i}", "vod_name": f"节目{i}", "vod_year": "2026"}
                for i in range((page - 1) * 3, min(11, page * 3))]
        result = {"list": rows}
        if not self.no_metadata:
            result.update(page=page, limit=3, total=11, pagecount=4)
        return httpx.Response(200, json=result)

    async def listing(self, parent=t4_browser.LIB_GUID, **body):
        response = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": t4_browser.LIB_GUID, "parent_guid": parent,
            "page": 1, "page_size": 22, **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    async def plugin(self):
        return (await self.listing())["list"][0]["guid"]

    async def category(self):
        home = await self.listing(await self.plugin())
        return next(row["guid"] for row in home["list"] if row["title"] == "电视剧")

    async def test_library_is_available_without_other_virtual_libraries(self):
        self.config.save({"hot_library_enabled": False, "catalog_library_enabled": False})
        response = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual([r["guid"] for r in response.json()["data"]], [t4_browser.LIB_GUID])
        response = await self.client.get("/v/api/v1/mdb/" + t4_browser.LIB_GUID)
        self.assertEqual(response.json()["data"]["name"], "T4 选片")
        self.assertFalse(any(r.url.host == "t4.test" for r in self.calls))

    async def test_upstream_library_acl_applies_to_root_and_child_directories(self):
        plugin = await self.plugin()
        self.calls.clear()
        self.config.save({"lib_acl": {"child": ["__online__"]}})
        child = {"known": True, "guid": "child", "is_admin": False}
        with patch.object(appmod.fnusers, "resolve", return_value=child):
            libraries = (await self.client.get("/v/api/v1/mediadb/list")).json()["data"]
            self.assertNotIn(t4_browser.LIB_GUID, [row["guid"] for row in libraries])
            for guid in (t4_browser.LIB_GUID, plugin):
                detail = await self.client.get("/v/api/v1/item/" + guid)
                self.assertEqual(detail.json()["code"], -6)
                listing = await self.client.post("/v/api/v1/item/list", json={
                    "parent_guid": guid})
                self.assertEqual(listing.json()["data"], [])
            self.config.save({"lib_acl": {"child": ["t4", "__online__"]}})
            detail = await self.client.get("/v/api/v1/item/" + plugin)
            self.assertEqual(detail.json()["code"], 0)
        self.assertFalse(any(row.url.host == "t4.test" for row in self.calls))

    async def test_upstream_global_hide_and_library_management_keep_t4_key(self):
        plugin = await self.plugin()
        self.config.save({"lib_hidden": ["t4"]})
        detail = await self.client.get("/v/api/v1/mdb/" + t4_browser.LIB_GUID)
        self.assertEqual(detail.json()["code"], -6)
        detail = await self.client.get("/v/api/v1/item/" + plugin)
        self.assertEqual(detail.json()["code"], -6)
        libraries = (await self.client.get("/v/api/v1/mediadb/list")).json()["data"]
        self.assertNotIn(t4_browser.LIB_GUID, [row["guid"] for row in libraries])
        managed = next(row for row in appmod._virtual_lib_list() if row["kind"] == "t4")
        self.assertEqual(managed["key"], "t4")
        self.assertTrue(managed["hidden"])

    async def test_t4_detail_keeps_upstream_debounced_storage(self):
        title = t4.map_title({"vod_id": "flush", "vod_name": "Flush",
                              "vod_play_from": "A", "vod_play_url": "第1集$a1"},
                             self.source, detail=True)
        guid = inject.brief_of(title)["guid"]
        with patch.object(self.store, "_schedule_flush") as scheduled:
            brief = self.store.replace_t4_detail(guid, title)
        scheduled.assert_called_once_with()
        self.assertEqual(self.store.tv_episode_guids(guid),
                         [self.store.t4_episode_guid(guid, "episode:1")])
        self.assertTrue(brief["lines"])

    async def test_root_preserves_plugin_order_and_does_not_run_home(self):
        self.config.import_t4_sources([dict(self.source, name="片单导航",
                                          api=fixtures.API + "&site=nav", indexs=True)])
        data = await self.listing()
        self.assertEqual([r["title"] for r in data["list"]], ["Test T4", "片单导航"])
        self.assertTrue(all(r["type"] == "Directory" for r in data["list"]))
        self.assertFalse(any(r.url.host == "t4.test" for r in self.calls))
        self.assertNotIn("token=", json.dumps(data))
        self.assertNotIn("http://", json.dumps(data))

    async def test_same_name_plugins_keep_separate_identity(self):
        self.config.import_t4_sources([dict(self.source, api=fixtures.API + "&site=other")])
        data = await self.listing()
        self.assertEqual([r["title"] for r in data["list"]], ["Test T4", "Test T4 (2)"])
        self.assertNotEqual(data["list"][0]["guid"], data["list"][1]["guid"])

    async def test_full_plugin_library_pagination_never_starts_crawlers(self):
        self.config.save({"sources": []})
        self.config.import_t4_sources([dict(
            self.source, name=f"Plugin {i}", site_key=f"plugin-{i}",
            api=fixtures.API + f"&site=plugin-{i}",
        ) for i in range(179)])
        before = len(self.calls)
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            self.client.headers["User-Agent"] = ua
            cards = []
            for page in range(1, 10):
                data = await self.listing(page=page, page_size=22)
                self.assertEqual(data["total"], 179)
                cards.extend(data["list"])
            self.assertEqual([r["title"] for r in cards],
                             [f"Plugin {i}" for i in range(179)])
            self.assertEqual(len({r["guid"] for r in cards}), 179)
        self.assertEqual(len(self.calls), before)

    async def test_nonsearchable_source_has_home_categories_and_folders(self):
        data = await self.listing(await self.plugin())
        self.assertEqual([r["title"] for r in data["list"]],
                         ["电视剧", "测试剧", "网盘目录"])
        folder = next(r for r in data["list"] if r["title"] == "网盘目录")
        self.assertEqual(folder["type"], "Directory")
        self.assertEqual(folder["can_play"], 0)
        self.assertIsNone(self.store.get_title_by_guid(folder["guid"]))
        nested = await self.listing(folder["guid"])
        self.assertEqual(nested["list"][0]["title"], "测试剧")
        self.assertEqual(len(nested["jump_list"]), 3)
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params for r in self.calls))

    async def test_native_window_handles_different_page_sizes_and_filters(self):
        guid = await self.category()
        first = await self.listing(guid, page_size=5)
        second = await self.listing(guid, page=2, page_size=5)
        third = await self.listing(guid, page=3, page_size=5)
        self.assertEqual(first["total"], 12)
        self.assertEqual([r["title"] for r in first["list"]],
                         ["年份", "节目0", "节目1", "节目2", "节目3"])
        self.assertEqual([r["title"] for r in second["list"]],
                         [f"节目{i}" for i in range(4, 9)])
        self.assertEqual([r["title"] for r in third["list"]], ["节目9", "节目10"])
        movies = [r for result in (first, second, third) for r in result["list"]
                  if r["type"] != "Directory"]
        self.assertEqual(len({r["guid"] for r in movies}), 11)
        self.assertTrue(all(r["type"] == "TV" for r in movies))
        choices = await self.listing(first["list"][0]["guid"])
        selected = next(r["guid"] for r in choices["list"] if "2026" in r["title"])
        filtered = await self.listing(selected)
        self.assertEqual(self.filters, {"year": "2026"})
        self.assertTrue(all(r["type"] == "TV" for r in filtered["list"]
                            if r["type"] != "Directory"))
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_category_type_reaches_detail_route_in_four_client_profiles(self):
        parent = await self.category()
        raw = {"vod_id": "movie-1", "vod_name": "测试剧"}
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            self.client.headers["User-Agent"] = ua
            with patch.object(self.browser, "_window", return_value=([raw], 1)):
                data = await self.listing(parent)
            card = next(r for r in data["list"] if r["title"] == "测试剧")
            self.assertEqual(card["type"], "TV")
            detail = await self.client.get("/v/api/v1/item/" + card["guid"])
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["data"]["type"], card["type"])
            self.assertTrue(self.store.tv_episode_guids(card["guid"]))
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_category_type_is_inherited_by_filtered_and_nested_directories(self):
        category = await self.category()
        first = await self.listing(category)
        choices = await self.listing(first["list"][0]["guid"])
        selected = next(r["guid"] for r in choices["list"] if "2026" in r["title"])
        with patch.object(self.browser, "_window", return_value=([
                {"vod_id": "pan", "vod_name": "资源目录", "vod_tag": "folder"}], 1)):
            folders = await self.listing(selected)
        child = next(r["guid"] for r in folders["list"] if r["title"] == "资源目录")
        with patch.object(self.browser, "_window", return_value=([
                {"vod_id": "unknown", "vod_name": "测试剧"}], 1)):
            data = await self.listing(child)
        self.assertEqual(data["list"][0]["type"], "TV")
        restored = t4_browser.T4Browser(self.store, self.config)
        self.assertIn("电视剧", restored._category_type_name(child))
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params
                             for r in self.calls))

    async def test_explicit_type_wins_and_home_does_not_guess_from_title(self):
        parent = await self.category()
        cards = self.browser._rows([
            {"vod_id": "film", "vod_name": "测试剧", "type_name": "动画电影"},
            {"vod_id": "series", "vod_name": "测试片"},
        ], self.source, parent)
        self.assertEqual([r["type"] for r in cards], ["Movie", "TV"])
        home = await self.plugin()
        unknown = self.browser._rows([
            {"vod_id": "unknown", "vod_name": "电视剧标题"}], self.source, home)[0]
        node = self.browser.node(unknown["guid"])
        self.assertEqual(self.store.get_title_by_guid(node["target_guid"])["type_name"], "")
        self.assertEqual(unknown["type"], "Directory")
        self.assertEqual(unknown["can_play"], 0)

    async def test_untyped_title_resolves_lazily_to_the_real_native_type(self):
        home = await self.plugin()
        for media_type, playlist, expected in (
                ("电视剧", "第1集$one#第2集$two", "TV"),
                ("电影", "正片$film", "Movie")):
            for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                       "com.trim.media.ios"):
                self.client.headers["User-Agent"] = ua
                raw = {"vod_id": media_type + ua, "vod_name": "Untyped"}
                before = len(self.calls)
                card = self.browser._rows([raw], self.source, home)[0]
                self.assertEqual(card["type"], "Directory")
                self.assertEqual(len(self.calls), before)
                node = self.browser.node(card["guid"])
                title = t4.map_title(dict(raw, type_name=media_type,
                    vod_play_from="A", vod_play_url=playlist), self.source, detail=True)
                with patch.object(t4, "detail", return_value=title) as detail:
                    data = await self.listing(card["guid"])
                    self.assertEqual(data["total"], 1)
                    resolved = data["list"][0]
                    self.assertEqual(resolved["guid"], node["target_guid"])
                    self.assertEqual(resolved["type"], expected)
                    self.assertEqual(resolved["parent_guid"], card["guid"])
                    await self.listing(card["guid"])
                    self.assertEqual(detail.await_count, 1)
                self.assertFalse(any("play" in r.url.params for r in self.calls[before:]))

    async def test_untyped_search_directory_survives_restart_without_loading_details(self):
        raw = {"vod_id": "unknown", "vod_name": "Untyped"}
        brief = inject.brief_of(t4.map_title(raw, self.source))
        self.store.remember_merged([brief])
        card = self.browser.media_card(brief)
        restored = t4_browser.T4Browser(self.store, self.config)
        self.assertEqual(restored.card(card["guid"])["type"], "Directory")
        self.assertEqual(restored.node(card["guid"])["target_guid"], brief["guid"])
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params
                             for r in self.calls))

    async def test_hidden_library_keeps_untyped_search_usable_without_eager_requests(self):
        self.config.save({"t4_library_enabled": False})
        brief = inject.brief_of(t4.map_title(
            {"vod_id": "unknown-search", "vod_name": "Untyped"}, self.source))
        self.store.remember_merged([brief])
        with patch.object(appmod, "_aggregate_briefs", return_value=[brief]), \
                patch.object(appmod, "_search_cache", {}), \
                patch.object(appmod, "_search_inflight", {}):
            cards = await appmod.online_search("Untyped", self.network)
        self.assertEqual(cards[0]["type"], "Directory")
        self.assertEqual(self.browser.sources(), [])
        title = t4.map_title({
            "vod_id": "unknown-search", "vod_name": "Untyped", "type_name": "电视剧",
            "vod_play_from": "A", "vod_play_url": "S01E01$a#S02E01$b",
        }, self.source, detail=True)
        with patch.object(t4, "detail", return_value=title):
            data = await self.listing(cards[0]["guid"])
        self.assertEqual(data["list"][0]["type"], "TV")
        self.assertEqual(data["list"][0]["number_of_seasons"], 2)
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_membership_label_does_not_override_category_media_type(self):
        parent = await self.category()
        raw = {"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "大会员"}
        card = self.browser._rows([raw], self.source, parent)[0]
        self.assertEqual(card["type"], "TV")
        brief = self.store.get_title_by_guid(card["guid"])
        self.assertEqual(brief["type_name"], "电视剧")
        detail_row = dict(raw, vod_play_from="A", vod_play_url="第1集$id-1")
        with patch.object(t4, "request", return_value={"list": [detail_row]}):
            title = await t4.detail(self.network, self.source, brief["locator"], brief)
        self.assertEqual(title.type_name, "电视剧")
        self.assertEqual(raw["type_name"], "大会员")

    async def test_cached_list_keeps_loaded_type_and_episodes(self):
        parent = await self.category()
        row = {"vod_id": "movie-1", "vod_name": "测试剧"}
        first = self.browser._rows([row], self.source, parent)[0]
        detail = await self.client.get("/v/api/v1/item/" + first["guid"])
        self.assertEqual(detail.json()["data"]["type"], "TV")
        before = self.store.get_title_by_guid(first["guid"])
        with patch.object(self.store, "remember_merged",
                          wraps=self.store.remember_merged) as remember:
            repeated = self.browser._rows([row], self.source, parent)[0]
        remember.assert_not_called()
        self.assertEqual(repeated["type"], "TV")
        self.assertGreater(repeated["number_of_episodes"], 1)
        self.assertEqual(self.store.get_title_by_guid(first["guid"]), before)

    async def test_restored_category_without_hint_uses_its_label_not_source_name(self):
        parent = await self.category()
        self.browser.node(parent).pop("category_type_name", None)
        self.browser.node(parent)["title"] = "豆瓣·热门电视剧 · 2024"
        self.assertIn("电视剧", self.browser._category_type_name(parent))
        home = await self.plugin()
        self.browser.node(home)["title"] = "电视剧来源"
        self.assertEqual(self.browser._category_type_name(home), "")

    async def test_metadata_free_pagination_finishes_on_short_last_page(self):
        self.no_metadata = True
        guid = await self.category()
        titles = []
        for page in range(1, 4):
            data = await self.listing(guid, page=page, page_size=5)
            titles.extend(r["title"] for r in data["list"] if r["type"] != "Directory")
        self.assertEqual(titles, [f"节目{i}" for i in range(11)])
        self.assertEqual(data["total"], 12)

    async def test_ext_and_extend_are_independent_in_get_and_post(self):
        for method in ("GET", "POST"):
            source = dict(self.source, method=method, ext={"init": "fixture"})

            def response(request):
                params = json.loads(request.content) if method == "POST" else request.url.params
                self.assertEqual(json.loads(params["ext"]), {"init": "fixture"})
                self.assertEqual(json.loads(params["extend"]), {"year": "2026"})
                self.assertEqual(request.url.params["token"], "fixture")
                return httpx.Response(200, json={"list": []})

            async with type(self.network)(transport=httpx.MockTransport(response)) as client:
                await t4.category(client, source, "电视剧", 2, {"year": "2026"})

    async def test_singleflight_bounded_cache_and_restart_directory_identity(self):
        guid = await self.plugin()
        await asyncio.gather(*(self.browser.listing(self.network, guid, {}) for _ in range(12)))
        self.assertEqual(len([r for r in self.calls if r.url.host == "t4.test"]), 1)
        self.assertEqual(self.browser._locks, {})
        restored = t4_browser.T4Browser(self.store, self.config)
        self.assertEqual(restored.card(guid)["title"], "Test T4")
        for page in range(1, 40):
            await restored._raw(self.network, self.source, "series", page, {})
        self.assertLessEqual(len(restored._cache), 32)
        self.assertLessEqual(restored._cache_bytes, 4 * 1024 * 1024)

    async def test_disabled_source_cannot_be_browsed_from_cached_node(self):
        guid = await self.plugin()
        await self.listing(guid)
        self.config.save({"sources": [dict(self.source, enabled=False)]})
        response = await self.client.post("/v/api/v1/item/list",
                                          json={"parent_guid": guid})
        self.assertEqual(response.status_code, 502)
        self.assertIn("停用", response.json()["msg"])

    async def test_native_directory_detail_and_parent_only_routing(self):
        guid = await self.plugin()
        response = await self.client.get("/v/api/v1/item/" + guid)
        self.assertEqual(response.json()["data"]["type"], "Directory")
        self.assertEqual(response.json()["data"]["can_play"], 0)
        response = await self.client.post("/v/api/v1/item/list", json={"parent_guid": guid})
        self.assertEqual(response.json()["code"], 0)
        self.assertTrue(response.json()["data"]["list"])

    async def test_home_card_reuses_multi_source_episode_player_without_preparse(self):
        home = await self.listing(await self.plugin())
        guid = next(r["guid"] for r in home["list"] if r["title"] == "测试剧")
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        detail = await self.client.get("/v/api/v1/item/" + guid)
        self.assertEqual(detail.json()["data"]["type"], "TV")
        eps = self.store.tv_episode_guids(guid)
        self.assertEqual(self.store.episode_lines(eps[1]), ["A", "B"])
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        info = (await self.client.post("/v/api/v1/play/info",
                                       json={"item_guid": eps[1]})).json()["data"]
        played = await self.client.post("/v/api/v1/play/play",
                                        json={"media_guid": info["media_guid"], "line": "B"})
        self.assertEqual(played.json()["data"]["play_link"], "https://media.test/b2.mp4")
        await self.listing(await self.plugin())
        self.assertTrue(self.store.get_title_by_guid(guid)["lines"])

    async def test_client_profiles_and_error_does_not_become_empty_success(self):
        guid = await self.plugin()
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            self.client.headers["User-Agent"] = ua
            data = await self.listing(guid)
            self.assertEqual(data["list"][0]["type"], "Directory")
            self.assertIn("path", data["list"][0])
            self.assertIn("video_guid", data["list"][0])
        self.browser._cache.clear()
        self.browser._cache_bytes = 0
        self.broken = True
        response = await self.client.post("/v/api/v1/item/list", json={"parent_guid": guid})
        self.assertEqual(response.status_code, 502)
        self.assertIn("503", response.json()["msg"])
        self.assertNotIn("token=", response.text)

    async def test_cancelled_browse_does_not_leave_locks_or_pending_requests(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(20)
            finally:
                cancelled.set()

        with patch.object(t4, "home", side_effect=slow):
            task = asyncio.create_task(self.browser._raw(self.network, self.source))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.browser._locks, {})

    async def test_cross_page_failure_cancels_other_page_requests(self):
        cancelled = asyncio.Event()

        async def raw(_client, _source, _tid=None, page=1, filters=None):
            if page == 1:
                return {"list": [{"vod_id": "a"}] * 3, "limit": 3, "total": 30}
            if page == 2:
                await asyncio.sleep(0.02)
                raise t4.T4Error("T4 服务返回 HTTP 503")
            try:
                await asyncio.sleep(20)
            finally:
                cancelled.set()

        with patch.object(self.browser, "_raw", side_effect=raw):
            with self.assertRaises(t4.T4Error):
                await self.browser._window(self.network, self.source,
                                           {"tid": "test", "filters": {}}, 3, 7)
        self.assertTrue(cancelled.is_set())

    async def test_browse_concurrency_is_bounded_and_lock_entries_are_reclaimed(self):
        active = peak = 0

        async def home(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"class": [], "list": []}

        with patch.object(t4, "home", side_effect=home):
            await asyncio.gather(*(self.browser._raw(
                self.network, dict(self.source, ext=str(i))) for i in range(20)))
        self.assertLessEqual(peak, 4)
        self.assertEqual(self.browser._locks, {})

    async def test_home_wrong_schema_returns_protocol_error(self):
        async with type(self.network)(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"class": [], "list": "not-a-list"}))) as client:
            with self.assertRaises(t4.T4Error):
                await t4.home(client, self.source)

    async def test_cache_reclaims_idle_results_and_has_no_secret_in_keys(self):
        guid = await self.plugin()
        await self.listing(guid)
        self.assertFalse(any("token" in key for key in self.browser._cache))
        with patch.object(t4_browser.time, "monotonic", return_value=10**12):
            await self.browser._raw(self.network, self.source, "pan")
        self.assertEqual(len(self.browser._cache), 1)

    async def test_home_switch_and_source_delete_remove_entry(self):
        self.config.save({"t4_library_enabled": False,
                          "hot_library_enabled": False, "catalog_library_enabled": False})
        response = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(response.json()["data"], [])
        self.config.save({"t4_library_enabled": True})
        self.config.remove_source(self.source["id"])
        self.assertEqual((await self.listing())["total"], 0)

    async def test_cached_page_does_not_rewrite_titles_or_rebuild_playback_indexes(self):
        guid = await self.plugin()
        await self.listing(guid)
        with patch.object(self.store, "remember_merged", wraps=self.store.remember_merged) as remember:
            await self.listing(guid)
        remember.assert_not_called()


if __name__ == "__main__":
    unittest.main()
