"""Client response contracts; these are not native-device decoder tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import client


class ClientTests(unittest.TestCase):
    def test_tv_dates_are_calendar_valid_and_complete(self):
        tv = client.parse_user_agent("com.trim.tv")
        for value, expected in [
            (2024, "2024-01-01"), ("2024-2", "2024-02-01"),
            ("2024-02-29", "2024-02-29"),
            ("2024-02-29T12:30:00Z", "2024-02-29"),
            ("2023-02-29", ""), ("2024-04-31", ""), ("0000", ""),
            ("2024junk", ""), ("2024-02-29Tbad", ""), (None, ""),
        ]:
            with self.subTest(value=value):
                card = client.normalize_card({"guid": "test", "release_date": value}, tv)
                self.assertEqual(card["release_date"], expected)

    def test_tv_nested_detail_normalizes_numbers_nulls_and_status(self):
        original = {"status": "在线", "release_date": 2024,
                    "item": {"air_date": None, "status": "在线"},
                    "episodes": [{"air_date": "2023-02-29"}], "can_play": 1}
        result = client.normalize_detail(original, client.parse_user_agent("com.trim.tv"))
        self.assertEqual(result["status"], "1")
        self.assertEqual(result["release_date"], "2024-01-01")
        self.assertEqual(result["item"], {"air_date": "", "status": "1"})
        self.assertEqual(result["episodes"][0]["air_date"], "")
        self.assertEqual(result["can_play"], 1)
        self.assertEqual(original["release_date"], 2024)
        self.assertEqual(original["item"]["air_date"], None)

    def test_tv_strict_keeps_history_backdrop_fields(self):
        # 播放历史按横幅排版：backdrops/posters 必须在 TV 严格白名单中保留，
        # 否则历史卡只剩竖幅 poster，被客户端按竖幅渲染。
        tv = client.parse_user_agent("com.trim.tv")
        card = client.normalize_card({
            "guid": "g1", "type": "Movie", "status": "在线",
            "poster": "/online/p.jpg", "posters": "/online/p.jpg",
            "backdrops": "/online/p.jpg",
        }, tv)
        self.assertIn("backdrops", card)
        self.assertIn("posters", card)
        self.assertEqual(card["backdrops"], "/online/p.jpg")

    def test_tv_strict_keeps_playable_fields_for_line_cards(self):
        # 二级线路墙卡片必须保留 can_play/play_item_guid/play_error，否则 TV/盒子
        # 把卡片当不可播、整页“加载失败”（Web 非严格端原样返回，无此问题）。
        tv = client.parse_user_agent("com.trim.tv")
        row = {
            "guid": "fnmedia-line-item:abc", "type": "TV", "status": "在线",
            "can_play": 1, "play_item_guid": "fnmedia-line-item:abc",
            "play_error": "", "video_guid": "vg1", "poster": "/online/p.jpg",
        }
        card = client.normalize_card(row, tv)
        self.assertEqual(card["can_play"], 1)
        self.assertEqual(card["play_item_guid"], "fnmedia-line-item:abc")
        self.assertEqual(card["play_error"], "")
        self.assertEqual(card["status"], "1")

    def test_tv_strict_fills_play_defaults_when_missing(self):
        tv = client.parse_user_agent("com.trim.tv")
        card = client.normalize_card({"guid": "g1", "type": "Movie"}, tv)
        self.assertEqual(card["play_error"], "")
        self.assertIn("can_play", card)

    def test_nonstrict_responses_are_not_rewritten(self):
        row = {"release_date": "2024", "status": "在线", "can_play": 1}
        for ua in ("trimemedia-web", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios", "unknown-client"):
            with self.subTest(ua=ua):
                profile = client.parse_user_agent(ua)
                self.assertIs(client.normalize_card(row, profile), row)
                self.assertIs(client.normalize_detail(row, profile), row)


if __name__ == "__main__":
    unittest.main()
