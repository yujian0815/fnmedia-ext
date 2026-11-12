"""LibreTV 风格批量源探活：并发、进度、取消与健康画像。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import source_probe as probe


def sources(n):
    return [{"id": f"s{i}", "name": f"源{i}", "api": "https://cms.test"}
            for i in range(n)]


class SourceProbeTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        probe.cancel()

    async def test_bounded_progress_and_health_writeback(self):
        active = 0
        peak = 0
        records = []

        async def fake_alive(source):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return {"alive": source["id"] != "s3", "ok": True,
                    "elapsed_ms": 12, "count": 2, "hit": "庆余年",
                    "tried": ["庆余年"], "status": 200, "error": ""}

        with patch.object(probe.providers, "probe_source_alive", side_effect=fake_alive), \
                patch.object(probe.sourcehealth, "record",
                             side_effect=lambda *args: records.append(args)), \
                patch.object(probe.sourcehealth, "is_dead", return_value=False):
            await probe.start(sources(12), concurrency=4)
            result = await probe.wait()
        self.assertEqual(result["total"], 12)
        self.assertEqual(result["done"], 12)
        self.assertEqual(result["alive_count"], 11)
        self.assertFalse(result["running"])
        self.assertLessEqual(peak, 4)
        self.assertEqual(len(records), 12)
        self.assertEqual(len(result["results"]), 12)

    async def test_cancel_keeps_completed_results(self):
        started = asyncio.Event()

        async def slow_alive(source):
            started.set()
            await asyncio.sleep(0.05)
            return {"alive": True, "ok": True, "elapsed_ms": 50,
                    "count": 1, "hit": "庆余年", "tried": ["庆余年"],
                    "status": 200, "error": ""}

        with patch.object(probe.providers, "probe_source_alive", side_effect=slow_alive), \
                patch.object(probe.sourcehealth, "record", return_value=None), \
                patch.object(probe.sourcehealth, "is_dead", return_value=False):
            await probe.start(sources(20), concurrency=4)
            await started.wait()
            probe.cancel()
            result = await probe.wait()
        self.assertFalse(result["running"])
        self.assertLess(result["done"], result["total"])
        self.assertEqual(len(result["results"]), result["done"])


if __name__ == "__main__":
    unittest.main()
