"""#EXT-X-START 注入：让原生 HLS 播放器（App/TV）从续播点起播。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def _vod_playlist() -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:8",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(5):
        lines.append("#EXTINF:6.0,")
        lines.append(f"/v/media/online/mg/seg?i={i}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class HlsStartTests(unittest.TestCase):
    def setUp(self):
        self.picked = {"ep_guid": "ep123"}

    def test_injects_start_for_vod_with_resume_ts(self):
        with patch.object(appmod.history, "get",
                          return_value={"ts": 1089}):
            out = appmod._inject_hls_start(_vod_playlist(), self.picked)
        self.assertIn("#EXT-X-START:TIME-OFFSET=1089,PRECISE=YES", out)
        # 标签紧跟 #EXTM3U
        self.assertTrue(out.startswith(
            "#EXTM3U\n#EXT-X-START:TIME-OFFSET=1089,PRECISE=YES"))
        # 分片与 ENDLIST 完整保留
        self.assertIn("#EXT-X-ENDLIST", out)
        self.assertEqual(out.count("seg?i="), 5)

    def test_no_start_when_ts_zero_or_missing(self):
        body = _vod_playlist()
        with patch.object(appmod.history, "get", return_value={"ts": 0}):
            self.assertEqual(appmod._inject_hls_start(body, self.picked), body)
        with patch.object(appmod.history, "get", return_value=None):
            self.assertEqual(appmod._inject_hls_start(body, self.picked), body)

    def test_no_start_for_live_playlist_without_endlist(self):
        live = _vod_playlist().replace("#EXT-X-ENDLIST\n", "")
        with patch.object(appmod.history, "get",
                          return_value={"ts": 100}):
            self.assertEqual(appmod._inject_hls_start(live, self.picked), live)

    def test_injection_is_idempotent(self):
        with patch.object(appmod.history, "get",
                          return_value={"ts": 1089}):
            once = appmod._inject_hls_start(_vod_playlist(), self.picked)
            twice = appmod._inject_hls_start(once, self.picked)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("#EXT-X-START"), 1)

    def test_no_start_without_ep_guid(self):
        body = _vod_playlist()
        with patch.object(appmod.history, "get",
                          return_value={"ts": 1089}):
            self.assertEqual(
                appmod._inject_hls_start(body, {"ep_guid": ""}), body)


if __name__ == "__main__":
    unittest.main()
