"""阿里云盘"读取(OAuth)/转存(web)"两套凭证并存、互不覆盖的回归。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import panali


class PanaliCredCoexistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / "ali.json")
        self.p = patch.object(panali, "_STATE_PATH", self.state)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_oauth_read_login_does_not_clobber_web_transfer_cred(self):
        # 先 web（可转存）登录
        panali._store_web_tokens({"access_token": "WEB_AT",
                                  "refresh_token": "WEB_RT",
                                  "expires_in": 7200, "resource_drive_id": "DRV"})
        self.assertTrue(panali._is_web_mode())
        self.assertEqual(panali._load().get("web_refresh_token"), "WEB_RT")
        # 随后 OAuth（读取）登录：mode 置 open，但不得清除 web 转存凭证
        panali._store_tokens({"access_token": "OPEN_AT",
                              "refresh_token": "OPEN_RT", "expires_in": 7200})
        st = panali._load()
        self.assertEqual(st.get("mode"), "open")
        self.assertEqual(st.get("web_refresh_token"), "WEB_RT")
        self.assertTrue(panali._is_web_mode())  # web 能力仍可被检测到
        # web_access_token 必须用 WEB refresh_token 续期，而非 OAuth 的
        with patch.object(panali, "_web_refresh",
                          return_value={"access_token": "WEB_AT2",
                                        "refresh_token": "WEB_RT2",
                                        "expires_in": 7200}) as wr:
            panali.web_access_token(force=True)
        wr.assert_called_once_with("WEB_RT")

    def test_open_only_login_has_no_web_capability(self):
        panali._store_tokens({"access_token": "OPEN_AT",
                              "refresh_token": "OPEN_RT", "expires_in": 7200})
        self.assertFalse(panali._is_web_mode())

    def test_legacy_web_mode_shared_key_fallback(self):
        # 旧状态：仅 mode=web + 共享 refresh_token，无专用 web 键 → 仍可续期
        panali._save({"mode": "web", "access_token": "OLD_AT",
                      "refresh_token": "OLD_RT", "expires_at": 0})
        self.assertTrue(panali._is_web_mode())
        with patch.object(panali, "_web_refresh",
                          return_value={"access_token": "NEW_AT",
                                        "refresh_token": "NEW_RT",
                                        "expires_in": 7200}) as wr:
            panali.web_access_token(force=True)
        wr.assert_called_once_with("OLD_RT")

    def test_logout_clears_web_credentials(self):
        panali._store_web_tokens({"access_token": "WEB_AT",
                                  "refresh_token": "WEB_RT", "expires_in": 7200})
        panali.logout()
        st = panali._load()
        self.assertNotIn("web_refresh_token", st)
        self.assertFalse(panali._is_web_mode())


if __name__ == "__main__":
    unittest.main()
