"""订阅注册、自动刷新边界与根错误传播。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import subscriptions as subs
import tvbox


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(subs, "_STATE", Path(self.tmp.name) / "subscriptions.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_due_respects_manual_and_failure_pause(self):
        with patch.object(subs.time, "time", return_value=1_000_000):
            subs.register("https://a", refresh_days=1)
            subs.register("https://manual", refresh_days=0)
        self.assertEqual(len(subs.due(1_000_000 + 90000)), 1)
        subs.mark_failure("https://a", "x")
        subs.mark_failure("https://a", "y")
        subs.mark_failure("https://a", "z")
        self.assertEqual(subs.due(1_000_000 + 90000), [])
        subs.mark_success("https://a")
        self.assertEqual(subs.due(1_000_000 + 90000), [])

    async def test_refresh_bounded_and_failure_preserves_old_data(self):
        with patch.object(subs.time, "time", return_value=1_000_000):
            for i in range(7):
                subs.register(f"https://s{i}", refresh_days=1)
        active = 0
        peak = 0

        async def refresh(url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await __import__("asyncio").sleep(0.005)
            active -= 1
            if url.endswith("s3"):
                raise RuntimeError("upstream down")

        result = await subs.refresh_due(refresh, now=1_000_000 + 90000)
        self.assertEqual(result["total"], 5)  # 单轮最多 5 个
        self.assertEqual(result["ok"], 4)
        self.assertLessEqual(peak, 2)
        self.assertEqual(subs._load()["https://s3"]["fail_streak"], 1)
        self.assertEqual(subs._load()["https://s3"]["last_sync"], 1_000_000)

    async def test_invalid_root_subscription_is_not_silent_success(self):
        with self.assertRaises(ValueError):
            await tvbox.load_subscription_full(text="not-a-subscription")


if __name__ == "__main__":
    unittest.main()
