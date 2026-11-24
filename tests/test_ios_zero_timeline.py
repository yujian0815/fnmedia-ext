"""iOS零基续播MKV：播放位置与全片历史坐标回归。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import app as appmod  # noqa: E402
import client as clientmod  # noqa: E402


UA = b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"


def _request(query: bytes = b"", play_link: str = "") -> Request:
    headers = [(b"user-agent", UA)]
    if play_link:
        headers.append((b"play-link", play_link.encode()))
    return Request({"type": "http", "method": "GET",
                    "path": "/v/media/online/mg/relay", "query_string": query,
                    "headers": headers})


def _json_request(payload: dict) -> Request:
    raw = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    return Request({"type": "http", "method": "POST",
                    "path": "/v/api/v1/play/record", "query_string": b"",
                    "headers": [(b"user-agent", UA),
                                (b"content-type", b"application/json")]}, receive)


def _job(start=1420):
    return {"key": "relay-key", "start": start, "duration": 2773,
            "duration_source": "playlist", "generation": "g" * 16,
            "zero_based": bool(start), "proc": type("Proc", (), {"returncode": None})(),
            "path": "/tmp/unused.mkv", "duration_header": False}


class IosZeroTimelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        appmod._RELAY_PLAY_CONTEXT.clear()
        appmod._RELAY_PLAY_SESSION.clear()
        appmod._RELAY_JOBS.clear()
        self.addCleanup(appmod._RELAY_PLAY_CONTEXT.clear)
        self.addCleanup(appmod._RELAY_PLAY_SESSION.clear)
        self.addCleanup(appmod._RELAY_JOBS.clear)

    def test_session_bound_at_entry_maps_record_without_relay_token(self):
        # relay字节请求不带play-link：只写media上下文；入口(media_range)带令牌绑定会话。
        job = _job(1420)
        job["timeline_start"] = 1417.6
        appmod._bind_ios_relay_play_context("mg", job, _request())
        appmod._bind_ios_relay_session("mg", "g" * 16, _request(play_link="S"))
        body = {"media_guid": "mg", "play_link": "S", "ts": 80, "duration": 1353}
        mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1498)  # round(1417.6+80)
        self.assertEqual(mapped["duration"], 2773)

    def test_play_play_body_session_binding_maps_record(self):
        job = _job(1420)
        job["timeline_start"] = 1420.0
        appmod._bind_ios_relay_play_context("mg", job, _request())
        appmod._bind_ios_relay_session("mg", "g" * 16, _request(), session="S2")
        body = {"media_guid": "mg", "play_link": "S2", "ts": 80}
        mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)

    def test_stream_play_link_binds_session_for_record(self):
        target = {"url": "https://cdn.test/v.m3u8", "line": "youku::ukm3u8",
                  "index": 14, "online_guid": "og", "ep_guid": "eg"}
        with patch.object(appmod, "_ios_resume_seconds", return_value=1420), \
                patch.object(appmod, "_ios_relay_url_identity",
                             return_value=(1420, "g" * 16)):
            appmod._stream_play_link(target, "mg", _request(play_link="S3"))
        job = _job(1420)
        job["timeline_start"] = 1420.0
        appmod._bind_ios_relay_play_context("mg", job, _request())
        mapped, allowed = appmod._ios_relay_history_body(
            {"media_guid": "mg", "play_link": "S3", "ts": 80}, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)

    def test_evicted_media_context_with_registry_rejected(self):
        appmod._bind_ios_relay_session("mg", "g" * 16, _request(play_link="S4"))
        # media上下文缺失（双缓存淘汰）：不得把相对ts当全局写入。
        with patch.object(appmod, "_pick_episode", return_value=None):
            _mapped, allowed = appmod._ios_relay_history_body(
                {"media_guid": "mg", "play_link": "S4", "ts": 80}, _request())
        self.assertFalse(allowed)

    def test_record_resolves_job_when_byte_user_differs(self):
        # 字节请求匿名(user="")写media上下文；record带令牌(user=u1)。按代际从job取坐标。
        job = _job(1420)
        job["timeline_start"] = 1420.0
        job["mg"] = "mg"
        appmod._RELAY_JOBS["relay-key"] = job
        with patch.object(appmod.histmod, "current_user", return_value=""):
            appmod._bind_ios_relay_play_context("mg", job, _request())
        with patch.object(appmod.histmod, "current_user", return_value="u1"):
            appmod._bind_ios_relay_session("mg", "g" * 16, _request(play_link="S5"))
            mapped, allowed = appmod._ios_relay_history_body(
                {"media_guid": "mg", "play_link": "S5", "ts": 80}, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)

    def test_record_resolves_generation_from_relay_url_session(self):
        job = _job(1420)
        job["timeline_start"] = 1420.0
        job["mg"] = "mg"
        appmod._RELAY_JOBS["relay-key"] = job
        url = f"/v/media/online/mg/relay?line=x&index=14&start=1420&g={'g' * 16}"
        mapped, allowed = appmod._ios_relay_history_body(
            {"media_guid": "mg", "play_link": url, "ts": 80}, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)

    def test_known_generation_but_job_gone_rejected(self):
        with patch.object(appmod.histmod, "current_user", return_value="u1"):
            appmod._bind_ios_relay_session("mg", "g" * 16, _request(play_link="S6"))
            _mapped, allowed = appmod._ios_relay_history_body(
                {"media_guid": "mg", "play_link": "S6", "ts": 80}, _request())
        self.assertFalse(allowed)

    def test_forged_generation_of_other_media_rejected(self):
        job = _job(1420)
        job["timeline_start"] = 1420.0
        job["mg"] = "other-mg"
        appmod._RELAY_JOBS["relay-key"] = job
        url = f"/v/media/online/mg/relay?line=x&index=14&start=1420&g={'g' * 16}"
        with patch.object(appmod, "_pick_episode", return_value=None):
            _mapped, allowed = appmod._ios_relay_history_body(
                {"media_guid": "mg", "play_link": url, "ts": 80}, _request())
        self.assertFalse(allowed)

    def test_evicted_session_generation_mismatch_still_rejected(self):
        # 会话登记代际与当前media上下文代际不一致→旧会话迟到record不得套用新坐标。
        appmod._bind_ios_relay_session("mg", "old" + "g" * 13, _request(play_link="S"))
        new_job = _job(0)  # 新全片会话，generation不同
        new_job["generation"] = "new" + "g" * 13
        appmod._bind_ios_relay_play_context("mg", new_job, _request())
        body = {"media_guid": "mg", "play_link": "S", "ts": 80}
        _mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertFalse(allowed)

    async def test_requested_duration_not_committed_before_real_origin(self):
        job = _job(1420)
        self.assertFalse(job.get("origin_resolved"))
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            appmod._relay_write_duration(job)
        write.assert_not_called()
        self.assertFalse(job["duration_header"])

        payload = json.dumps({
            "format": {"duration": "2773.000000"},
            "packets": [{"pts_time": "1417.600000", "flags": "K__"}],
        }).encode()

        class Probe:
            returncode = 0

            async def communicate(self):
                return payload, b""

        target = {"url": "https://cdn.test/v.m3u8", "headers": {},
                  "online_guid": "og", "index": 14, "line": "youku::ukm3u8"}
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=Probe())):
            await appmod._relay_probe_meta(job, target)
        self.assertTrue(job["origin_resolved"])
        self.assertEqual(job["timeline_start"], 1417.6)
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            appmod._relay_write_duration(job)
        write.assert_called_once()
        self.assertAlmostEqual(write.call_args.args[1], 1355.4, places=3)
        self.assertTrue(job["duration_header"])

    async def test_probe_failure_still_resolves_origin_and_writes_duration(self):
        job = _job(1420)

        class ProbeFail:
            returncode = None

            async def communicate(self):
                raise asyncio.TimeoutError()

            def kill(self):
                pass

        target = {"url": "https://cdn.test/v.m3u8", "headers": {},
                  "online_guid": "og", "index": 14, "line": "youku::ukm3u8"}
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=ProbeFail())):
            await appmod._relay_probe_meta(job, target)
        self.assertTrue(job["origin_resolved"])
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            appmod._relay_write_duration(job)
        write.assert_called_once()
        self.assertAlmostEqual(write.call_args.args[1], 1353.0, places=3)

    def test_play_info_uses_relative_position_and_remaining_duration(self):
        target = {"url": "https://cdn.test/v.m3u8", "line": "youku::ukm3u8",
                  "index": 14, "online_guid": "og"}
        job = _job()
        info = {"ts": 1500, "item": {"ts": 1500, "watched_ts": 1500,
                                      "duration": 2773, "runtime": 46}}
        request = _request()
        with patch.object(appmod, "_ios_relay_job_for",
                          return_value=(job, 1420, job["key"])):
            appmod._ios_zero_timeline_play_info(info, target, request)
            job["size_est"] = 0
            stream = {"video_streams": [{"duration": 0}],
                      "files": [{"path": "在线/video.m3u8", "size": 0}]}
            appmod._apply_ios_relay_metadata(
                stream, {"title": "video", "runtime": 46}, target, request)
        self.assertEqual(info["ts"], 80)
        self.assertEqual(info["item"]["ts"], 80)
        self.assertEqual(info["item"]["watched_ts"], 80)
        self.assertEqual(info["item"]["duration"], 1353)
        self.assertEqual(info["item"]["runtime"], 22)
        self.assertEqual(stream["video_streams"][0]["duration"], 1353)
        self.assertEqual(stream["files"][0]["size"], 676500000)

        fresh = {"ts": 1420, "item": {"ts": 1420, "watched_ts": 1420,
                                       "duration": 2773, "runtime": 46}}
        with patch.object(appmod, "_ios_relay_job_for",
                          return_value=({}, 1420, "new-key")), \
                patch.object(appmod, "_relay_duration_fallback", return_value=2773):
            appmod._ios_zero_timeline_play_info(fresh, target, _request())
        self.assertEqual(fresh["ts"], 0)
        self.assertEqual(fresh["item"]["duration"], 1353)

        untouched = {"ts": 1500, "item": {"ts": 1500}}
        other = Request({"type": "http", "method": "GET", "path": "/",
                         "query_string": b"", "headers": []})
        appmod._ios_zero_timeline_play_info(untouched, target, other)
        self.assertEqual(untouched["ts"], 1500)

    def test_fixed_line_play_info_uses_zero_based_job_timeline(self):
        brief = {"guid": "og", "title": "视频", "type_name": "电影",
                 "year": "2026", "poster": "", "overview": "", "runtime": 46}
        target = {"online_guid": "og", "index": 0, "line": "youku::ukm3u8",
                  "ep_guid": "", "url": "https://cdn.test/v.m3u8", "headers": {}}
        job = _job()
        request = _request()
        guid = appmod._line_item_guid("og", target["line"], 0)
        with patch.object(appmod, "_fixed_media_target", return_value=target), \
                patch.object(appmod.store, "get_title_by_guid", return_value=brief), \
                patch.object(appmod.store, "resolve_episode", return_value=target), \
                patch.object(appmod.history, "get",
                             return_value={"ts": 1500, "duration": 2773}), \
                patch.object(appmod, "_ios_relay_job_for",
                             return_value=(job, 1420, job["key"])), \
                patch.object(appmod, "_ios_prepare_playlist"):
            response = appmod._vod_play_info(
                guid, clientmod.Client(kind="ios"), request)
        data = json.loads(response.body)["data"]
        self.assertEqual(data["ts"], 80)
        self.assertEqual(data["item"]["duration"], 1353)
        self.assertEqual(data["item"]["runtime"], 22)

    def test_t4_hls_keeps_global_timeline(self):
        target = {"url": "https://cdn.test/v.m3u8", "line": "t4::line",
                  "index": 0, "online_guid": "og", "t4": {"id": "source"}}
        info = {"ts": 123, "item": {"ts": 123, "watched_ts": 123,
                                      "duration": 3600, "runtime": 60}}
        original = json.loads(json.dumps(info))
        appmod._ios_zero_timeline_play_info(info, target, _request())
        self.assertEqual(info, original)
        job = _job(start=123)
        job["duration"] = 3600
        with patch.object(appmod, "_ios_relay_job_for",
                          return_value=(job, 123, job["key"])):
            self.assertEqual(appmod._ios_relay_duration(
                target, {"runtime": 60}, _request()), 3600)

    async def test_actual_keyframe_origin_drives_all_timeline_mappings(self):
        job = _job(1420)
        job.update(timeline_start=1420.0, duration_header=True, size_est=0)
        payload = json.dumps({
            "format": {"duration": "2773.000000"},
            "packets": [{"pts_time": "1417.600000", "flags": "K__"}],
        }).encode()

        class Probe:
            returncode = 0

            async def communicate(self):
                return payload, b""

        target = {"url": "https://cdn.test/v.m3u8", "headers": {},
                  "online_guid": "og", "index": 14, "line": "youku::ukm3u8"}
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=Probe())) as spawn:
            await appmod._relay_probe_meta(job, target)
        self.assertEqual(job["timeline_start"], 1417.6)
        self.assertFalse(job["duration_header"])
        self.assertEqual(job["size_est"], int((2773 - 1417.6) * 500000))
        self.assertIn("1420%+#1", spawn.await_args.args)
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            appmod._relay_write_duration(job)
        self.assertAlmostEqual(write.call_args.args[1], 1355.4)

        info = {"ts": 1500, "item": {"ts": 1500, "watched_ts": 1500,
                                      "duration": 2773, "runtime": 46}}
        with patch.object(appmod, "_ios_relay_job_for",
                          return_value=(job, 1420, job["key"])):
            appmod._ios_zero_timeline_play_info(info, target, _request())
        self.assertEqual(info["ts"], 82)
        self.assertEqual(info["item"]["duration"], 1355)

        appmod._bind_ios_relay_play_context(
            "mg", job, _request(play_link="actual-session"))
        mapped, allowed = appmod._ios_relay_history_body(
            {"media_guid": "mg", "play_link": "actual-session",
             "ts": 82, "duration": 1355}, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)
        self.assertEqual(mapped["duration"], 2773)

    def test_duration_header_is_remaining_for_zero_based_suffix(self):
        job = _job()
        job["origin_resolved"] = True
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            appmod._relay_write_duration(job)
        write.assert_called_once_with(job["path"], 1353.0)
        self.assertTrue(job["duration_header"])

    async def test_play_record_maps_relative_time_back_to_global_history(self):
        request = _request()
        appmod._bind_ios_relay_play_context("mg", _job(), request)
        body = {"item_guid": "og", "media_guid": "mg", "ts": 80,
                "duration": 1353}
        mapped, allowed = appmod._ios_relay_history_body(body, request)
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)
        self.assertEqual(mapped["duration"], 2773)
        reset, allowed = appmod._ios_relay_history_body(
            {**body, "ts": 0, "reset": True}, request)
        self.assertTrue(allowed)
        self.assertEqual(reset["ts"], 0)

        with patch.object(appmod.store, "get_title_by_guid", return_value={"title": "t"}), \
                patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod, "_history_snapshot", return_value=None), \
                patch.object(appmod.history, "record") as record:
            response = await appmod.play_record(_json_request(body))
        self.assertEqual(response.status_code, 200)
        recorded = record.call_args.args[0]
        self.assertEqual(recorded["ts"], 1500)
        self.assertEqual(recorded["duration"], 2773)

    def test_play_link_session_isolates_late_relay_requests(self):
        new_job = dict(_job(1420), key="new-relay", generation="1" * 16)
        old_job = dict(_job(1000), key="old-relay", generation="2" * 16)
        appmod._bind_ios_relay_play_context(
            "mg", new_job, _request(play_link="session-new"))
        appmod._bind_ios_relay_play_context(
            "mg", old_job, _request(play_link="session-old"))
        new_body = {"media_guid": "mg", "play_link": "session-new",
                    "ts": 80, "duration": 1353}
        mapped, allowed = appmod._ios_relay_history_body(new_body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped["ts"], 1500)
        self.assertEqual(mapped["duration"], 2773)
        old_mapped, allowed = appmod._ios_relay_history_body(
            {**new_body, "play_link": "session-old"}, _request())
        self.assertTrue(allowed)
        self.assertEqual(old_mapped["ts"], 1080)

        zero_job = dict(_job(0), key="zero-relay", generation="3" * 16)
        appmod._bind_ios_relay_play_context(
            "mg", zero_job, _request(play_link="session-zero"))
        mapped_again, allowed = appmod._ios_relay_history_body(new_body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped_again["ts"], 1500)
        self.assertEqual(mapped_again["duration"], 2773)

    def test_evicted_session_context_never_uses_new_media_context(self):
        old_job = dict(_job(1420), key="old-relay", generation="1" * 16)
        appmod._bind_ios_relay_play_context(
            "mg", old_job, _request(play_link="old-session"))
        full_job = dict(_job(0), key="full-relay", generation="2" * 16)
        appmod._bind_ios_relay_play_context(
            "mg", full_job, _request(play_link="new-session"))
        user = str(appmod.histmod.current_user() or "")
        appmod._RELAY_PLAY_CONTEXT.pop((user, "session:old-session"), None)
        body = {"media_guid": "mg", "play_link": "old-session",
                "ts": 80, "duration": 1355}
        mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertFalse(allowed)
        self.assertEqual(mapped, body)

    def test_t4_record_without_relay_context_keeps_original_history(self):
        body = {"media_guid": "t4-mg", "play_link": "t4-session",
                "ts": 123, "duration": 3600}
        target = {"url": "https://cdn.test/v.m3u8", "line": "t4::line",
                  "t4": {"id": "source"}}
        with patch.object(appmod, "_pick_episode", return_value=target):
            mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped, body)

    def test_t4_session_is_allowed_with_stale_media_context(self):
        old_job = dict(_job(1420), key="old-relay", generation="1" * 16)
        appmod._bind_ios_relay_play_context(
            "t4-mg", old_job, _request(play_link="old-relay-session"))
        body = {"media_guid": "t4-mg", "play_link": "new-t4-session",
                "ts": 123, "duration": 3600}
        target = {"url": "https://cdn.test/v.m3u8", "line": "t4::line",
                  "t4": {"id": "source"}}
        service = MagicMock()
        service.session_target.side_effect = lambda _mg, key: (
            dict(target) if key == "new-t4-session" else None)
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_get_t4_service", return_value=service):
            mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertTrue(allowed)
        self.assertEqual(mapped, body)

    def test_late_relay_record_rejected_when_current_target_is_t4(self):
        old_job = dict(_job(1420), key="old-relay", generation="1" * 16)
        appmod._bind_ios_relay_play_context(
            "t4-mg", old_job, _request(play_link="old-relay-session"))
        user = str(appmod.histmod.current_user() or "")
        appmod._RELAY_PLAY_CONTEXT.pop((user, "session:old-relay-session"), None)
        body = {"media_guid": "t4-mg", "play_link": "old-relay-session",
                "ts": 80, "duration": 1353}
        target = {"url": "https://cdn.test/v.m3u8", "line": "t4::line",
                  "t4": {"id": "source"}}
        service = MagicMock()
        service.session_target.return_value = None
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_get_t4_service", return_value=service):
            mapped, allowed = appmod._ios_relay_history_body(body, _request())
        self.assertFalse(allowed)
        self.assertEqual(mapped, body)

    async def test_missing_zero_based_context_never_corrupts_history(self):
        request = _request()
        target = {"url": "https://cdn.test/v.m3u8", "line": "youku::ukm3u8"}
        body = {"item_guid": "og", "media_guid": "mg", "ts": 80}
        with patch.object(appmod, "_pick_episode", return_value=target):
            _mapped, allowed = appmod._ios_relay_history_body(body, request)
            with patch.object(appmod.history, "record") as record:
                response = await appmod.play_record(_json_request(body))
        self.assertFalse(allowed)
        self.assertEqual(response.status_code, 200)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
