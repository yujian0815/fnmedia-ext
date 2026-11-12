"""播放历史快照（孤儿历史重建）契约测试。

验证：record 时存入精简 brief 快照；store 缓存淘汰后，仍能凭快照重建
影片/剧集信息并恢复 ts 续播，不依赖易失的 store 缓存。全部离线、不联网。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import history as hmod  # noqa: E402


def _movie_brief():
    return {
        "guid": "movieguid", "title": "测试电影", "year": "2025",
        "poster": "https://img/p.jpg", "type_name": "电影",
        "locator": {},
        "lines": {"量子": [{"title": "正片", "url": "https://cdn/a.mp4",
                             "direct": True, "headers": {}}]},
    }


def _tv_brief(n=100):
    return {
        "guid": "tvguid", "title": "测试剧", "year": "2024",
        "poster": "https://img/t.jpg", "type_name": "剧集",
        "locator": {},
        "lines": {"L1": [{"title": f"第{i}集", "url": f"https://cdn/{i}.m3u8",
                          "direct": True, "headers": {}} for i in range(n)]},
    }


class SnapshotBuildTests(unittest.TestCase):
    def test_movie_snapshot_keeps_one_playable(self):
        snap = hmod.build_snapshot(_movie_brief())
        self.assertEqual(snap["guid"], "movieguid")
        self.assertEqual(len(snap["lines"]["量子"]), 1)
        self.assertEqual(snap["lines"]["量子"][0]["url"], "https://cdn/a.mp4")

    def test_tv_snapshot_keeps_only_target_episode(self):
        # 真实 ep guid 是 hash，无法反解 index；靠 ep 元数据定位集
        ep_guid = "abc123hash456"
        ep = {"ep_guid": ep_guid, "index": 5, "season": 1}
        snap = hmod.build_snapshot(_tv_brief(), ep_guid, ep=ep)
        # 只保留第 5 集这一条，而非全部 100 集
        self.assertEqual(len(snap["lines"]["L1"]), 1)
        self.assertEqual(snap["lines"]["L1"][0]["url"], "https://cdn/5.m3u8")
        self.assertEqual(snap["_ep_index"], 5)
        self.assertEqual(snap["_ep_guid"], ep_guid)
        # 体积远小于整剧
        import json
        self.assertLess(len(json.dumps(snap).encode()), 4096)

    def test_tv_snapshot_plaintext_guid_fallback(self):
        # 明文 ep: guid 仍可解析（兼容/测试形态）
        snap = hmod.build_snapshot(_tv_brief(), "ep:tvguid:3")
        self.assertEqual(snap["_ep_index"], 3)
        self.assertEqual(snap["lines"]["L1"][0]["url"], "https://cdn/3.m3u8")

    def test_invalid_brief_returns_none(self):
        self.assertIsNone(hmod.build_snapshot(None))
        self.assertIsNone(hmod.build_snapshot({}))


class HistorySnapshotRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = hmod._STATE
        hmod._STATE = Path(self._tmp.name) / "history.json"
        self.h = hmod.History()

    def tearDown(self):
        hmod._STATE = self._orig_state
        self._tmp.cleanup()

    def test_record_stores_and_reads_snapshot(self):
        snap = hmod.build_snapshot(_movie_brief())
        self.h.record({"item_guid": "movieguid", "ts": 500,
                       "duration": 0}, snapshot=snap)
        got = self.h.get_snapshot("movieguid")
        self.assertIsNotNone(got)
        self.assertEqual(got["guid"], "movieguid")
        rec = self.h.get("movieguid")
        self.assertEqual(rec["ts"], 500)

    def test_repeated_record_keeps_snapshot_without_recompute(self):
        snap = hmod.build_snapshot(_movie_brief())
        self.h.record({"item_guid": "movieguid", "ts": 10}, snapshot=snap)
        # 高频上报不带快照：已有快照应保留
        self.h.record({"item_guid": "movieguid", "ts": 20})
        self.assertIsNotNone(self.h.get_snapshot("movieguid"))
        self.assertEqual(self.h.get("movieguid")["ts"], 20)

    def test_persistence_across_reload(self):
        snap = hmod.build_snapshot(_tv_brief(), "ep:tvguid:5", ep={"ep_guid": "ep:tvguid:5", "index": 5})
        self.h.record({"item_guid": "ep:tvguid:5", "ts": 300}, snapshot=snap)
        self.h.flush()
        reloaded = hmod.History()
        got = reloaded.get_snapshot("ep:tvguid:5")
        self.assertIsNotNone(got)
        self.assertEqual(got["lines"]["L1"][0]["url"], "https://cdn/5.m3u8")

    def test_episode_index_parse(self):
        self.assertEqual(hmod._ep_index_from_guid("ep:tvguid:5"), 5)
        self.assertIsNone(hmod._ep_index_from_guid("movieguid"))
        self.assertIsNone(hmod._ep_index_from_guid("ep:tvguid:x"))



class LatestEpisodeRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = hmod._STATE
        hmod._STATE = Path(self._tmp.name) / "history.json"
        self.h = hmod.History()

    def tearDown(self):
        hmod._STATE = self._orig_state
        self._tmp.cleanup()

    def _tv_snap(self, ep_idx, ep_guid):
        return {"guid": "showguid", "title": "剧", "_ep_index": ep_idx,
                "_ep_guid": ep_guid, "lines": {"L": [{"url": "u"}]}}

    def test_find_latest_episode_by_online_guid(self):
        self.h.record({"item_guid": "ep1", "ts": 100},
                      snapshot=self._tv_snap(1, "ep1"))
        self.h.record({"item_guid": "ep5", "ts": 1243},
                      snapshot=self._tv_snap(4, "ep5"))
        # 影片级旧记录（无 ep_index）不应干扰
        self.h.record({"item_guid": "showguid", "ts": 980},
                      snapshot={"guid": "showguid", "lines": {}})
        got = self.h.latest_episode_record("showguid")
        self.assertIsNotNone(got)
        self.assertEqual(got["item_guid"], "ep5")
        self.assertEqual(got["ts"], 1243)

    def test_returns_none_when_no_episode(self):
        self.h.record({"item_guid": "movie", "ts": 50},
                      snapshot={"guid": "movie", "lines": {"L": [{"url": "u"}]}})
        self.assertIsNone(self.h.latest_episode_record("movie"))



class RecordStoreEvictionFallbackTests(unittest.TestCase):
    """store 缓存淘汰后，record 仍应更新已有 history（不依赖 store 命中）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = hmod._STATE
        hmod._STATE = Path(self._tmp.name) / "history.json"
        self.h = hmod.History()

    def tearDown(self):
        hmod._STATE = self._orig_state
        self._tmp.cleanup()

    def test_update_existing_record_without_store(self):
        snap = hmod.build_snapshot(_movie_brief())
        self.h.record({"item_guid": "movieguid", "ts": 100}, snapshot=snap)
        # store 已淘汰（模拟 get_title/get_episode 都返回 None），但 history 有记录
        self.assertTrue(self.h.get("movieguid") is not None)
        # 路由兜底判定：history.get 命中即可继续 record（真实逻辑在 app 层）
        self.assertTrue(self.h.get_snapshot("movieguid") is not None)
        # 新进度可写入，且保留快照
        self.h.record({"item_guid": "movieguid", "ts": 250},
                      snapshot=self.h.get_snapshot("movieguid"))
        self.assertEqual(self.h.get("movieguid")["ts"], 250)
        self.assertIsNotNone(self.h.get_snapshot("movieguid"))

    def test_unknown_guid_not_in_history(self):
        # 全新 guid 且 store 未命中：history 无记录，应走 forward（这里验证判空）
        self.assertIsNone(self.h.get("never-seen-guid"))
        self.assertIsNone(self.h.get_snapshot("never-seen-guid"))


if __name__ == "__main__":
    unittest.main()