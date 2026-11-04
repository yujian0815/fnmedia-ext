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
