"""Native season/episode identity checks using observed AT label shapes."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import inject
import store as storemod
import t4


SOURCE = {"id": "series", "name": "Fixture", "api": "https://t4.test/api"}


class EpisodeMappingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        item = patch.object(storemod, "_STATE", Path(temp.name) / "titles.json")
        item.start()
        self.addCleanup(item.stop)
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        item = patch.object(storemod, "store", self.store)
        item.start()
        self.addCleanup(item.stop)

    def remember(self, flags, playlist):
        title = t4.map_title({
            "vod_id": "series-1", "vod_name": "Series", "type_name": "电视剧",
            "vod_play_from": flags, "vod_play_url": playlist,
        }, SOURCE, detail=True)
        guid = storemod.make_guid(title)
        if self.store.get_title_by_guid(guid):
            self.store.replace_t4_detail(guid, title)
        else:
            self.store.remember_merged([inject.brief_of(title)])
        return guid

    def test_observed_cloud_labels_share_explicit_episode_identity(self):
        cases = [
            "第01集", "第 1 集", "01", "01(1.77 GB)", "01.mkv(1.23 GB)",
            "01. 第1集(5.4 GB)", "S01E01(4.6 GB)", "S01E01.第1集(4.92 GB)",
            "S01E01.2026.2160p.60fps.WEB-DL.HDR.H.265.10bit.AAC.mkv(5.17 GB)",
            "Where the Mask Ends.2026.S01E01.2160p.WEB-DL.HEVC.mkv(5.4 GB)",
            "第1集 · 4K", "EP01", "01.2024.2160p.IQ.WEB-DL.H265(1.17 GB)",
            "幕后之王.S01E01.mkv",
        ]
        for label in cases:
            with self.subTest(label=label):
                self.assertEqual(t4.episode_key(label), "episode:1")
        self.assertEqual(t4.episode_key("01(2 GB)", "Season 2"),
                         "season:2:episode:1")
        self.assertEqual(t4.episode_key("第1集", "第2季"), "season:2:episode:1")
        self.assertEqual(t4.episode_key("第2季第1集"), "season:2:episode:1")
        self.assertEqual(t4.episode_key("S02E01.第1集"), "season:2:episode:1")

    def test_controls_are_not_episodes_and_real_play_ids_are_untouched(self):
        for label in ("📄 媒体信息", "🔍 全局搜索", "➕ 稍后再看", "➕ 追剧·第1季",
                      "互动状态", "点赞", "投币", "收藏", "订阅信息", "检查更新",
                      "立即巡检", "取消追剧", "选集播放"):
            with self.subTest(label=label):
                self.assertEqual(t4.episode_key(label), "")
        self.assertEqual(t4.episode_key("Anything", play_id="msubcheck-7"), "")
        self.assertEqual(t4.episode_key("第1集", play_id="msubep-7-1"), "episode:1")
        related = t4.episode_key("EP01", "BiliBili · 相关视频")
        self.assertEqual(t4.episode_season(related), 0)
        self.assertEqual(t4.episode_number(related), 0)
        guid = self.remember("A", "第1集$opaque-1#选集播放$opaque-action#点赞$bililike-1")
        guids = self.store.tv_episode_guids(guid)
        self.assertEqual(len(guids), 1)
        self.assertEqual(self.store.get_episode_target(guids[0])["url"], "opaque-1")

    def test_uncertain_ranges_extras_and_technical_numbers_are_not_guessed(self):
        for label in ("S01E01-E02", "S01E01E02", "01-02", "S01E01 S02E01",
                      "S01E01.第2集", "第1集第2集",
                      "S01E01预告", "第1集花絮", "2024.2160p", "1080p", "全12集"):
            with self.subTest(label=label):
                self.assertTrue(t4.episode_key(label).startswith("label:"))

    def test_missing_episodes_and_mixed_seasons_do_not_align_by_position(self):
        guid = self.remember("A$$$B$$$Season 2", (
            "S01E03(1 GB)$a3#S01E01(2 GB)$a1#S01E02(3 GB)$a2$$$"
            "第3集$b3#第1集$b1$$$02(2 GB)$s2e2#01(1 GB)$s2e1"))
        seasons = inject.season_list(self.store.get_title_by_guid(guid))
        self.assertEqual([s["season_number"] for s in seasons], [1, 2])
        self.assertEqual([s["number_of_episodes"] for s in seasons], [3, 2])
        eps = inject.episode_list(self.store.get_title_by_guid(guid), 1)
        self.assertEqual([e["episode_number"] for e in eps], [1, 2, 3])
        self.assertIsNone(self.store.get_episode_target(eps[1]["guid"], "B"))
        self.assertEqual(self.store.get_episode_target(eps[2]["guid"], "B")["url"], "b3")
        s2 = inject.episode_list(self.store.get_title_by_guid(guid), 2)
        self.assertEqual(self.store.get_episode_target(s2[0]["guid"])["url"], "s2e1")
        self.assertNotEqual(eps[0]["guid"], s2[0]["guid"])
        self.assertNotEqual(eps[0]["media_guid"], s2[0]["media_guid"])
        self.assertEqual(s2[0]["parent_guid"], seasons[1]["guid"])

    def test_supplemental_content_is_separate_from_regular_next_episode(self):
        guid = self.remember("A", "第1集$a1#第3集$a3#S01E01预告$extra1#彩蛋1.mp4$extra2")
        brief = self.store.get_title_by_guid(guid)
        seasons = inject.season_list(brief)
        self.assertEqual([s["season_number"] for s in seasons], [1, 0])
        self.assertEqual([e["episode_number"] for e in inject.episode_list(brief, 1)], [1, 3])
        self.assertEqual(len(inject.episode_list(brief, 0)), 2)
        self.assertEqual(seasons[1]["title"], "特别内容")
        self.assertEqual(inject.detail_item(brief)["number_of_seasons"], 2)
        regular = self.store.tv_episode_guids(guid, regular_only=True)
        self.assertEqual([self.store.get_episode(eg)["episode_number"] for eg in regular], [1, 3])

    def test_related_videos_remain_selectable_but_never_replace_the_regular_episode(self):
        guid = self.remember("A$$$BiliBili · 相关视频",
                             "第1集$main1#第2集$main2$$$第1集$related1#EP02$related2")
        regular = self.store.tv_episode_guids(guid, regular_only=True)
        self.assertEqual(len(regular), 2)
        self.assertEqual(self.store.episode_lines(regular[0]), ["A"])
        extras = inject.episode_list(self.store.get_title_by_guid(guid), 0)
        self.assertEqual(len(extras), 2)
        self.assertEqual({self.store.get_episode_target(e["guid"])["url"] for e in extras},
                         {"related1", "related2"})

    def test_label_size_refresh_and_restart_preserve_episode_guids(self):
        guid = self.remember("A", "S01E02(1 GB)$old2#S01E01(2 GB)$old1")
        guids = self.store.tv_episode_guids(guid)
        self.remember("A", "01.mkv(3 GB)$new1#02.mkv(4 GB)$new2#03.mkv(5 GB)$new3")
        self.assertEqual(self.store.tv_episode_guids(guid)[:2], guids)
        self.store.flush()
        restored = storemod.Store()
        self.addCleanup(restored.close)
        self.assertEqual(restored.tv_episode_guids(guid)[:2], guids)
        self.assertEqual(restored.get_episode_target(guids[1])["url"], "new2")

    def test_duplicate_episode_labels_are_not_matched_by_array_position(self):
        guid = self.remember("A$$$B",
                             "S01E01(1 GB)$first#S01E01(2 GB)$second$$$第1集$unique")
        eps = self.store.tv_episode_guids(guid, regular_only=True)
        self.assertEqual(len(eps), 1)
        self.assertEqual(self.store.episode_lines(eps[0]), ["B"])
        self.assertEqual(inject._ep_count(self.store.get_title_by_guid(guid)), 3)
        variants = inject.episode_list(self.store.get_title_by_guid(guid), 0)
        self.assertEqual(len(variants), 2)
        self.assertEqual({self.store.get_episode_target(e["guid"])["url"] for e in variants},
                         {"first", "second"})

    def test_native_episode_menu_uses_cached_index_without_reparsing_every_file(self):
        guid = self.remember("A", "#".join(f"S01E{n:04}(1 GB)$id{n}"
                                         for n in range(1, 1001)))
        brief = self.store.get_title_by_guid(guid)
        with patch("t4.episode_key", side_effect=AssertionError("no file rescan")):
            self.assertEqual(inject._ep_count(brief), 1000)
            self.assertEqual(len(inject.episode_list(brief, 1)), 1000)
            self.assertEqual(inject.season_list(brief)[0]["number_of_episodes"], 1000)


if __name__ == "__main__":
    unittest.main()
