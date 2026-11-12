"""HLS 广告过滤的正确性、加密/初始化标签保留与失败回退。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter


class AdFilterPlaylistTests(unittest.TestCase):
    def test_lead_median_guard_keeps_routine_packaging(self):
        # 思路对齐 LibreTV 中位数保护：片头组与后续各组分片规模接近时，
        # 属于采集站常规 DISCONTINUITY 封装，不应被当广告删除。
        self.assertTrue(adfilter._lead_group_looks_routine(
            10, [10, 11, 9, 10, 12]))
        # 片头组明显更短（典型广告插段）则不是常规封装
        self.assertFalse(adfilter._lead_group_looks_routine(
            2, [20, 21, 19, 20]))
        # 空/零值安全
        self.assertFalse(adfilter._lead_group_looks_routine(0, [10]))
        self.assertFalse(adfilter._lead_group_looks_routine(5, []))

    def test_routine_lead_group_not_dropped_by_heuristic(self):
        # 常规封装：采集站把正片按固定块切分，每个衔接处都打 DISCONTINUITY。
        # 片头组与其它非主块规模接近（中位数保护成立），不应被当广告删除；
        # 正片主块（最大块）仍被正确识别。
        lines = ["#EXTM3U", "#EXT-X-DISCONTINUITY"]
        for i in range(10):  # 片头组 10 片
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://lead.test/seg{i}.ts")
        lines.append("#EXT-X-DISCONTINUITY")
        for i in range(40):  # 正片主块 40 片（最大）
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://cdn.test/main/{i}.ts")
        lines.append("#EXT-X-DISCONTINUITY")
        for i in range(10):  # 非主块，规模与片头组一致
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://tail.test/seg{i}.ts")
        lines.append("#EXT-X-DISCONTINUITY")
        for i in range(10):  # 再一个同规模非主块，使中位数=10
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://tail2.test/seg{i}.ts")
        text = "\n".join(lines) + "\n"
        out, removed = adfilter._detect_ad_blocks(text)
        # 中位数保护只针对片头组：片头与非主块规模中位数接近时保留片头，
        # 正片主块始终保留。正片之后的块不属片头，不纳入此保护语义。
        self.assertIn("lead.test/seg0.ts", out)
        self.assertIn("cdn.test/main/0.ts", out)

    def test_short_lead_ad_before_long_main_is_removed(self):
        # 真广告：短小片头组（10 片）远小于正片主块（40 片），中位数保护不成立，
        # 应作为广告删除，正片保留。
        lines = ["#EXTM3U", "#EXT-X-DISCONTINUITY"]
        for i in range(10):
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://lead.test/seg{i}.ts")
        lines.append("#EXT-X-DISCONTINUITY")
        for i in range(40):
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://cdn.test/main/{i}.ts")
        text = "\n".join(lines) + "\n"
        out, removed = adfilter._detect_ad_blocks(text)
        self.assertGreater(removed, 0)
        self.assertNotIn("lead.test/seg0.ts", out)
        self.assertIn("cdn.test/main/0.ts", out)

    def test_url_marked_lead_removed_by_full_filter(self):
        # URL 强广告信号在完整 filter_hls 链路里删除，不受中位数防误判影响。
        lines = ["#EXTM3U", "#EXT-X-DISCONTINUITY"]
        for i in range(10):
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://ads.test/ad/{i}.ts")
        lines.append("#EXT-X-DISCONTINUITY")
        for i in range(40):
            lines.append("#EXTINF:6.0,")
            lines.append(f"https://cdn.test/main/{i}.ts")
        text = "\n".join(lines) + "\n"
        out, removed = adfilter.filter_hls(text, "https://cdn.test/main.m3u8")
        self.assertGreater(removed, 0)
        self.assertNotIn("/ad/", out)
        self.assertIn("main/0.ts", out)

    def test_scte_group_removed_but_key_map_and_content_survive(self):
        text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.test/key.bin"
#EXT-X-MAP:URI="https://cdn.test/init.mp4"
#EXT-X-CUE-OUT:DURATION=30
#EXTINF:15.0,
https://cdn.test/ad/15.ts
#EXTINF:15.0,
https://cdn.test/ad/30.ts
#EXT-X-CUE-IN
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
https://cdn.test/main/1.ts
#EXTINF:6.0,
https://cdn.test/main/2.ts
"""
        out, removed = adfilter.filter_hls(text, "https://cdn.test/main.m3u8")
        self.assertEqual(removed, 2)
        self.assertIn("#EXT-X-KEY", out)
        self.assertIn("#EXT-X-MAP", out)
        self.assertIn("main/1.ts", out)
        self.assertIn("main/2.ts", out)
        self.assertNotIn("/ad/15.ts", out)
        self.assertNotIn("/ad/30.ts", out)

    def test_mixed_group_does_not_drop_whole_content(self):
        text = """#EXTM3U
#EXTINF:5.0,
https://cdn.test/ad/a.ts
#EXTINF:6.0,
https://cdn.test/main/1.ts
#EXTINF:6.0,
https://cdn.test/main/2.ts
"""
        out, _ = adfilter.filter_hls(text, "https://cdn.test/main.m3u8")
        self.assertIn("main/1.ts", out)
        self.assertIn("main/2.ts", out)

    def test_empty_result_restores_original_playlist(self):
        text = """#EXTM3U
#EXT-X-CUE-OUT:DURATION=15
#EXTINF:15.0,
https://cdn.test/ad/only.ts
#EXT-X-CUE-IN
"""
        out, removed = adfilter.filter_hls(text, "https://cdn.test/main.m3u8")
        self.assertEqual(out, text)
        self.assertEqual(removed, 0)

    def test_custom_middle_skip_is_opt_in_and_preserves_key_map(self):
        text = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.test/key.bin"
#EXT-X-MAP:URI="https://cdn.test/init.mp4"
#EXTINF:6.0,
https://cdn.test/main/1.ts
#EXTINF:6.0,
https://cdn.test/main/2.ts
#EXTINF:6.0,
https://cdn.test/main/3.ts
"""
        out, removed = adfilter.filter_hls(
            text, "https://cdn.test/main.m3u8", skip_ranges=[(6, 12)])
        self.assertEqual(removed, 1)
        self.assertIn("main/1.ts", out)
        self.assertNotIn("main/2.ts", out)
        self.assertIn("main/3.ts", out)
        self.assertIn("#EXT-X-KEY", out)
        self.assertIn("#EXT-X-MAP", out)
        # 未传 ranges 时完全按原清单处理，不影响基线。
        original, _ = adfilter.filter_hls(text, "https://cdn.test/main.m3u8")
        self.assertIn("main/2.ts", original)

    def test_custom_middle_empty_result_restores_original(self):
        text = "#EXTM3U\n#EXTINF:6.0,\nhttps://cdn.test/main/only.ts\n"
        out, removed = adfilter.filter_hls(
            text, "https://cdn.test/main.m3u8", skip_ranges=[(0, 6)])
        self.assertEqual(out, text)
        self.assertEqual(removed, 0)

    def test_master_playlist_is_not_filtered(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nmain.m3u8\n"
        out, removed = adfilter.filter_hls(text, "https://cdn.test/master.m3u8")
        self.assertEqual(out, text)
        self.assertEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()
