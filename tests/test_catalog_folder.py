"""二级线路墙冷片（含海量分类 catalog）按需搜索绑定回归。

未在 store 的 guid 进入二级目录时，_vod_line_folder 统一调 ensure_folder_bound
主动按片名搜索落库，而不是直接返回空目录、等用户手动刷新。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402
import client as clientmod  # noqa: E402


def _brief(g="cat-og"):
    return {
        "guid": g, "title": "海量片", "year": "2026", "type_name": "电视剧",
        "poster": "https://x.test/p.jpg", "overview": "", "remarks": "",
        "score": "", "actor": "", "director": "", "area": "",
        "lines": {"lzi::lzm3u8": [
            {"title": "第01集", "url": "https://x.test/1.m3u8",
             "direct": True, "headers": {"Referer": "https://x.test"}}]},
        "catalog_cat": "tv",
    }


class CatalogFolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_calls_ensure_bound_when_not_in_store(self):
        g = "cat-cold-1"
        bound = _brief(g)
        with patch.object(appmod.store, "get_title_by_guid",
                          side_effect=[None, bound]), \
                patch.object(appmod, "ensure_folder_bound",
                             new=AsyncMock(return_value=bound)) as ensure, \
                patch.object(appmod.store, "target_lines",
                             return_value=[{"line": "lzi::lzm3u8", "m3u8": True}]), \
                patch.object(appmod.store, "list_episodes", return_value=["ep"]), \
                patch.object(appmod, "_vod_line_card",
                             side_effect=lambda b, ln, idx, p: {"type": "TV",
                                                                 "guid": "card"}), \
                patch.object(appmod, "_vod_pansou_card", return_value=None), \
                patch.object(appmod.history, "resume_episode",
                             return_value=(None, 0)):
            resp = await appmod._vod_line_folder(
                g, {}, clientmod.Client(kind="ios"))
        ensure.assert_called_once_with(g)
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["total"], 1)

    async def test_ensure_bound_uses_catalog_title_hint(self):
        # catalog 冷片非 chart guid：_folder_title_hint 从 catalog 内存取片名
        g = "cat-cold-2"
        with patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"title": "海量片", "year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None):
            title = appmod._folder_title_hint(g)
        self.assertEqual(title, "海量片")

    async def test_unknown_guid_returns_empty(self):
        g = "unknown-og"
        with patch.object(appmod, "ensure_folder_bound",
                          new=AsyncMock(return_value=None)), \
                patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid", return_value=None):
            resp = await appmod._vod_line_folder(
                g, {}, clientmod.Client(kind="ios"))
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["list"], [])

    async def test_prefetch_folder_binding_nonblocking_and_skips_bound(self):
        # folder 详情请求触发非阻塞预绑定：已绑定不启动；未绑定启动一次且不抛错。
        appmod._FOLDER_BIND_FUT.clear()
        # 未绑定：会启动绑定（mock 掉实际搜索，避免网络）
        with patch.object(appmod.store, "get_title_by_guid", return_value=None), \
                patch.object(appmod, "_start_folder_bind",
                             return_value=object()) as start:
            appmod.prefetch_folder_binding("g1")  # 非阻塞、不 await
        start.assert_called_once_with("g1")
        # 已绑定：不再启动
        with patch.object(appmod.store, "get_title_by_guid",
                          return_value={"guid": "g2"}), \
                patch.object(appmod, "_start_folder_bind") as start2:
            appmod.prefetch_folder_binding("g2")
        start2.assert_not_called()
        # 异常被吞掉、不向上抛（保证详情请求不受影响）
        with patch.object(appmod.store, "get_title_by_guid",
                          side_effect=RuntimeError("boom")):
            appmod.prefetch_folder_binding("g3")
        appmod._FOLDER_BIND_FUT.clear()


if __name__ == "__main__":
    unittest.main()
