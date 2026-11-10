"""subfetch 代理兼容性与抓取回退的离线契约测试。

背景：本机 httpx 0.23.3 的 Client 只接受 proxies=（proxy= 在 0.26 才引入）。
fetch_first 旧实现硬编码 proxy= 且无回退，配置订阅代理时会抛 TypeError，
导致 drpy.import_remote 下载规则脚本静默全失败。这里锁定兼容工厂与抓取路径。
全部离线：网络调用用 mock，不发真实请求。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import httpx  # noqa: E402
import subfetch  # noqa: E402

PROXY = "http://user:pass@192.168.31.198:7893"


class ProxyKwargsTests(unittest.TestCase):
    def test_empty_proxy_returns_no_kwargs(self):
        self.assertEqual(subfetch._proxy_kwargs(""), {})

    def test_proxy_kwargs_picks_supported_param_name(self):
        kw = subfetch._proxy_kwargs(PROXY)
        # 必须命中当前 httpx 实际支持的那个参数名，且参数值正确
        self.assertEqual(len(kw), 1)
        self.assertIn(kw, ({"proxy": PROXY}, {"proxies": PROXY}))
        # 用返回的关键字必须能真正构造出客户端（不抛 TypeError）
        httpx.Client(trust_env=False, **kw).close()

    def test_sub_client_constructs_with_configured_proxy(self):
        with patch.object(subfetch, "subscription_proxy", return_value=PROXY):
            cli = subfetch.sub_client()
            try:
                self.assertIsInstance(cli, httpx.Client)
            finally:
                cli.close()

    def test_sub_client_direct_when_no_proxy(self):
        with patch.object(subfetch, "subscription_proxy", return_value=""):
            cli = subfetch.sub_client()
            try:
                self.assertIsInstance(cli, httpx.Client)
            finally:
                cli.close()


class FetchFirstProxyTests(unittest.TestCase):
    def test_fetch_first_succeeds_through_old_httpx_with_proxy(self):
        # 复现旧 bug：若 fetch_first 仍硬编码 proxy=，在 httpx 0.23 下会 TypeError
        captured = {}

        class _Resp:
            content = b"// drpy rule\nfunction home(){}"

            def raise_for_status(self):
                return None

        class _FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                return _Resp()

        with patch.object(subfetch, "subscription_proxy", return_value=PROXY), \
                patch.object(httpx, "Client", _FakeClient):
            blob, hit = subfetch.fetch_first(["https://example.test/a.js"])

        self.assertEqual(blob, _Resp.content)
        self.assertEqual(hit, "https://example.test/a.js")
        # 代理必须以当前 httpx 支持的参数名传入（旧实现这里会是不被支持的 proxy=）
        self.assertTrue("proxy" in captured or "proxies" in captured)
        self.assertEqual(captured.get("proxy", captured.get("proxies")), PROXY)

    def test_fetch_first_returns_none_when_all_fail(self):
        class _BoomClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                raise httpx.ConnectError("nxdomain")

        with patch.object(subfetch, "subscription_proxy", return_value=""), \
                patch.object(httpx, "Client", _BoomClient):
            blob, err = subfetch.fetch_first(["https://example.test/x.js"])
        self.assertIsNone(blob)
        self.assertIn("ConnectError", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
