"""盘搜 302 免中转改造回归：相关度归一化、关键词拆分、阿里/UC 直链 302。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import pansou
import app as appmod


class PansouRelevanceTests(unittest.TestCase):
    def test_colon_variant_matches(self):
        # 归一化去冒号：分享 note 带冒号也能匹配无冒号关键词（此前漏掉阿里/UC 源）
        self.assertEqual(
            pansou._relevance("生化危机：爆发夜 Resident Evil (2026)", "生化危机爆发夜"),
            100)

    def test_unrelated_rejected(self):
        self.assertEqual(pansou._relevance("流浪地球2 (2023)", "生化危机爆发夜"), 0)

    def test_token_fallback(self):
        # 空格分词逐词命中
        self.assertGreater(
            pansou._relevance("生化危机 爆发夜 4K", "生化危机 爆发夜"), 0)

    def test_search_kw_keeps_subtitle_space_separated(self):
        kw = appmod._pansou_search_kw("生化危机：爆发夜")
        self.assertIn("爆发夜", kw)
        self.assertIn(" ", kw)  # 空格分隔便于拆主名做双关键词召回


class PansouDirect302Tests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_302_to_ali_signed_url_no_relay(self):
        # 阿里原画直链免 Cookie：/direct 应 302 到签名直链（客户端直连 CDN，免 NAS 中转）
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "query_string": b"index=0", "headers": []})
        snap = {"url": "/v/api/ext/pan/ali/play?fid=FID1", "pan": True,
                "pan_provider": "ali", "m3u8": False,
                "line": "builtin-pansou::PanSou网盘高清"}
        fake_mod = type("M", (), {})()
        fake_mod.playable = lambda fid: {"url": "https://dl1-v6.aliyundrive.cloud/signed", "m3u8": False}
        with patch.object(appmod, "_fixed_media_target",
                          return_value={"_pansou_manual": True}), \
                patch.object(appmod, "_await_pansou_target",
                          new=AsyncMock(return_value=snap)), \
                patch.dict(appmod._PAN_MODS, {"ali": fake_mod}, clear=False):
            resp = await appmod.direct_proxy("MGX", req)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"],
                         "https://dl1-v6.aliyundrive.cloud/signed")

    async def test_direct_quark_falls_back_to_relay(self):
        # 夸克需 Cookie：/direct 302 回 /pan/play 由 NAS 注入 Cookie 中转
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "query_string": b"index=0", "headers": []})
        snap = {"url": "/v/api/ext/pan/play?fid=FID2", "pan": True,
                "pan_provider": "quark", "m3u8": False,
                "line": "builtin-pansou::PanSou网盘高清"}
        with patch.object(appmod, "_fixed_media_target",
                          return_value={"_pansou_manual": True}), \
                patch.object(appmod, "_await_pansou_target",
                          new=AsyncMock(return_value=snap)):
            resp = await appmod.direct_proxy("MGX", req)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/v/api/ext/pan/play?fid=FID2")


class PansouDeadMsgTests(unittest.TestCase):
    def test_dead_msg_window(self):
        import time as _t
        mg = "MGDEAD"
        with patch.object(appmod, "_pansou_manual_state", return_value="dead"), \
                patch.object(appmod, "_pansou_is_user_pick", return_value=True), \
                patch.dict(appmod._PAN_DEAD_MSG_T0, {}, clear=True):
            self.assertTrue(appmod._pansou_dead_msg_active(mg))   # 首次记 t0
            self.assertTrue(appmod._pansou_dead_msg_active(mg))   # 窗口内
            appmod._PAN_DEAD_MSG_T0[mg] = _t.time() - appmod._PAN_DEAD_MSG_WIN - 1
            self.assertFalse(appmod._pansou_dead_msg_active(mg))  # 窗口过后放行
            # 过期后保持 False，不重置 t0 重新提示
            self.assertFalse(appmod._pansou_dead_msg_active(mg))

    def test_dead_msg_requires_user_pick(self):
        with patch.object(appmod, "_pansou_manual_state", return_value="dead"), \
                patch.object(appmod, "_pansou_is_user_pick", return_value=False), \
                patch.dict(appmod._PAN_DEAD_MSG_T0, {}, clear=True):
            self.assertFalse(appmod._pansou_dead_msg_active("MGX"))

    def test_dead_msg_only_when_dead(self):
        with patch.object(appmod, "_pansou_manual_state", return_value="ready"), \
                patch.object(appmod, "_pansou_is_user_pick", return_value=True), \
                patch.dict(appmod._PAN_DEAD_MSG_T0, {}, clear=True):
            self.assertFalse(appmod._pansou_dead_msg_active("MGX"))

    def test_message_ts_builds(self):
        # 纯文字提示分片可生成（ffmpeg 可用时）；无 ffmpeg 环境跳过。
        import panqr
        if not panqr.ffmpeg_available():
            self.skipTest("ffmpeg not available")
        data = panqr.build_message_ts("网盘分享已失效", "已切换在线线路")
        self.assertTrue(data and len(data) > 1000)
        self.assertEqual(data[:1], b"\x47")  # TS 同步字节


if __name__ == "__main__":
    unittest.main()
