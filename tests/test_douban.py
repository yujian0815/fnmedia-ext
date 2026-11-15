"""豆瓣主源 + TMDB 横幅兜底双源回归（不打真实 API）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import douban
import tmdb


class DoubanMatchingTests(unittest.TestCase):
    def test_suggest_pick_title_and_year(self):
        rows = [{"id": "1", "title": "奥德赛", "year": "1997"},
                {"id": "2", "title": "奥德赛", "year": "2026"}]
        self.assertEqual(douban._pick_suggest(rows, "奥德赛", "2026")["id"], "2")

    def test_suggest_reject_unrelated(self):
        rows = [{"id": "9", "title": "完全不同的片", "year": "1999"}]
        self.assertIsNone(douban._pick_suggest(rows, "奥德赛", "2026"))

    def test_chart_id_used_directly(self):
        brief = {"guid": "cg", "chart": {"id": "36808876", "cat": "movie"}}
        self.assertEqual(douban._douban_id(brief), "36808876")
        self.assertEqual(douban._cache_key(brief), "id:36808876")

    def test_old_chart_binding_recovers_id(self):
        with patch("charts.chart_cache.get_guid", return_value={"id": "36808876"}):
            self.assertEqual(douban._douban_id({"guid": "cg"}), "36808876")


class DoubanNormalizeTests(unittest.TestCase):
    def test_actor_and_landscape_mapping(self):
        detail = {"id": "1", "title": "奥德赛", "original_title": "The Odyssey",
                  "intro": "简介", "rating": {"value": 8.6},
                  "genres": ["冒险"], "durations": ["173分钟"],
                  "pubdate": ["2026-08-14(中国大陆)"],
                  "directors": [{"name": "克里斯托弗·诺兰"}],
                  "pic": {"large": "poster.jpg"}}
        celebrities = {"actors": [
            {"id": "a1", "name": "马特·达蒙", "latin_name": "Matt Damon",
             "character": "饰 奥德修斯 Odysseus",
             "avatar": {"large": "actor.jpg"}}]}
        photos = {"photos": [
            {"image": {"large": {"width": 500, "height": 900, "url": "portrait.jpg"}}},
            {"image": {"large": {"width": 1600, "height": 900, "url": "wide.jpg"}}},
        ]}
        m = douban._normalize_detail(detail, celebrities, photos, "1")
        self.assertEqual(m["cast"][0]["source"], "douban")
        self.assertEqual(m["cast"][0]["character"], "奥德修斯 Odysseus")
        self.assertEqual(m["cast"][0]["profile"], "actor.jpg")
        self.assertEqual(m["backdrop"], "wide.jpg")
        self.assertEqual(m["runtime"], 173)
        self.assertEqual(m["release_date"], "2026-08-14")


class MergeTests(unittest.TestCase):
    def test_douban_primary_tmdb_backdrop_wins(self):
        db = {"douban_id": "d1", "overview": "豆瓣简介",
              "cast": [{"name": "豆瓣演员"}], "backdrop": "豆瓣横图",
              "poster": "豆瓣海报"}
        tm = {"tmdb_id": 1, "overview": "TMDB简介",
              "cast": [{"name": "TMDB演员"}], "backdrop": "TMDB横图",
              "runtime": 120}
        out = tmdb._merge_metadata(db, tm)
        self.assertEqual(out["overview"], "豆瓣简介")
        self.assertEqual(out["cast"], db["cast"])
        self.assertEqual(out["backdrop"], "TMDB横图")
        self.assertEqual(out["poster"], "豆瓣海报")
        self.assertEqual(out["runtime"], 120)
        self.assertEqual(out["tmdb_id"], 1)

    def test_either_source_alone_works(self):
        self.assertEqual(tmdb._merge_metadata({"douban_id": "d"}, None),
                         {"douban_id": "d"})
        self.assertEqual(tmdb._merge_metadata(None, {"tmdb_id": 1}),
                         {"tmdb_id": 1})

    def test_douban_can_work_without_tmdb_key(self):
        with patch.object(tmdb, "_key", return_value=""), \
                patch.object(douban, "enabled", return_value=True), \
                patch("config.config") as cfg:
            cfg.get.return_value = {"tmdb_enabled": True}
            self.assertTrue(tmdb.enabled())


if __name__ == "__main__":
    unittest.main()
