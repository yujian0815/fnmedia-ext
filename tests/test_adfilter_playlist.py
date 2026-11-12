"""HLS 广告过滤的正确性、加密/初始化标签保留与失败回退。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter


class AdFilterPlaylistTests(unittest.TestCase):
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
