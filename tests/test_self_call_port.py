"""全端口兼容：本机自调用经 unix socket，不依赖 nginx 监听端口。

背景：插件经 supervise 接管官方 unix socket（/var/run/trim_media.sock），
nginx 在 5666/5667/80/443（或用户自定义端口）转发到该 socket。旧实现把根相对
地址绝对化为写死的 http://127.0.0.1:5666 再自调用，用户改非标准端口即失败。
现改为：_self_url 用无端口哨兵 host，_self_or 对本应用 URL 走 uds 客户端，
端口完全无关。本文件锁定该契约，防止回退到写死端口。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


def _fake_req(ext) -> Request:
    """构造带 app.state.ext 的最小 Request（不触发网络）。"""
    state = types.SimpleNamespace(ext=ext)
    appobj = types.SimpleNamespace(state=state)
    return Request({"type": "http", "method": "GET", "path": "/",
                    "query_string": b"", "headers": [], "app": appobj})


class SelfCallPortTests(unittest.TestCase):
    def test_self_url_has_no_fixed_port(self):
        # 根相对 → 无端口哨兵 base；绝不内嵌 5666/5667（非标准端口兼容的核心）
        u = appmod._self_url("/v/media/online/mg/main.m3u8?index=0")
        self.assertTrue(u.startswith(appmod.SELF_BASE))
        self.assertNotIn(":5666", u)
        self.assertNotIn(":5667", u)
        self.assertIn("/v/media/online/mg/main.m3u8?index=0", u)
        # 外部绝对地址原样保留
        ext = "https://cdn.example/x.m3u8"
        self.assertEqual(appmod._self_url(ext), ext)

    def test_is_self_url(self):
        self.assertTrue(appmod._is_self_url(appmod.SELF_BASE + "/v/x"))
        self.assertFalse(appmod._is_self_url("https://cdn.example/x.m3u8"))
        # 旧写死端口地址不再被判为“本应用自调用”（避免误走 uds 之外的旧路径）
        self.assertFalse(appmod._is_self_url("http://127.0.0.1:5666/v/x"))

    def test_self_or_routes_self_via_uds(self):
        ext = object()  # 外部客户端哨兵；自调用不应复用它
        req = _fake_req(ext)
        with tempfile.NamedTemporaryFile() as tf, \
                patch.object(appmod, "SELF_SOCK", tf.name), \
                patch.object(appmod, "_SELF_UDS", True):
            self_url = appmod._self_url("/v/x")
            cli = appmod._self_or(ext, req, self_url)
            # 本应用 URL → 独立的 uds 自调用客户端（非外部 ext），并缓存
            self.assertIsNot(cli, ext)
            self.assertIs(cli, req.app.state.self_cli)
            self.assertIsInstance(cli._transport, httpx.AsyncHTTPTransport)
            # 外部 URL → 仍用原客户端
            self.assertIs(appmod._self_or(ext, req, "https://cdn.example/x"), ext)
            asyncio.run(cli.aclose())

    def test_self_client_falls_back_to_ext_without_socket(self):
        ext = object()
        req = _fake_req(ext)
        with patch.object(appmod, "SELF_SOCK", "/nonexistent/fnmedia-self.sock"), \
                patch.object(appmod, "_SELF_UDS", True):
            # 无接管 socket（如 supervise 未运行）→ 回退 ext，不构建 uds
            self.assertIs(appmod.self_client(req), ext)

    def test_tcp_fallback_when_self_base_set(self):
        # 显式 FNM_SELF_BASE（TCP 回退）→ _SELF_UDS=False，自调用仍走原客户端
        ext = object()
        req = _fake_req(ext)
        with patch.object(appmod, "_SELF_UDS", False):
            self_url = appmod._self_url("/v/x")
            self.assertIs(appmod._self_or(ext, req, self_url), ext)


if __name__ == "__main__":
    unittest.main()