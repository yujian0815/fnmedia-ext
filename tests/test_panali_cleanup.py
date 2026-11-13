"""阿里网盘转存缓存账本与自动清理回归（mock 隔离真实网盘 API）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import panali


class PanaliCacheLedgerTests(unittest.TestCase):
    def setUp(self):
        self._st = {}
        self._p_load = patch.object(panali, "_load", side_effect=lambda: self._st)
        self._p_save = patch.object(panali, "_save", side_effect=lambda st: self._st.update(st))
        self._p_load.start(); self._p_save.start()
        self.addCleanup(self._p_load.stop)
        self.addCleanup(self._p_save.stop)

    def test_register_and_summary(self):
        panali.cache_register(["f1", "f2"],
                              {"f1": {"name": "a.mp4", "size": 100},
                               "f2": {"name": "b.mkv", "size": 200}})
        s = panali.cache_summary()
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["size"], 300)

    def test_register_keeps_existing_ts(self):
        panali.cache_register(["f1"])
        old = self._st["cache"]["f1"]["ts"]
        self._st["cache"]["f1"]["ts"] = old - 9999
        panali.cache_register(["f1"])  # 重复转存不重置 TTL
        self.assertEqual(self._st["cache"]["f1"]["ts"], old - 9999)

    def test_touch_refreshes(self):
        panali.cache_register(["f1"])
        self._st["cache"]["f1"]["ts"] = time.time() - 9999
        panali.cache_touch("f1")
        self.assertGreater(self._st["cache"]["f1"]["ts"], time.time() - 60)


class PanaliCleanupTests(unittest.TestCase):
    def setUp(self):
        self._st = {}
        self._p_load = patch.object(panali, "_load", side_effect=lambda: self._st)
        self._p_save = patch.object(panali, "_save", side_effect=lambda st: self._st.update(st))
        self._p_load.start(); self._p_save.start()
        self.addCleanup(self._p_load.stop)
        self.addCleanup(self._p_save.stop)

    def test_cleanup_only_expired_ledger_files(self):
        now = time.time()
        self._st["cache"] = {
            "expired": {"ts": now - 7 * 3600, "name": "old.mp4", "size": 100},
            "fresh": {"ts": now - 3600, "name": "new.mp4", "size": 200},
        }
        deleted = []
        present = {"expired": {"file_id": "expired", "type": "file", "size": 100},
                   "fresh": {"file_id": "fresh", "type": "file", "size": 200},
                   "user_own": {"file_id": "user_own", "type": "file", "size": 999}}

        def fake_post(cli, path, body, **kw):
            if path == "/adrive/v3/file/list":
                return {"items": list(present.values()), "next_marker": ""}
            if path == "/v2/file/delete":
                deleted.append(body["file_id"])
                return {"code": 0, "status": 204}
            return {}

        with patch.object(panali, "_is_web_mode", return_value=True), \
                patch.object(panali, "_web_client") as wc, \
                patch.object(panali, "_web_ensure_save_dir", return_value=("d", "p")), \
                patch.object(panali, "_web_post", side_effect=fake_post):
            wc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            wc.return_value.__exit__ = MagicMock(return_value=False)
            res = panali.cleanup_expired(ttl_seconds=6 * 3600)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(deleted, ["expired"])          # 只删过期且登记的
        self.assertNotIn("user_own", deleted)           # 不碰用户自己的文件
        self.assertIn("fresh", self._st["cache"])       # 未过期的保留
        self.assertNotIn("expired", self._st["cache"])

    def test_cleanup_not_web_mode_skips(self):
        self._st["cache"] = {"x": {"ts": 0}}
        with patch.object(panali, "_is_web_mode", return_value=False):
            res = panali.cleanup_expired()
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(res.get("reason"), "not_web_mode")


if __name__ == "__main__":
    unittest.main()
