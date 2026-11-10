"""TVBox 式二级线路海报墙与固定线路 VOD 播放。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
from store import episode_guid, ep_media_guid


def brief(tv=False):
    lines = {
        "baofeng::bfzym3u8": [{"title": "第01集" if tv else "正片",
                                "url": "https://baofeng.test/v.m3u8", "direct": True}],
        "lzi::lzm3u8": [{"title": "第01集" if tv else "正片",
                          "url": "https://lzi.test/v.m3u8", "direct": True}],
    }
    return {"guid": "og", "title": "蜘蛛侠：崭新之日", "year": "2026",
            "type_name": "电视剧" if tv else "电影", "poster": "https://img.test/p.jpg",
            "overview": "测试简介", "lines": lines}


def target(line="baofeng::bfzym3u8", index=0):
    return {"url": "https://baofeng.test/v.m3u8", "headers": {},
            "line": line, "index": index, "total": 1,
            "ep_title": "第01集", "m3u8": True}


def multi_episode_brief(ep_count=8):
    def ep(i):
        return {"title": f"第{i+1:02d}集", "url": f"https://lzi.test/v{i}.m3u8",
                "direct": True}
    return {"guid": "og", "title": "多集内容", "year": "2026",
            "type_name": "日韩动漫", "poster": "https://img.test/p.jpg",
            "overview": "", "lines": {"lzi::lzm3u8": [ep(i) for i in range(ep_count)]}}


class VodResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.brief = multi_episode_brief(8)

    def tearDown(self):
        guids = [episode_guid("og", "", i) for i in range(8)]
        appmod.history.delete(guids)

    def test_resume_index_and_play_info_locate_exact_episode_and_ts(self):
        all_eg = [episode_guid("og", "", i) for i in range(8)]
        for i in range(3):
            appmod.history.set_position(all_eg[i], 1000, 1000)
        appmod.history.set_position(all_eg[3], 600, 1200)
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.brief), \
                patch.object(appmod.store, "tv_episode_guids", return_value=all_eg), \
                patch.object(appmod.store, "get_episode",
                             side_effect=lambda g: {
                                 "index": all_eg.index(g), "ep_guid": g,
                                 "online_guid": "og", "season_guid": "season",
                                 "title": self.brief["title"],
                                 "ep_title": f"第{all_eg.index(g)+1}集"}
                             if g in all_eg else None), \
                patch.object(appmod.store, "resolve_episode",
                             side_effect=lambda og, line, idx: target(line, idx)), \
                patch.object(appmod.store, "list_episodes",
                             side_effect=lambda og, line=None: self.brief["lines"][line]):
            idx = appmod._vod_resume_index(self.brief)
            self.assertEqual(idx, 3)
            line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", idx)
            response = appmod._vod_play_info(
                line_guid, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["guid"], all_eg[3])
        self.assertEqual(data["ts"], 600)
        # Web 端多集内容为 Episode 形状（显示选集入口）；续播定位不受影响
        self.assertEqual(data["type"], "Episode")

    def test_history_guid_normalization_collapses_line_identity(self):
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.brief):
            line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 3)
            normalized = appmod._normalize_history_guid(line_guid)
        self.assertEqual(normalized, episode_guid("og", "", 3))
        movie_guid = appmod._line_item_guid("movie-og", "lzi::lzm3u8", 0)
        with patch.object(appmod.store, "get_title_by_guid", return_value=None):
            self.assertEqual(appmod._normalize_history_guid(movie_guid), "movie-og")
        self.assertTrue(appmod._is_online_item_guid(line_guid))

    def test_anime_variety_and_tv_all_classify_as_multi_episode(self):
        for type_name in ("国产剧", "日韩动漫", "港台综艺", "欧美综艺", "日本动画番剧"):
            b = dict(self.brief, type_name=type_name)
            self.assertTrue(appmod.inj.is_tv(b), type_name)


class VodPansouCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = brief(tv=True)

    def test_pansou_card_only_when_available(self):
        # PanSou 不可用时不出网盘卡
        with patch.object(appmod, "_pansou_available", return_value=False):
            self.assertIsNone(appmod._vod_pansou_card(self.b, 0, "og"))
        # 可用时返回 TV 模型、固定网盘线路 guid
        with patch.object(appmod, "_pansou_available", return_value=True), \
                patch.object(appmod, "PANSOU_LINE", "builtin-pansou::PanSou网盘高清"), \
                patch.object(appmod, "PANSOU_LABEL", "🎞️网盘高清（PanSou）"), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]):
            card = appmod._vod_pansou_card(self.b, 0, "og")
        self.assertIsNotNone(card)
        self.assertEqual(card["type"], "TV")
        self.assertEqual(card["number_of_episodes"], 1)
        parsed = appmod._parse_line_item_guid(card["guid"])
        self.assertEqual(parsed[1], "builtin-pansou::PanSou网盘高清")

    def test_fixed_media_target_resolves_pansou_virtual_line(self):
        line_guid = appmod._line_item_guid(
            "og", "builtin-pansou::PanSou网盘高清", 0)
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.b), \
                patch.object(appmod, "_is_pansou_line_name", return_value=True), \
                patch.object(appmod, "_pansou_manual_target",
                             return_value={"line": "builtin-pansou::PanSou网盘高清",
                                           "url": "pansou://manual",
                                           "_pansou_manual": True, "xsrc": True}):
            t = appmod._fixed_media_target(line_guid)
        self.assertTrue(t["_fixed_line"])
        self.assertTrue(t["_pansou_manual"])
        self.assertEqual(t["online_guid"], "og")

    async def test_folder_appends_pansou_card_at_end(self):
        m3u8 = dict(target("lzi::lzm3u8"), resolve=False, m3u8=True)
        with patch.object(appmod.store, "get_title_by_guid", return_value=self.b), \
                patch.object(appmod.store, "target_lines", return_value=[m3u8]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode",
                             return_value=(None, 0)), \
                patch.object(appmod, "_pansou_available", return_value=True), \
                patch.object(appmod, "PANSOU_LINE",
                             "builtin-pansou::PanSou网盘高清"), \
                patch.object(appmod, "PANSOU_LABEL", "🎞️网盘高清（PanSou）"), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]):
            resp = await appmod._vod_line_folder(
                "og", {}, clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        # 一张 CMS 来源卡 + 一张网盘卡
        self.assertEqual(data["total"], 2)
        self.assertTrue(
            appmod._is_pansou_line_name(
                appmod._parse_line_item_guid(data["list"][-1]["guid"])[1]))


class VodLineFolderTests(unittest.IsolatedAsyncioTestCase):
    def test_first_level_chart_is_directory(self):
        row = appmod.chart_card({"guid": "og", "cat": "movie", "title": "蜘蛛侠：崭新之日",
                                  "year": "2026", "rating": 8.0, "cover": "https://img.test/p.jpg",
                                  "tags": [], "desc": ""})
        self.assertEqual(row["type"], "Directory")
        self.assertEqual(row["can_play"], 0)

    def test_movie_filter_does_not_hide_directory(self):
        item = {"guid": "og", "cat": "movie", "title": "蜘蛛侠：崭新之日",
                "year": "2026", "rating": 8.0, "cover": "https://img.test/p.jpg",
                "tags": [], "desc": ""}
        with patch.object(appmod.chart_cache, "items", return_value=[item]):
            rows = appmod._chart_rows_all({"tags": {"type": ["Movie"]}}, "movie")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "Directory")

    def test_folder_guid_is_idempotent_and_detail_is_directory(self):
        self.assertEqual(appmod._line_folder_guid("og"),
                         appmod._line_folder_guid(appmod._line_folder_guid("og")))
        b = brief()
        folder_guid = appmod._line_folder_guid("og")
        with patch.object(appmod.store, "get_title_by_guid", return_value=b):
            response = appmod._vod_folder_detail(folder_guid, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["guid"], folder_guid)
        self.assertEqual(data["type"], "Directory")
        self.assertEqual(data["can_play"], 0)
        self.assertEqual(data["play_item_guid"], "")
        # Directory 卡也必须含列表模型必填字段，否则 App/TV 进二级目录就渲染失败
        for k in ("video_guid", "poster", "path", "lan", "runtime",
                  "single_child_guid", "file_name", "ts", "watched"):
            self.assertIn(k, data, f"folder 详情卡缺列表必填字段 {k}")
        self.assertTrue(data["video_guid"])

    async def test_folder_tag_list_is_local_empty_filter_not_upstream(self):
        # 回归：line-folder 的 tag/list 必须本地返回空筛选条，透传官方会让 TV/盒子
        # 因未知 guid 的异常响应而整页“加载失败”。
        folder_guid = appmod._line_folder_guid("og")
        qs = ("parent_guid=" + folder_guid + "&is_favorite=0").encode()
        request = Request({"type": "http", "method": "GET",
                            "path": "/v/api/v1/tag/list", "query_string": qs,
                            "headers": [(b"user-agent",
                                         b"1.4.2 (com.trim.tv) okHttp/4.9.0")],
                            "app": appmod.app})
        called = {"upstream": False}

        async def fake_upstream(_req):
            called["upstream"] = True
            raise AssertionError("line-folder 不应透传官方")

        with patch.object(appmod, "forward_upstream", side_effect=fake_upstream):
            resp = await appmod.guard_native_tag_list(request)
        data = json.loads(resp.body)
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["genres"], [])
        self.assertFalse(called["upstream"])

    async def test_tv_folder_keeps_tv_model_and_episode_count(self):
        b = brief(tv=True)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "target_lines", return_value=[
                    target("baofeng::bfzym3u8"), target("lzi::lzm3u8")]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode", return_value=(None, 0)):
            response = await appmod._vod_line_folder("og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["mdb_category"], "TV")
        self.assertTrue(all(row["type"] == "TV" for row in data["list"]))
        self.assertTrue(all(row["number_of_episodes"] == 1 for row in data["list"]))
        self.assertTrue(data["list"][0]["title"].startswith("暴风资源｜"))
        parsed = appmod._parse_line_item_guid(data["list"][0]["guid"])
        self.assertEqual(parsed, ("og", "baofeng::bfzym3u8", 0))
        self.assertEqual(data["list"][0]["parent_guid"], appmod._line_folder_guid("og"))
        self.assertEqual(data["list"][0]["ancestor_guid"], appmod._line_folder_guid("og"))
        # 列表模型必填字段（缺失会导致 TV/盒子整库“加载失败”、iOS 卡片不渲染）
        required = ("video_guid", "poster", "path", "lan", "runtime",
                    "single_child_guid", "file_name", "ts", "watched")
        for k in required:
            self.assertIn(k, data["list"][0], f"线路卡缺列表必填字段 {k}")
        self.assertTrue(data["list"][0]["video_guid"])

    async def test_folder_aggregates_technical_lines_by_source(self):
        b = brief(tv=True)
        m3u8_line = dict(target("dytt::dyttm3u8"), resolve=False, m3u8=True)
        resolve_line = dict(target("dytt::dytt"), resolve=True, m3u8=False)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "target_lines", return_value=[
                    resolve_line, m3u8_line]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode", return_value=(None, 0)):
            response = await appmod._vod_line_folder("og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(
            appmod._parse_line_item_guid(data["list"][0]["guid"])[1], "dytt::dyttm3u8")

    async def test_folder_episode_count_uses_all_guids_not_only_direct(self):
        # 回归：选中解析线（list_episodes 因 direct=False 返回空）时，
        # 卡片集数仍须按 tv_episode_guids 的真实集数显示，不能变 0。
        b = brief(tv=True)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "target_lines", return_value=[
                    target("lzi::lzm3u8")]), \
                patch.object(appmod.store, "list_episodes", return_value=[]), \
                patch.object(appmod.store, "tv_episode_guids",
                             return_value=["ep1", "ep2", "ep3"]), \
                patch.object(appmod.history, "resume_episode",
                             return_value=(None, 0)):
            response = await appmod._vod_line_folder(
                "og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["number_of_episodes"], 3)

    def test_fixed_media_target_uses_exact_line(self):
        b = brief()
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()):
            result = appmod._fixed_media_target(appmod._line_item_guid("og", "baofeng::bfzym3u8", 0))
        self.assertEqual(result["line"], "baofeng::bfzym3u8")
        self.assertTrue(result["_fixed_line"])

    async def test_vod_play_info_keeps_original_history_identity(self):
        b = brief(tv=True)
        eg = episode_guid("og", "", 0)
        ep = {"ep_guid": eg, "online_guid": "og", "season_guid": "season-og",
              "index": 0, "season": 1, "title": b["title"], "ep_title": "第01集"}
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()), \
                patch.object(appmod.store, "tv_episode_guids", return_value=[eg]), \
                patch.object(appmod.store, "list_episodes", return_value=[target()]), \
                patch.object(appmod.store, "get_episode", return_value=ep):
            # App/TV 端保持 TV 模型（原生选集/右下角集数契约不变）
            app_resp = appmod._vod_play_info(
                appmod._line_item_guid("og", "lzi::lzm3u8", 0),
                clientmod.Client(kind="android"))
            app_data = json.loads(app_resp.body)["data"]
            self.assertEqual(app_data["type"], "TV")
            self.assertEqual(app_data["item"]["type"], "TV")
            self.assertEqual(app_data["guid"], eg)
            self.assertEqual(app_data["media_guid"],
                             ep_media_guid(eg, "lzi::lzm3u8"))
            self.assertEqual(app_data["item"]["number_of_episodes"], 1)

    async def test_web_vod_play_info_uses_episode_shape_with_line_scoped_guids(self):
        # 回归：Web 只在 play/info 为 Episode 形状时显示选集入口；二级线路墙此前返回
        # TV 形状导致无「选集」。修复后 Web 用 Episode 形状且 guid 全部带固定线路身份，
        # 使选集链锁定本线路（不竞速、不串线）。
        b = brief(tv=True)
        eg = episode_guid("og", "", 0)
        ep = {"ep_guid": eg, "online_guid": "og", "season_guid": "season-og",
              "index": 0, "season": 1, "title": b["title"], "ep_title": "第01集"}
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()), \
                patch.object(appmod.store, "get_episode", return_value=ep):
            resp = appmod._vod_play_info(line_guid, clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        # 顶层与 item 都是 Episode，触发 Web 选集入口
        self.assertEqual(data["type"], "Episode")
        self.assertEqual(data["item"]["type"], "Episode")
        # 选集链 guid 带固定线路身份（点选集走 line-item/line-season，不透传官方）
        self.assertEqual(data["item"]["guid"], line_guid)
        self.assertEqual(data["item"]["parent_guid"],
                         appmod._line_season_guid("og", "lzi::lzm3u8", 1))
        self.assertEqual(data["media_guid"], ep_media_guid(eg, "lzi::lzm3u8"))
        self.assertEqual(data["item"]["media_guid"],
                         ep_media_guid(eg, "lzi::lzm3u8"))
        # 带线路 media guid 能反查回固定线路 target（锁定线路、不竞速）
        def _resolve(og, line, index):
            return target(line or "lzi::lzm3u8", index)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", side_effect=_resolve):
            tgt = appmod._fixed_media_target(line_guid, None)
        self.assertEqual(tgt["line"], "lzi::lzm3u8")

    async def test_cold_folder_waits_for_binding_to_avoid_empty_page(self):
        b = brief()
        async def slow_bind(*args, **kwargs):
            await asyncio.sleep(0.05)
        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod.chart_cache, "is_chart_guid", return_value=True), \
                patch.object(appmod, "start_resolve_chart", return_value=None), \
                patch.object(appmod, "_chart_resolving", {"chart-og": slow_bind()}), \
                patch.object(appmod, "chart_card", return_value={"title": "测试"}):
            store_mock.get_title_by_guid.side_effect = [None, b]
            store_mock.target_lines.return_value = [target("lzi::lzm3u8")]
            response = await appmod._vod_line_folder(
                "chart-og", {}, clientmod.Client(kind="web"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)

    async def test_cold_folder_invokes_ensure_bound_and_clears_cooldown(self):
        # 冷片进入二级目录：必须触发 ensure_folder_bound 主动搜索绑定；
        # 仍未绑定时清掉短冷却，避免重进被 45s 冷却挡住持续空目录。
        called = {}

        async def fake_ensure(guid):
            called["guid"] = guid
            return None

        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod, "ensure_folder_bound", side_effect=fake_ensure), \
                patch.object(appmod, "_CHART_FAILED_TS", {"chart-og": 1.0}), \
                patch.object(appmod, "_CHART_FAIL_LEVEL", {"chart-og": ""}), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "小城良方"}), \
                patch.object(appmod.catalog, "find_brief_by_guid", return_value=None):
            store_mock.get_title_by_guid.return_value = None
            response = await appmod._vod_line_folder(
                "chart-og", {}, clientmod.Client(kind="android"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 0)
        self.assertEqual(called["guid"], "chart-og")
        self.assertNotIn("chart-og", appmod._CHART_FAILED_TS)
        self.assertNotIn("chart-og", appmod._CHART_FAIL_LEVEL)

    async def test_cold_folder_wait_constants_within_client_timeout(self):
        # 用户要求：给足加载时间，宁可在加载画面内等待也不提前返回空。实测客户端
        # 可容忍 6s+，全源最慢 ~3.7s，故总窗口放宽到 8s；快源软/硬超时均小于总窗口。
        self.assertLessEqual(appmod._FOLDER_BIND_WAIT, 8.0)
        self.assertLessEqual(appmod._FOLDER_DETAIL_BIND_WAIT, 8.0)
        self.assertLess(appmod._FOLDER_FAST_SOFT, appmod._FOLDER_BIND_WAIT)
        self.assertLess(appmod._FOLDER_FAST_HARD, appmod._FOLDER_BIND_WAIT)

    async def test_ensure_folder_bound_searches_by_title_and_binds_guid(self):
        # 未绑定冷片：按片名快速搜索，命中同名同年结果后把身份换成该 guid 落库。
        g = "cold-og"
        picked = {"guid": "other", "title": "冷片", "year": "2026",
                  "lines": {"lzi::lzm3u8": [{"url": "https://x/1.m3u8"}]}}

        async def fake_agg(title, maxn, **kw):
            return [picked]

        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod, "_norm_title", side_effect=lambda s: s), \
                patch.object(appmod, "schedule_prewarm_title"), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}):
            # 未绑定返回 None；落库后返回已绑定 brief
            store_mock.get_title_by_guid.side_effect = \
                lambda x: picked if store_mock.remember_merged.called else None
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNotNone(out)
        store_mock.remember_merged.assert_called_once()
        bound = store_mock.remember_merged.call_args[0][0][0]
        self.assertEqual(bound["guid"], g)  # 身份换成请求的 folder guid
        appmod._FOLDER_BIND_FUT.clear()

    async def test_ensure_folder_bound_returns_existing_without_search(self):
        # 已绑定直接返回，不触发任何搜索。
        existing = {"guid": "hot-og", "title": "热片", "lines": {}}
        with patch.object(appmod.store, "get_title_by_guid", return_value=existing), \
                patch.object(appmod, "_aggregate_briefs") as agg:
            out = await appmod.ensure_folder_bound("hot-og")
        self.assertIs(out, existing)
        agg.assert_not_called()

    def test_folder_picker_accepts_year_language_suffix_rejects_contains(self):
        # 资源站常把片名写成《美人余2026》《璀璨之城粤语》，二级目录应能匹配；
        # 但《突如其来》绝不能误绑《突如其来的假期》。
        year_brief = {"title": "美人余2026", "year": "2026", "lines": {"a": [1]}}
        lang_brief = {"title": "璀璨之城粤语", "year": "2026", "lines": {"a": [1]}}
        wrong_brief = {"title": "突如其来的假期", "year": "", "lines": {"a": [1]}}
        self.assertIsNotNone(
            appmod._pick_folder_brief("美人余", "", [year_brief]))
        self.assertIsNotNone(
            appmod._pick_folder_brief("璀璨之城", "", [lang_brief]))
        self.assertIsNone(
            appmod._pick_folder_brief("突如其来", "", [wrong_brief]))

    async def test_ensure_folder_bound_fast_miss_schedules_full_fallback(self):
        # 快源短软超时未命中：本次返回 None，但调度后台全源兜底，刷新即有。
        g = "fast-miss-og"
        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs",
                             new=AsyncMock(return_value=[])), \
                patch.object(appmod, "_norm_title", side_effect=lambda s: s), \
                patch.object(appmod, "schedule_prewarm_title"), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": ""}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=None), \
                patch.object(appmod, "_schedule_folder_full_fallback") as fb:
            store_mock = patch.object(appmod.store, "get_title_by_guid",
                                      return_value=None)
            store_mock.start()
            self.addCleanup(store_mock.stop)
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNone(out)
        fb.assert_called_once()
        self.assertEqual(fb.call_args[0][0], g)
        appmod._FOLDER_BIND_FUT.clear()
        appmod._FOLDER_FULL_FB.clear()

    async def test_ensure_folder_bound_fast_miss_schedules_background_fallback(self):
        # 快源未命中：先在实时预算剩余窗口内补一次全源，仍无则调度后台全源兜底，
        # 全程受 _FOLDER_BIND_WAIT 总预算约束（不拖过客户端超时）；用户重进即有。
        g = "fast-miss-bg-og"
        agg_calls = []

        async def fake_agg(keyword, maxn, **kw):
            agg_calls.append(kw)
            return []  # 快源/全源均无命中

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod, "_norm_title", side_effect=lambda s: s), \
                patch.object(appmod, "schedule_prewarm_title"), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": ""}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=None), \
                patch.object(appmod, "_schedule_folder_full_fallback") as fb, \
                patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod.store, "remember_merged"):
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNone(out)
        # 先快源（fast_only）、再补一次全源（非 fast_only）
        self.assertEqual(len(agg_calls), 2)
        self.assertTrue(agg_calls[0].get("fast_only"))
        self.assertFalse(agg_calls[1].get("fast_only"))
        # 实时预算耗尽仍无，后台全源兜底被调度
        fb.assert_called_once()
        appmod._FOLDER_BIND_FUT.clear()

    async def test_fast_resolve_skips_full_fallback_when_fast_misses(self):
        # 快速模式（folder/预取）：快源未中不得再做 25s 全源兜底，避免单飞任务被
        # 长兜底占住、二级目录复用后拖到 App 网关超时。播放链路仍走常规（含兜底）。
        appmod._chart_resolving.clear()
        appmod._CHART_FULL_FALLBACK.clear()
        appmod._CHART_FAILED_TS.pop("chart-og", None)
        agg_calls = []

        async def fake_agg(title, maxn, **kw):
            agg_calls.append(kw)
            return []  # 快源无命中

        item = {"title": "冷片", "cat": "movie"}
        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod.chart_cache, "get_guid", return_value=item), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod, "_best_chart_brief", return_value=None), \
                patch.object(appmod, "_schedule_full_fallback") as sched, \
                patch.object(appmod, "_chart_prefetch_sem",
                             return_value=__import__("asyncio").Semaphore(6)):
            store_mock.get_title_by_guid.return_value = None
            fut = appmod.start_resolve_chart(
                "chart-og", fast_soft_timeout=2.0, fast_skip_fallback=True)
            self.assertIsNotNone(fut)
            await fut
            sched.assert_called_once_with("chart-og")
        # 只调用一次快源聚合，没有第二次全源兜底
        self.assertEqual(len(agg_calls), 1)
        self.assertTrue(agg_calls[0].get("fast_only"))

    async def test_fast_miss_schedules_background_full_fallback(self):
        # 快速模式快源未中：不阻塞当前请求，但要调度后台完整绑定，刷新即可命中。
        appmod._chart_resolving.clear()
        appmod._CHART_FULL_FALLBACK.clear()
        appmod._CHART_FAILED_TS.pop("chart-og", None)
        scheduled = []

        def cap(guid):
            scheduled.append(guid)

        item = {"title": "冷片", "cat": "movie"}
        with patch.object(appmod, "store") as store_mock, \
                patch.object(appmod.chart_cache, "get_guid", return_value=item), \
                patch.object(appmod, "_aggregate_briefs",
                             new=AsyncMock(return_value=[])), \
                patch.object(appmod, "_best_chart_brief", return_value=None), \
                patch.object(appmod, "_schedule_full_fallback", side_effect=cap), \
                patch.object(appmod, "_chart_prefetch_sem",
                             return_value=__import__("asyncio").Semaphore(6)):
            store_mock.get_title_by_guid.return_value = None
            fut = appmod.start_resolve_chart(
                "chart-og", fast_soft_timeout=2.0, fast_skip_fallback=True)
            await fut
        self.assertEqual(scheduled, ["chart-og"])
        # 真实调度函数应可去重（同一 guid 不重复入队）
        appmod._CHART_FULL_FALLBACK.clear()
        with patch.object(appmod, "resolve_chart", new=AsyncMock()):
            appmod._schedule_full_fallback("g1")
            appmod._schedule_full_fallback("g1")
        self.assertEqual(appmod._CHART_FULL_FALLBACK, {"g1"})
        appmod._CHART_FULL_FALLBACK.clear()

    async def test_fixed_line_tv_exposes_seasons_and_line_scoped_episodes(self):
        b = brief(tv=True)
        folder_guid = appmod._line_folder_guid("og")
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "get_season", return_value={
                    "online_guid": "og", "season": 1}), \
                patch.object(appmod.inj, "season_list", return_value=[{
                    "guid": season_guid, "type": "Season", "title": "第1季",
                    "parent_guid": "og"}]), \
                patch.object(appmod.inj, "episode_list", return_value=[{
                    "guid": "ep1", "type": "Episode", "title": "第01集",
                    "episode_number": 1, "media_guid": "old-media",
                    "video_guid": "old-video"}]), \
                patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None)):
            request = Request({"type": "http", "method": "GET",
                               "path": f"/v/api/v1/season/list/{line_guid}",
                               "query_string": b"", "headers": [], "app": appmod.app})
            seasons = json.loads((await appmod.season_list_route(line_guid, request)).body)["data"]
            episodes = json.loads((await appmod.episode_list_route(line_guid, request)).body)["data"]
        self.assertEqual(seasons[0]["guid"], season_guid)
        self.assertEqual(episodes[0]["parent_guid"], season_guid)
        self.assertEqual(episodes[0]["guid"],
                         appmod._line_item_guid("og", "lzi::lzm3u8", 0))
        self.assertEqual(episodes[0]["media_guid"],
                         ep_media_guid("ep1", "lzi::lzm3u8"))

    def test_fixed_media_target_resolves_line_season_to_first_episode(self):
        # 回归：line-season guid 也须能解析为固定线路目标（第0集），
        # 否则播放页对季节点请求 stream/list 返回空，右下角集数/选集入口消失。
        b = brief(tv=True)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode",
                             return_value=target("lzi::lzm3u8")):
            t = appmod._fixed_media_target(season_guid, None)
        self.assertIsNotNone(t)
        self.assertEqual(t["line"], "lzi::lzm3u8")
        self.assertEqual(t["index"], 0)

    async def test_season_card_keeps_cms_wrapper_shape(self):
        # 普通 CMS 合成季卡必须保持 v1.0.0 负载形状：集数报 0、can_play=None，
        # 由客户端从 episode/list 自行统计；给真实集数/可播放会让飞牛 Web
        # 把包装季当真实季而隐藏集列表。
        b = brief(tv=True)
        season = {"guid": "season-1", "season": 1,
                  "episode_guids": ["e1", "e2", "e3"]}
        with patch.object(appmod.store, "get_title_by_guid", return_value=b):
            row = appmod.inj.season_item(b, season)
        self.assertEqual(row["type"], "Season")
        self.assertIsNone(row["can_play"])
        self.assertEqual(row["number_of_episodes"], 0)

    async def test_line_season_detail_reports_real_episode_count(self):
        # 回归：季卡详情必须给真实集数（普通 CMS 片此前被硬编码为 0，
        # 飞牛 Web 不会自行统计，导致季卡不显示集数聚合）。
        b = brief(tv=True)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        season_row = {"guid": "season-og", "type": "Season",
                      "title": "第1季", "season_number": 1,
                      "number_of_episodes": 3, "parent_guid": "og"}
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.inj, "is_tv", return_value=True), \
                patch.object(appmod.inj, "season_list", return_value=[season_row]):
            resp = appmod._vod_line_season_detail(
                season_guid, clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["type"], "Season")
        self.assertEqual(data["number_of_episodes"], 3)
        self.assertEqual(data["guid"], season_guid)

    async def test_post_stream_list_supports_fixed_line_card(self):
        b = brief(tv=True)
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        ep = {"ep_guid": "ep1", "online_guid": "og", "season_guid": "season-og",
              "index": 0, "title": b["title"], "ep_title": "第01集"}

        async def receive():
            return {"type": "http.request", "body": json.dumps(
                {"guid": line_guid}).encode()}

        request = Request({"type": "http", "method": "POST",
                           "path": "/v/api/v1/stream/list", "query_string": b"",
                           "headers": [], "app": appmod.app}, receive)
        fixed_target = dict(target("lzi::lzm3u8"), online_guid="og",
                            ep_guid="ep1", kind="episode", _fixed_line=True)
        with patch.object(appmod, "_fixed_media_target", return_value=fixed_target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]), \
                patch.object(appmod.store, "get_episode", return_value=ep), \
                patch.object(appmod.inj, "episode_stream_list", return_value={
                    "files": [{"guid": "file1", "can_play": 1}],
                    "video_streams": [{"media_guid": "media1"}],
                    "audio_streams": [], "subtitle_streams": []}), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.stream_list_post(request)
        data = json.loads(response.body)["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["files"][0]["can_play"], 1)
        self.assertEqual(data["video_streams"][0]["media_guid"], "media1")

    async def test_stream_list_supports_fixed_line_card(self):
        b = brief(tv=True)
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        ep = {"ep_guid": "ep1", "online_guid": "og", "season_guid": "season-og",
              "index": 0, "title": b["title"], "ep_title": "第01集"}
        fixed_target = dict(target("lzi::lzm3u8"), online_guid="og",
                            ep_guid="ep1", kind="episode", _fixed_line=True)
        with patch.object(appmod, "_fixed_media_target", return_value=fixed_target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "tv_episode_guids", return_value=["ep1"]), \
                patch.object(appmod.store, "get_episode", return_value=ep), \
                patch.object(appmod.inj, "episode_stream_list", return_value={
                    "files": [{"guid": "file1", "can_play": 1}],
                    "video_streams": [{"media_guid": "media1"}],
                    "audio_streams": [], "subtitle_streams": []}), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.stream_list(line_guid, Request(
                {"type": "http", "method": "GET", "path": "/v/api/v1/stream/list/x",
                 "query_string": b"", "headers": [], "app": appmod.app}))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["files"][0]["can_play"], 1)
        self.assertEqual(data["video_streams"][0]["media_guid"], "media1")

    async def test_play_play_fixed_line_has_no_auto_and_ignores_other_bitrate(self):
        b = brief()
        guid = appmod._line_item_guid("og", "baofeng::bfzym3u8", 0)
        async def receive():
            return {"type": "http.request", "body": json.dumps(
                {"media_guid": guid, "bitrate": 1}).encode()}
        request = Request({"type": "http", "method": "POST", "path": "/v/api/v1/play/play",
                           "query_string": b"", "headers": [], "app": appmod.app}, receive)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.store, "resolve_episode", return_value=target()), \
                patch.object(appmod, "_line_choices", return_value=[target(), target("lzi::lzm3u8")]), \
                patch.object(appmod, "_guess_wrapper", return_value=("HLS", True)), \
                patch.object(appmod, "_warm_preset_lines"), \
                patch.object(appmod, "_warm_playlist", new=AsyncMock()):
            response = await appmod.play_play(request)
        data = json.loads(response.body)["data"]
        self.assertIn("line=baofeng%3A%3Abfzym3u8", data["play_link"])
        self.assertNotIn("auto=1", data["play_link"])


    def _season_req(self, ua_kind):
        ua = {"web": "Mozilla/5.0 trimemedia-web Chrome",
              "android": "Dalvik/2.1.0 (Linux; U; Android 15; V2203A) okhttp/4.12"}[ua_kind]
        return Request({"type": "http", "method": "GET",
                        "path": "/v/api/v1/season/list/x",
                        "query_string": b"",
                        "headers": [(b"user-agent", ua.encode())],
                        "app": appmod.app})

    async def test_web_season_card_playable_with_real_count_android_keeps_wrapper(self):
        # 回归：飞牛 Web 不展开 can_play=null/集数0 的包装季，导致剧集不显示；
        # App/TV 必须保持 v1.0.0 包装形状（null/0），给真实集数反而隐藏集列表。
        b = brief(tv=True)
        line_guid = appmod._line_item_guid("og", "lzi::lzm3u8", 0)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        def fresh_seasons(_brief=None):
            return [{"guid": season_guid, "type": "Season", "title": "第1季",
                     "can_play": None, "number_of_episodes": 0,
                     "parent_guid": "og"}]
        with patch.object(appmod, "_load_t4_detail", new=AsyncMock(return_value=None)), \
                patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.inj, "season_list", side_effect=fresh_seasons), \
                patch.object(appmod, "_line_episode_count", return_value=12):
            web = json.loads((await appmod.season_list_route(
                line_guid, self._season_req("web"))).body)["data"]
            android = json.loads((await appmod.season_list_route(
                line_guid, self._season_req("android"))).body)["data"]
        self.assertEqual(web[0]["can_play"], 1)
        self.assertEqual(web[0]["number_of_episodes"], 12)
        # App 端保持 CMS 包装形状不变
        self.assertIsNone(android[0]["can_play"])
        self.assertEqual(android[0]["number_of_episodes"], 0)

    async def test_web_line_season_detail_playable(self):
        # line-season guid 的 item 详情在 Web 端同样要可播放、带真实集数。
        b = brief(tv=True)
        season_guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
        with patch.object(appmod.store, "get_title_by_guid", return_value=b), \
                patch.object(appmod.inj, "is_tv", return_value=True), \
                patch.object(appmod.inj, "season_list", return_value=[{
                    "guid": "season-og", "type": "Season", "title": "第1季",
                    "season_number": 1, "can_play": None,
                    "number_of_episodes": 0, "parent_guid": "og"}]), \
                patch.object(appmod, "_line_episode_count", return_value=12):
            resp = appmod._vod_line_season_detail(
                season_guid, clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["can_play"], 1)
        self.assertEqual(data["number_of_episodes"], 12)
        self.assertEqual(data["guid"], season_guid)


if __name__ == "__main__":
    unittest.main()
