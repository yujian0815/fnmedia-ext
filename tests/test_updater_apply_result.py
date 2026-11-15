"""更新必须以独立脚本 apply-result 为准（issue #49）。"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import updater


class ApplyResultTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(updater._STATE)
        updater._STATE.update(status="idle", progress="", last_error="",
                              updated_version="", finished_at=0.0)
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.state_file = root / "state.json"
        self.result_file = root / "apply-result"
        self.patches = [patch.object(updater, "_STATE_FILE", self.state_file),
                        patch.object(updater, "_APPLY_RESULT_FILE", self.result_file)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        updater._STATE.clear()
        updater._STATE.update(self._old_state)
        self.td.cleanup()

    def test_install_waits_for_ok_result(self):
        payload = b"abc"
        digest = hashlib.sha256(payload).hexdigest()
        info = {"version": "9.9.9", "asset_name": "updater-test.fpk",
                "asset_size": len(payload), "asset": "https://dl.test/pkg",
                "sum_url": "https://dl.test/sum"}

        def _download(_url, path, _size):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        with patch.object(updater, "_download_with_fallbacks", side_effect=_download), \
                patch.object(updater, "_get_text_with_fallbacks", return_value=digest), \
                patch.object(updater, "_launch_transient_updater") as launch:
            updater._blocking_install(info, None)
        launch.assert_called_once()
        self.assertEqual(updater._STATE["status"], "installing")
        self.assertEqual(updater.status()["status"], "installing")

        self.result_file.write_text("OK 9.9.9")
        view = updater.status()
        self.assertEqual(view["status"], "done")
        self.assertEqual(view["updated_version"], "9.9.9")

    def test_fail_result_becomes_error_not_done(self):
        updater._STATE.update(status="installing", progress="安装中")
        self.result_file.write_text("FAIL service did not start")
        view = updater.status()
        self.assertEqual(view["status"], "error")
        self.assertIn("service did not start", view["last_error"])

    def test_restore_installing_without_result_stays_installing(self):
        self.state_file.write_text('{"status":"installing","progress":"安装中"}')
        updater._restore()
        self.assertEqual(updater._STATE["status"], "installing")

    def test_restore_download_interrupted_is_error(self):
        self.state_file.write_text('{"status":"downloading","progress":"下载中"}')
        updater._restore()
        self.assertEqual(updater._STATE["status"], "error")
        self.assertIn("重新发起更新", updater._STATE["progress"])

    def test_restore_ok_result_completes(self):
        self.state_file.write_text('{"status":"installing","progress":"安装中"}')
        self.result_file.write_text("OK 1.2.3")
        updater._restore()
        self.assertEqual(updater._STATE["status"], "done")
        self.assertEqual(updater._STATE["updated_version"], "1.2.3")


if __name__ == "__main__":
    unittest.main()
