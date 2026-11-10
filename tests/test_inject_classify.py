"""is_tv / fnos_type 分类边界：单集动画电影必须按 Movie 处理。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import inject


def _brief(type_name: str, ep_count: int) -> dict:
    return {
        "guid": "g", "title": "样本", "type_name": type_name,
        "poster": "https://img.example.com/cover.jpg",
        "lines": {
            "src::m3u8": [
                {"title": f"第{i + 1}集" if ep_count > 1 else "正片",
                 "url": f"https://x/{i}.m3u8", "direct": True}
                for i in range(ep_count)
            ]
        },
    }


class IsTvClassificationTests(unittest.TestCase):
    def test_single_episode_anime_is_movie(self):
        # 单集动画电影/剧场版/番外不得判成 TV Episode，否则播放历史只能竖幅
        for tn in ("动画片", "日韩动漫", "国产动漫", "番剧", "日本动漫"):
            with self.subTest(type_name=tn):
                self.assertFalse(inject.is_tv(_brief(tn, 1)))
                self.assertEqual(inject.fnos_type(_brief(tn, 1)), "Movie")

    def test_multi_episode_anime_is_tv(self):
        for tn in ("动画片", "日韩动漫", "番剧", "动画剧"):
            with self.subTest(type_name=tn):
                self.assertTrue(inject.is_tv(_brief(tn, 12)))

    def test_single_episode_variety_is_movie_multi_is_tv(self):
        self.assertFalse(inject.is_tv(_brief("港台综艺", 1)))
        self.assertTrue(inject.is_tv(_brief("港台综艺", 5)))

    def test_explicit_movie_words_always_movie(self):
        for tn in ("剧情片", "动画电影", "纪录片", "科幻片"):
            with self.subTest(type_name=tn):
                self.assertFalse(inject.is_tv(_brief(tn, 1)))

    def test_history_movie_card_uses_backdrop_and_movie_type(self):
        from urllib.parse import urlsplit, parse_qs
        brief = _brief("动画片", 1)
        card = inject.history_item(brief, {"ts": 100, "duration": 1000,
                                           "updated_at": 1.0})
        self.assertEqual(card["type"], "Movie")
        self.assertTrue(card["backdrops"])
        # backdrops 在普通海报 token 上带 crop 查询参数
        q = parse_qs(urlsplit(card["backdrops"]).query)
        self.assertEqual(q.get("crop"), ["16x9"])
        self.assertTrue(card["poster"])


class EpCountTests(unittest.TestCase):
    def test_ep_count_uses_max_per_line_not_sum(self):
        # 回归：单集电影有 6 条平行线路，旧实现把各线集数相加误算成 6 集，
        # 导致单集动画电影被判成连载 TV（Episode）、播放历史只能竖幅。
        single = _brief("动画片", 1)
        single["lines"] = {f"src{i}::m3u8": [
            {"title": "正片", "url": f"https://x/{i}.m3u8", "direct": True}
        ] for i in range(6)}
        self.assertEqual(inject._ep_count(single), 1)
        self.assertFalse(inject.is_tv(single))

        # 多集剧：各线路集数一致，取最大即真实集数
        multi = _brief("日韩动漫", 24)
        self.assertEqual(inject._ep_count(multi), 24)
        self.assertTrue(inject.is_tv(multi))

    def test_ep_count_empty_lines(self):
        self.assertEqual(inject._ep_count({"lines": {}}), 0)


if __name__ == "__main__":
    unittest.main()
