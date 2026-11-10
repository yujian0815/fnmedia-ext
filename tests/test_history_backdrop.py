"""播放历史卡片按横幅（backdrop）模型展示的契约。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import inject

POSTER_URL = "https://img.example.com/cover.jpg"


class HistoryBackdropTests(unittest.TestCase):
    def _brief(self, **over):
        b = {
            "guid": "g1", "title": "测试剧", "year": "2025",
            "poster": POSTER_URL,
            "type_name": "国产剧", "score": "8.0", "lines": {},
        }
        b.update(over)
        return b

    def test_movie_history_backdrop_uses_crop_query_on_plain_token(self):
        brief = self._brief(type_name="剧情片")
        card = inject.history_item(brief, {"ts": 100, "duration": 1000,
                                           "updated_at": 1.0})
        self.assertEqual(card["type"], "Movie")
        # 三个图片字段统一为 16:9 裁切横图（不同端取图字段不一致）
        for key in ("poster", "posters", "backdrops"):
            q = parse_qs(urlsplit(card[key]).query)
            self.assertEqual(q.get("crop"), ["16x9"], key)
        self.assertEqual(card["poster"], card["backdrops"])
        # 基础路径仍是普通 base64 图片 token（不带 JSON payload）
        base = card["backdrops"].split("?", 1)[0]
        self.assertNotIn("{", base)

    def test_episode_history_backdrop_uses_crop_query(self):
        brief = self._brief()
        ep = {"ep_guid": "ep1", "online_guid": "g1", "index": 2,
              "season": 1, "episode_number": 3, "ep_title": "第3集"}
        card = inject.history_item(brief, {"ts": 0, "duration": 0,
                                           "updated_at": 1.0}, ep)
        self.assertEqual(card["type"], "Episode")
        for key in ("poster", "posters", "backdrops"):
            q = parse_qs(urlsplit(card[key]).query)
            self.assertEqual(q.get("crop"), ["16x9"], key)
        self.assertTrue(card["backdrops"].startswith("/online/"))


if __name__ == "__main__":
    unittest.main()
