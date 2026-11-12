"""1.1 统一安装包与旧 full/lite 客户端的更新兼容性。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import updater  # noqa: E402


def asset(name: str) -> dict:
    return {"name": name, "browser_download_url": "https://dl.test/" + name,
            "size": 123}


class UnifiedAssetTests(unittest.TestCase):
    def test_unified_plain_package_serves_former_full_and_lite_clients(self):
        assets = [asset("fnmedia-ext-1.1.0.fpk"),
                  asset("fnmedia-ext-1.1.0.fpk.sha256")]
        for legacy_full in (True, False):
            with self.subTest(legacy_full=legacy_full):
                picked = updater._pick_asset(assets, legacy_full)
                self.assertEqual(picked["name"], "fnmedia-ext-1.1.0.fpk")

    def test_legacy_full_alias_is_only_a_compatibility_bridge(self):
        assets = [asset("fnmedia-ext-1.1.0.fpk"),
                  asset("fnmedia-ext-1.1.0-full.fpk")]
        self.assertEqual(updater._pick_asset(assets, True)["name"],
                         "fnmedia-ext-1.1.0-full.fpk")
        self.assertEqual(updater._pick_asset(assets, False)["name"],
                         "fnmedia-ext-1.1.0.fpk")

    def test_old_dual_release_keeps_legacy_selection(self):
        assets = [asset("fnmedia-ext-1.0.0.fpk"),
                  asset("fnmedia-ext-1.0.0-lite.fpk"),
                  asset("fnmedia-ext-1.0.0-full.fpk")]
        self.assertEqual(updater._pick_asset(assets, True)["name"],
                         "fnmedia-ext-1.0.0-full.fpk")
        self.assertEqual(updater._pick_asset(assets, False)["name"],
                         "fnmedia-ext-1.0.0.fpk")

    def test_old_full_never_falls_back_to_lite_only_asset(self):
        assets = [asset("fnmedia-ext-1.0.0-lite.fpk"),
                  asset("fnmedia-ext-1.0.0-public.fpk")]
        self.assertIsNone(updater._pick_asset(assets, True))
        self.assertEqual(updater._pick_asset(assets, False)["name"],
                         "fnmedia-ext-1.0.0-lite.fpk")

    def test_normalized_release_is_standard_and_finds_matching_checksum(self):
        assets = [asset("fnmedia-ext-1.1.0.fpk"),
                  asset("fnmedia-ext-1.1.0.fpk.sha256")]
        info = updater._normalize({
            "tag_name": "v1.1.0", "name": "v1.1.0", "body": "统一版",
            "assets": assets}, True)
        self.assertEqual(info["edition"], "standard")
        self.assertEqual(info["asset_name"], "fnmedia-ext-1.1.0.fpk")
        self.assertTrue(info["sum_url"].endswith(".fpk.sha256"))


class ReleaseProcessTests(unittest.TestCase):
    def test_publish_builds_before_push_and_publishes_after_upload(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "tools" / "publish-public.sh").read_text()
        self.assertLess(script.index("build_unified\nfi"), script.index("git fetch origin"))
        self.assertIn("--draft \\", script)
        self.assertIn('--draft=false', script)
        self.assertLess(script.index("gh release upload"), script.index('--draft=false'))
        self.assertNotIn("--exclude='tools/'", script)
        self.assertIn('"$LEGACY_FULL" "${LEGACY_FULL}.sha256"', script)
        self.assertNotIn('"$LITE_BASE"', script)
        self.assertNotIn('"$LITE_FPK"', script)
        workflow = (root / ".github" / "workflows" / "moo-json.yml").read_text()
        self.assertIn('git pull --rebase --autostash origin main', workflow)
        self.assertIn('gh release list --limit 1', workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
