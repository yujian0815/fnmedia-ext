"""插件更新 stable/beta 双通道回归：版本比较与通道读取。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import updater


class VersionKeyTests(unittest.TestCase):
    def test_stable_newer_than_same_base_beta(self):
        # beta 切回正式通道时，同基础版本的正式版必须可被检测为更新
        self.assertTrue(updater.is_newer("1.1.0", "1.1.0-beta.5"))

    def test_beta_ordering(self):
        self.assertTrue(updater.is_newer("1.1.0-beta.6", "1.1.0-beta.5"))
        self.assertFalse(updater.is_newer("1.1.0-beta.5", "1.1.0-beta.6"))

    def test_higher_base_beta_newer_than_lower_stable(self):
        self.assertTrue(updater.is_newer("1.1.0-beta.5", "1.0.1"))

    def test_lower_stable_not_newer_than_beta(self):
        self.assertFalse(updater.is_newer("1.0.1", "1.1.0-beta.5"))

    def test_rc_between_beta_and_stable(self):
        self.assertTrue(updater.is_newer("1.1.0-rc.1", "1.1.0-beta.5"))
        self.assertTrue(updater.is_newer("1.1.0", "1.1.0-rc.1"))

    def test_v_prefix_tolerated(self):
        self.assertTrue(updater.is_newer("v1.2.0", "1.1.0"))


class ChannelTests(unittest.TestCase):
    def test_default_stable(self):
        with patch.object(updater._config, "config") as mc:
            mc.get.return_value = {}
            self.assertEqual(updater._get_channel(), "stable")

    def test_beta(self):
        with patch.object(updater._config, "config") as mc:
            mc.get.return_value = {"update_channel": "beta"}
            self.assertEqual(updater._get_channel(), "beta")

    def test_invalid_falls_back_stable(self):
        with patch.object(updater._config, "config") as mc:
            mc.get.return_value = {"update_channel": "xxx"}
            self.assertEqual(updater._get_channel(), "stable")


if __name__ == "__main__":
    unittest.main()
