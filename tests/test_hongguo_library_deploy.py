"""红果部署完成必须同时满足独立媒体库真实可取内容。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


class HongguoLibraryDeployTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_window_with_rows_is_ready(self):
        with patch.object(appmod.catalog, "standalone_available", return_value=True), \
                patch.object(appmod.catalog, "standalone_window",
                             AsyncMock(return_value=([{"guid": "one"}], 1))) as window:
            ok = await appmod._wait_for_hongguo_library("custom_hg", timeout=1)
        self.assertTrue(ok)
        window.assert_awaited_once_with("custom_hg", 0, 1)

    async def test_refresh_is_required_before_library_ready(self):
        refresh = AsyncMock()
        with patch.object(appmod.catalog, "standalone_available",
                          side_effect=[False, True]), \
                patch.object(appmod.catalog, "refresh_routes", refresh), \
                patch.object(appmod.catalog, "standalone_window",
                             AsyncMock(return_value=([{"guid": "one"}], 1))):
            ok = await appmod._wait_for_hongguo_library("custom_hg", timeout=1)
        self.assertTrue(ok)
        refresh.assert_awaited_once()

    async def test_empty_library_returns_false_instead_of_ready(self):
        with patch.object(appmod.catalog, "standalone_available", return_value=False), \
                patch.object(appmod.time, "monotonic", side_effect=[0.0, 200.0]):
            ok = await appmod._wait_for_hongguo_library("custom_hg", timeout=1)
        self.assertFalse(ok)

    async def test_deploy_callback_waits_for_real_library(self):
        with patch.object(appmod, "_ensure_hongguo_source",
                          return_value={"id": "custom_hg", "api": "test"}), \
                patch.object(appmod, "_wait_for_hongguo_library",
                             AsyncMock(return_value=True)) as wait:
            got = await asyncio.to_thread(
                appmod._ensure_hongguo_source_and_library, asyncio.get_running_loop())
        self.assertEqual(got["id"], "custom_hg")
        wait.assert_awaited_once_with("custom_hg")

    async def test_deploy_callback_rejects_empty_library(self):
        with patch.object(appmod, "_ensure_hongguo_source",
                          return_value={"id": "custom_hg", "api": "test"}), \
                patch.object(appmod, "_wait_for_hongguo_library",
                             AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "独立库暂无可取内容"):
                await asyncio.to_thread(
                    appmod._ensure_hongguo_source_and_library, asyncio.get_running_loop())


if __name__ == "__main__":
    unittest.main(verbosity=2)
