"""PR89：TV 一级线路目录的默认播放身份，其它端保持 Directory。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
from store import episode_guid


def _brief(tv=True):
    return {"guid": "og", "title": "剧集" if tv else "电影", "year": "2026",
            "type_name": "电视剧" if tv else "电影", "poster": "",
            "overview": "", "lines": {"lzi": [{"title": "第01集", "url": "u"}]}}


def _tv_client():
    return clientmod.Client(kind=clientmod.TV, is_tv=True, strict_card=True)


class TvFolderPlayTests(unittest.TestCase):
    def test_tv_folder_detail_points_to_resume_episode(self):
        expected = episode_guid("og", "", 3)
        with patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                patch.object(appmod, "_vod_resume_index", return_value=3):
            resp = appmod._vod_folder_detail(
                appmod._line_folder_guid("og"), _tv_client())
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["type"], "Directory")
        self.assertEqual(data["can_play"], 1)
        self.assertEqual(data["play_item_guid"], expected)

    def test_web_folder_detail_remains_non_playable_directory(self):
        with patch.object(appmod.store, "get_title_by_guid", return_value=_brief()):
            resp = appmod._vod_folder_detail(
                appmod._line_folder_guid("og"), clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["can_play"], 0)
        self.assertEqual(data["play_item_guid"], "")

    def test_movie_folder_not_changed_on_tv(self):
        with patch.object(appmod.store, "get_title_by_guid",
                          return_value=_brief(tv=False)):
            resp = appmod._vod_folder_detail(
                appmod._line_folder_guid("og"), _tv_client())
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["can_play"], 0)
        self.assertEqual(data["play_item_guid"], "")

    def test_tv_folder_play_info_uses_resume_episode(self):
        expected = episode_guid("og", "", 2)
        with patch.object(appmod.store, "get_title_by_guid", return_value=_brief()), \
                patch.object(appmod.store, "get_episode",
                             return_value={"ep_guid": expected}), \
                patch.object(appmod, "_vod_resume_index", return_value=2), \
                patch.object(appmod.inj, "episode_play_info",
                             return_value={"type": "Episode", "guid": expected}), \
                patch.object(appmod.history, "get", return_value={"ts": 123}):
            resp = appmod._vod_folder_play_info(
                appmod._line_folder_guid("og"), _tv_client())
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["type"], "Episode")
        self.assertEqual(data["guid"], expected)
        self.assertEqual(data["ts"], 123)

    def test_web_folder_play_info_stays_directory(self):
        with patch.object(appmod.store, "get_title_by_guid", return_value=_brief()):
            resp = appmod._vod_folder_play_info(
                appmod._line_folder_guid("og"), clientmod.Client(kind="web"))
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["type"], "Directory")
        self.assertEqual(data["item"]["can_play"], 0)


if __name__ == "__main__":
    unittest.main()
