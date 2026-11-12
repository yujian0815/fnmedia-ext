"""片段跳过、原生 play_config 与自动连播 Episode 契约。"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import history
import inject
import skip_marks as sm


class SkipMarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "skip_marks.json"
        self._patch = unittest.mock.patch.object(sm, "_STATE", self.state)
        self._patch.start()
        self.tokens = history.set_current_user("user-1")

    def tearDown(self):
        history.reset_current_user(self.tokens)
        self._patch.stop()
        self.tmp.cleanup()

    def test_native_config_set_get_and_disable(self):
        self.assertEqual(sm.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})
        sm.set_mark("season-1", {"skip_opening": 90, "skip_ending": 120})
        self.assertEqual(sm.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 90, "skip_ending": 120})
        sm.set_mark("season-1", {"enabled": False})
        self.assertEqual(sm.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})
        self.assertEqual(sm.get("season-1")["skip_opening"], 90)

    def test_libretv_segment_validation_and_resolution(self):
        sm.set_mark("season-1", {
            "skip_opening": 85,
            "segments": [
                {"type": "opening", "start": 0, "end": 85},
                {"type": "middle", "start": 300, "end": 330},
                {"type": "ending", "start": 1700, "end": -1},
                {"type": "middle", "start": 320, "end": 340},  # 过近，丢弃
                {"type": "middle", "start": -1, "end": 20},   # 非法，丢弃
            ]})
        segments = sm.resolve_segments("season-1", 1800)
        self.assertEqual([(s["type"], s["start"], s["end"]) for s in segments],
                         [("opening", 0, 85), ("middle", 300, 330),
                          ("ending", 1700, 1800)])
        self.assertEqual(sm.find_active_segment("season-1", 310, 1800)["type"],
                         "middle")
        self.assertIsNone(sm.find_active_segment("season-1", 1000, 1800))

    def test_custom_hls_skip_is_opt_in_and_middle_only(self):
        self.assertEqual(sm.hls_ranges("season-1"), [])
        sm.set_mark("season-1", {"custom_hls_skip": True, "segments": [
            {"type": "opening", "start": 0, "end": 80},
            {"type": "middle", "start": 300, "end": 330},
            {"type": "ending", "start": 1700, "end": -1},
        ]})
        self.assertEqual(sm.hls_ranges("season-1"), [(300, 330)])

    def test_injected_play_info_uses_native_config(self):
        sm.set_mark("season-1", {"skip_opening": 85, "skip_ending": 100})
        movie = inject.play_info({"guid": "movie-1", "title": "影片",
                                  "type_name": "电影", "lines": {}})
        self.assertEqual(movie["play_config"],
                         {"guid": "movie-1", "skip_opening": 0, "skip_ending": 0})
        ep = {"ep_guid": "ep-1", "online_guid": "show-1", "season_guid": "season-1",
              "season": 1, "index": 0, "episode_number": 1, "ep_title": "第1集"}
        info = inject.episode_play_info(ep)
        self.assertEqual(info["play_config"],
                         {"guid": "season-1", "skip_opening": 85, "skip_ending": 100})
        # 原生自动连播依赖的 Episode 身份必须完整且稳定，无需新增 next 路由。
        self.assertEqual(info["type"], "Episode")
        self.assertEqual(info["parent_guid"], "season-1")
        self.assertEqual(info["item"]["parent_guid"], "season-1")
        self.assertEqual(info["item"]["episode_number"], 1)
        self.assertEqual(info["item"]["guid"], "ep-1")


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class SkipConfigRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_config_normalizes_line_season_identity(self):
        import app as appmod
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(sm, "_STATE", Path(tmp) / "skip_marks.json"):
            guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
            response = await appmod.play_set_config_by_item(_Request({
                "guid": guid, "skip_opening": 90, "skip_ending": 110}))
            self.assertEqual(response.status_code, 200)
            data = response.body
            import json
            payload = json.loads(data)
            base = appmod.season_guid("og", 1)
            self.assertEqual(payload["data"]["guid"], base)
            self.assertEqual(payload["data"]["play_config"],
                             {"guid": base, "skip_opening": 90, "skip_ending": 110})
            self.assertEqual(sm.play_config(base),
                             {"guid": base, "skip_opening": 90, "skip_ending": 110})


if __name__ == "__main__":
    unittest.main()
