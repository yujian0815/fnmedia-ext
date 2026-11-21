"""真实接管开关：只调度独立 systemd 单元，测试不得停止本机服务。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
from starlette.requests import Request


def _request(payload: dict, *, confirmed: bool = True) -> Request:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if confirmed:
        headers.append((b"x-fnmedia-admin", b"1"))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST",
                    "path": "/v/api/ext/takeover/disable",
                    "headers": headers, "query_string": b""}, receive)


class TakeoverControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_active_takeover(self):
        with patch.object(appmod.config, "get", return_value={"source_priority": {}}), \
                patch.object(appmod.config, "all_sources", return_value=[]), \
                patch.object(appmod.hot_cache, "status", return_value={}), \
                patch.object(appmod.chart_cache, "status", return_value={}), \
                patch.object(appmod.catalog, "status", return_value={}):
            result = await appmod.ext_status(_request({}))
        self.assertTrue(result["ok"])
        self.assertIs(result["takeover_active"], True)

    async def test_disable_requires_explicit_admin_confirmation(self):
        with patch.object(appmod, "_schedule_takeover_disable") as schedule:
            response = await appmod.ext_takeover_disable(
                _request({"confirm": True}, confirmed=False))
        self.assertEqual(response.status_code, 403)
        schedule.assert_not_called()

    async def test_disable_accepts_and_schedules_independent_stop(self):
        with patch.object(appmod, "_schedule_takeover_disable") as schedule:
            result = await appmod.ext_takeover_disable(_request({"confirm": True}))
        self.assertEqual(result, {"ok": True, "accepted": True,
                                  "restore": "app_center"})
        schedule.assert_called_once_with()

    async def test_schedule_failure_does_not_report_success(self):
        with patch.object(appmod, "_schedule_takeover_disable",
                          side_effect=RuntimeError("mock unavailable")):
            response = await appmod.ext_takeover_disable(_request({"confirm": True}))
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.body)
        self.assertFalse(body["ok"])
        self.assertIn("mock unavailable", body["msg"])

    def test_schedule_uses_fixed_delayed_disable_command(self):
        with patch("shutil.which", return_value="/usr/bin/mock-tool"), \
                patch("subprocess.run") as run:
            appmod._schedule_takeover_disable()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["systemd-run", "--no-block", "--unit", "fnmedia-ext-disable"])
        self.assertIn("--collect", cmd)
        self.assertIn("--no-block", cmd)
        self.assertEqual(cmd[-3], "/bin/sh")
        self.assertEqual(cmd[-2], "-c")
        self.assertEqual(cmd[-1],
                         "sleep 2 && exec systemctl disable --now fnmedia-ext.service")
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
