"""iOS fncache relay：单飞、生命周期与二次起播回归。"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod  # noqa: E402


class _Proc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9


def _target() -> dict:
    return {"url": "https://cdn.test/v.m3u8", "headers": {}, "line": "lzi::lzm3u8",
            "index": 0, "online_guid": "og", "m3u8": True, "_fixed_line": True}


def _request(query: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/v/media/online/mg/relay",
                    "query_string": query, "headers": []})


def _index_test_cluster(timecode: int, payload_size: int = 1, keyframe: bool = True) -> bytes:
    block = b"\x81\x00\x00" + (b"\x80" if keyframe else b"\x00") + b"\0" * payload_size
    payload = (appmod._ebml_encode_element(
        0xE7, appmod._ebml_encode_uint(timecode))
        + appmod._ebml_encode_element(0xA3, block))
    return appmod._ebml_encode_element(0x1F43B675, payload)


def _write_index_test_mkv(path: str, clusters: list[bytes]) -> None:
    info = appmod._ebml_encode_element(0x1549A966, appmod._ebml_encode_element(
        0x2AD7B1, appmod._ebml_encode_uint(1_000_000)))
    track_entry = (appmod._ebml_encode_element(0xD7, b"\x01")
                   + appmod._ebml_encode_element(0x83, b"\x01"))
    tracks = appmod._ebml_encode_element(0x1654AE6B, appmod._ebml_encode_element(0xAE, track_entry))
    reserved = appmod._ebml_encode_element(0xEC, b"\0" * 65536)
    payload = info + tracks + reserved + b"".join(clusters)
    unknown_segment = (appmod._ebml_encode_uint(0x18538067)
                       + b"\x01\xff\xff\xff\xff\xff\xff\xff")
    data = appmod._ebml_encode_element(0x1A45DFA3, b"") + unknown_segment + payload
    Path(path).write_bytes(data)


class IosRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fnmedia-ios-relay-test-")
        self.addCleanup(self.tmp.cleanup)
        self.dir_patch = patch.object(appmod, "_RELAY_DIR", self.tmp.name)
        self.dir_patch.start()
        self.addCleanup(self.dir_patch.stop)
        self.real_probe_meta = appmod._relay_probe_meta
        self.probe_patch = patch.object(appmod, "_relay_probe_meta", new=AsyncMock())
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        appmod._RELAY_JOBS.clear()
        self.addCleanup(appmod._RELAY_JOBS.clear)
        appmod._RELAY_RESUME_FALLBACK.clear()
        self.addCleanup(appmod._RELAY_RESUME_FALLBACK.clear)
        appmod._RELAY_REUSE_ALIAS.clear()
        self.addCleanup(appmod._RELAY_REUSE_ALIAS.clear)
        appmod._RELAY_RESERVED_GENERATION.clear()
        self.addCleanup(appmod._RELAY_RESERVED_GENERATION.clear)
        appmod._RELAY_PUBLISHED_TOTAL.clear()
        self.addCleanup(appmod._RELAY_PUBLISHED_TOTAL.clear)
        appmod._RELAY_PLAY_CONTEXT.clear()
        self.addCleanup(appmod._RELAY_PLAY_CONTEXT.clear)
        if hasattr(appmod, "_RELAY_LOCK"):
            appmod._RELAY_LOCK = asyncio.Lock()

    async def asyncTearDown(self):
        tasks = [j.get("index_task") for j in appmod._RELAY_JOBS.values()
                 if j.get("index_task") is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _serve(self, job: dict, range_header: str):
        request = _request()
        request.scope["headers"].append((b"range", range_header.encode()))
        with patch.object(appmod, "_pick_episode", return_value=_target()), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)):
            return await appmod.ios_relay("mg", request)

    def _job(self, data: bytes, *, returncode=None, size_est: int = 0) -> dict:
        fd, path = tempfile.mkstemp(prefix="range-", suffix=".mkv", dir=self.tmp.name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return {"proc": _Proc(returncode), "path": path, "ts": time.time(),
                "duration": 0, "size_est": size_est, "active": 0}

    async def test_index_worker_progresses_without_http_requests(self):
        fd, path = tempfile.mkstemp(prefix="worker-", suffix=".mkv", dir=self.tmp.name)
        os.close(fd)
        job = {"proc": _Proc(), "path": path, "duration": 0, "cues": []}
        calls = []

        async def refresh(j, force=False):
            calls.append(force)
            if not force:
                j["cues"].append((len(j["cues"]), 100 + len(j["cues"])))
                if len(j["cues"]) >= 3:
                    j["proc"].returncode = 0

        with patch.object(appmod, "_relay_refresh_index", new=refresh):
            await asyncio.wait_for(appmod._relay_index_worker(job), 2)
        self.assertEqual(len([x for x in calls if not x]), 3,
                         "没有任何Range/stream请求时worker也必须持续推进索引")
        self.assertTrue(calls[-1], "进程结束后必须进行一次最终索引交接")

    async def test_drop_cancels_index_worker(self):
        job = self._job(b"")
        job["key"] = "worker-drop"
        task = asyncio.create_task(asyncio.sleep(60))
        job["index_task"] = task
        appmod._RELAY_JOBS["worker-drop"] = job
        appmod._relay_drop("worker-drop")
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(job["proc"].killed)
        Path(job["path"]).write_bytes(b"new-job-file")
        before = Path(job["path"]).read_bytes()
        self.assertFalse(appmod._mkv_update_index(job),
                         "已淘汰worker不得写入同路径的新job文件")
        self.assertEqual(Path(job["path"]).read_bytes(), before)

    async def test_initial_media_range_waits_for_first_cues(self):
        job = self._job(b"\x1a\x45\xdf\xa3" + bytes(5000), size_est=10000)
        job["cues"] = []
        calls = 0

        async def refresh(j, force=False):
            nonlocal calls
            calls += 1
            if calls >= 2:
                j["cues"] = [(0, 100)]

        with patch.object(appmod, "_relay_refresh_index", new=refresh):
            response = await self._serve(job, "bytes=0-99")
        self.assertEqual(response.status_code, 206)
        self.assertGreaterEqual(calls, 2,
                                "首个正式响应不能抢在第一条Cue之前让客户端认定无索引")

    async def test_new_job_unlinks_stale_file_before_ffmpeg_can_open_it(self):
        target = _target()
        key = appmod._relay_key(target)
        path = os.path.join(self.tmp.name, f"{key}.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0, payload_size=5000)])
        self.assertGreater(os.path.getsize(path), 5000)
        calls = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            # 模拟真实启动窗口：进程已返回，但尚未打开/截断输出文件。
            return _Proc()

        with patch.object(appmod.asyncio, "create_subprocess_exec", new=spawn):
            jobs = await asyncio.gather(*(appmod._ensure_relay_job(target)
                                          for _ in range(3)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({id(j) for j in jobs}), 1)
        self.assertFalse(os.path.exists(path),
                         "新任务尚未产出时绝不能让首个Range读到重启前旧MKV")
        self.assertFalse(jobs[0]["index_task"].done(),
                         "真实relay job必须从创建起运行独立索引worker")

    async def test_concurrent_prewarm_spawns_one_ffmpeg_and_overwrites_stale_file(self):
        calls = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            await asyncio.sleep(0.02)  # 让三个调用都进入在途状态，复现登记前竞态。
            return _Proc()

        with patch.object(appmod.asyncio, "create_subprocess_exec", new=spawn):
            jobs = await asyncio.gather(*(appmod._ensure_relay_job(_target())
                                          for _ in range(3)))
        self.assertEqual(len(calls), 1, "同一集同一线路只能有一个 ffmpeg")
        self.assertEqual(len({id(j) for j in jobs}), 1)
        self.assertIn("-y", calls[0], "服务重启后必须覆盖同 key 的陈旧临时文件")

    async def test_active_job_is_not_killed_by_creation_ttl(self):
        proc = _Proc()
        path = os.path.join(self.tmp.name, "active.mkv")
        Path(path).write_bytes(b"mkv")
        target = _target()
        key = appmod._relay_key(target)
        job = {"proc": proc, "path": path, "ts": time.time() - appmod._RELAY_TTL - 1,
               "duration": 0, "size_est": 0, "active": 1}
        appmod._RELAY_JOBS[key] = job

        with patch.object(appmod.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            got = await appmod._ensure_relay_job(target)
        self.assertIs(got, job)
        self.assertFalse(proc.killed)
        spawn.assert_not_awaited()

    async def test_idle_running_job_is_not_restarted_by_ttl(self):
        proc = _Proc()
        target = _target()
        key = appmod._relay_key(target)
        job = self._job(b"mkv", returncode=None)
        job.update(proc=proc, ts=time.time() - appmod._RELAY_TTL - 1,
                   active=0)
        appmod._RELAY_JOBS[key] = job
        with patch.object(appmod.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            got = await appmod._ensure_relay_job(target)
        self.assertIs(got, job)
        self.assertFalse(proc.killed)
        spawn.assert_not_awaited()

    async def test_completed_file_survives_short_pause_ttl(self):
        proc = _Proc(returncode=0)
        target = _target()
        key = appmod._relay_key(target)
        job = self._job(b"mkv", returncode=0)
        job.update(proc=proc, ts=time.time() - appmod._RELAY_TTL - 1,
                   active=0)
        appmod._RELAY_JOBS[key] = job
        with patch.object(appmod.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            got = await appmod._ensure_relay_job(target)
        self.assertIs(got, job)
        self.assertFalse(proc.killed)
        spawn.assert_not_awaited()

    async def test_soft_capacity_does_not_kill_running_producers(self):
        old = []
        for name in ("a", "b", "c"):
            job = self._job(name.encode(), returncode=None)
            appmod._RELAY_JOBS[name] = job
            old.append(job)
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=_Proc())):
            await appmod._ensure_relay_job(_target())
        self.assertTrue(all(not job["proc"].killed for job in old))

    async def test_relay_honors_explicit_line_from_play_link(self):
        default = _target()
        selected = dict(default, line="baofeng::bfzym3u8",
                        url="https://cdn.test/selected.m3u8")
        proc = _Proc(returncode=0)
        path = os.path.join(self.tmp.name, "selected.mkv")
        Path(path).write_bytes(b"\x1a\x45\xdf\xa3")
        job = {"proc": proc, "path": path, "ts": time.time(),
               "duration": 0, "size_est": 0, "active": 0}
        query = urllib.parse.urlencode({
            "line": selected["line"], "index": 0, "auto": 1}).encode()
        request = _request(query)
        request.scope["headers"].append((b"range", b"bytes=0-1"))
        with patch.object(appmod, "_fixed_media_target", return_value=None), \
                patch.object(appmod.store, "resolve_media_guid", return_value=default), \
                patch.object(appmod.store, "resolve_episode", return_value=selected), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)) as ensure:
            response = await appmod.ios_relay("mg", request)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(ensure.await_args.args[0]["line"], selected["line"])
        self.assertEqual(ensure.await_args.args[0]["url"], selected["url"])

    async def test_range_total_never_underestimates_actual_file(self):
        response = await self._serve(self._job(b"x" * 100, size_est=50), "bytes=0-99")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.body), 100)
        self.assertEqual(response.headers["File-Size"], "100")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-99/100")

    async def test_open_ended_range_keeps_stable_estimate_and_actual_length(self):
        response = await self._serve(self._job(b"x" * 100, size_est=150), "bytes=0-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.body), 100)
        self.assertEqual(response.headers["Content-Length"], "100")
        self.assertEqual(response.headers["File-Size"], "150")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-99/150")

    async def test_open_ended_range_is_capped_to_bounded_memory_chunk(self):
        job = self._job(b"x" * 100, size_est=100)
        with patch.object(appmod, "_RELAY_RANGE_MAX", 10):
            response = await self._serve(job, "bytes=0-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.body), 10)
        self.assertEqual(response.headers["Content-Length"], "10")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-9/100")

    async def test_completed_file_beyond_eof_returns_416_not_empty_206(self):
        response = await self._serve(
            self._job(b"abc", returncode=0, size_est=3), "bytes=10-20")
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */3")

    async def test_running_file_seek_timeout_returns_retryable_error_not_empty_206(self):
        job = self._job(b"abc", returncode=None, size_est=100)
        with patch.object(appmod, "_relay_wait_size", new=AsyncMock(return_value=3)):
            response = await self._serve(job, "bytes=10-20")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertNotIn("Content-Range", response.headers)

    async def test_failed_empty_job_does_not_fake_ebml_success(self):
        response = await self._serve(
            self._job(b"", returncode=1, size_est=0), "bytes=0-1")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("Content-Range", response.headers)

    async def test_failed_job_is_replaced_instead_of_reused_for_ttl(self):
        target = _target()
        key = appmod._relay_key(target)
        failed = self._job(b"", returncode=1)
        appmod._RELAY_JOBS[key] = failed
        replacement = _Proc()
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=replacement)) as spawn:
            got = await appmod._ensure_relay_job(target)
        self.assertIsNot(got, failed)
        self.assertIs(got["proc"], replacement)
        spawn.assert_awaited_once()

    async def test_special_target_is_resolved_and_auth_headers_reach_ffmpeg(self):
        target = dict(_target(), url="https://source.test/share/1", resolve=True,
                      headers={"User-Agent": "SourceAgent", "Referer": "https://source.test/",
                               "Cookie": "session=ok"})

        async def resolve(picked, request):
            picked.update(url="https://cdn.test/real.m3u8", resolve=False, m3u8=True)
            return picked

        calls = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            return _Proc()

        with patch.object(appmod, "_resolve_if_needed", new=AsyncMock(side_effect=resolve)) as resolver, \
                patch.object(appmod.asyncio, "create_subprocess_exec", new=spawn):
            await appmod._ensure_relay_job(target, _request())
        resolver.assert_awaited_once()
        args = calls[0]
        self.assertIn("https://cdn.test/real.m3u8", args)
        self.assertNotIn("https://source.test/share/1", args)
        self.assertEqual(args[args.index("-user_agent") + 1], "SourceAgent")
        forwarded = args[args.index("-headers") + 1]
        self.assertIn("Referer: https://source.test/", forwarded)
        self.assertIn("Cookie: session=ok", forwarded)

    async def test_active_special_job_reuse_does_not_resolve_again(self):
        target = dict(_target(), url="https://source.test/share/1", resolve=True)
        key = appmod._relay_key(target)
        job = self._job(b"mkv", returncode=None)
        appmod._RELAY_JOBS[key] = job
        with patch.object(appmod, "_resolve_if_needed", new=AsyncMock()) as resolver, \
                patch.object(appmod.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            got = await appmod._ensure_relay_job(target, _request())
        self.assertIs(got, job)
        resolver.assert_not_awaited()
        spawn.assert_not_awaited()

    async def test_cached_playlist_duration_seeds_stable_range_estimate(self):
        with patch.object(appmod, "_relay_duration_fallback", return_value=7297), \
                patch.object(appmod.asyncio, "create_subprocess_exec",
                             new=AsyncMock(return_value=_Proc())):
            job = await appmod._ensure_relay_job(_target())
        self.assertEqual(job["duration"], 7297)
        self.assertEqual(job["duration_source"], "playlist")
        self.assertEqual(job["size_est"], int(7297 * 4_000_000 / 8))
        self.assertFalse(job["duration_header"])

    async def test_hls_probe_ignores_playlist_text_bit_rate(self):
        proc = _Proc(returncode=0)
        proc.communicate = AsyncMock(return_value=(json.dumps({"format": {
            "duration": "7297.083464", "bit_rate": "82"}}).encode(), b""))
        job = self._job(b"")
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=proc)):
            await self.real_probe_meta(job, _target())
        self.assertEqual(job["duration"], 7297)
        self.assertEqual(job["duration_source"], "ffprobe")
        self.assertEqual(job["size_est"], int(7297 * 4_000_000 / 8))
        Path(job["path"]).write_bytes(b"\x1a\x45\xdf\xa3")
        response = await self._serve(job, "bytes=0-1")
        expected_total = str(int(7297 * 4_000_000 / 8))
        self.assertEqual(response.headers["File-Size"], expected_total)
        self.assertEqual(response.headers["Content-Range"],
                         f"bytes 0-1/{expected_total}")

    async def test_resume_range_estimates_only_remaining_bytes(self):
        job = self._job(b"\x1a\x45\xdf\xa3")
        job.update(start=1200, requested_start=1200,
                   zero_based=True, timeline_start=1200.0)
        proc = _Proc(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"{}", b""))
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=proc)), \
                patch.object(appmod, "_relay_duration_fallback", return_value=3600):
            await self.real_probe_meta(job, _target())
        self.assertEqual(job["duration"], 3600)
        self.assertEqual(job["size_est"], 1_200_000_000)
        response = await self._serve(job, "bytes=0-1")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["File-Size"], "1200000000")
        self.assertEqual(response.headers["Content-Range"],
                         "bytes 0-1/1200000000")

    def test_mkv_duration_replaces_reserved_void_without_changing_layout(self):
        def element(eid, payload):
            self.assertLess(len(payload), 127)
            return eid + bytes([0x80 | len(payload)]) + payload

        void = b"\xec\x01" + (2).to_bytes(7, "big") + b"\x00\x00"
        scale = element(b"\x2a\xd7\xb1", (1_000_000).to_bytes(3, "big"))
        info = element(b"\x15\x49\xa9\x66", scale + void)
        data = (element(b"\x1a\x45\xdf\xa3", b"") + b"\x18\x53\x80\x67\xff"
                + info + b"\x1f\x43\xb6\x75")
        path = os.path.join(self.tmp.name, "duration.mkv")
        Path(path).write_bytes(data)
        self.assertTrue(appmod._mkv_write_duration(path, 7297.083464))
        patched = Path(path).read_bytes()
        self.assertEqual(len(patched), len(data))
        at = patched.index(b"\x44\x89\x88")
        ticks = struct.unpack(">d", patched[at + 3:at + 11])[0]
        self.assertAlmostEqual(ticks / 1000, 7297.083464)
        self.assertEqual(patched[at + 11:], data[at + 11:])

    async def test_active_range_wait_prepares_duration_header(self):
        job = self._job(b"x" * 4096)
        job["duration"] = 7297
        with patch.object(appmod, "_mkv_write_duration", return_value=True) as write:
            size = await appmod._relay_wait_size(job, 1, 0.1)
        self.assertEqual(size, 4096)
        self.assertTrue(job["duration_header"])
        write.assert_called_once_with(job["path"], 7297)

    async def test_ffprobe_timeout_kills_process(self):
        proc = _Proc()
        job = self._job(b"")
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=proc)), \
                patch.object(appmod.asyncio, "wait_for",
                             new=AsyncMock(side_effect=asyncio.TimeoutError())):
            await self.real_probe_meta(job, _target())
        self.assertTrue(proc.killed)

    def test_plain_cms_keeps_no_referer_compatibility(self):
        target = dict(_target(), headers={"User-Agent": "SourceAgent",
                                          "Referer": "https://cdn.test/",
                                          "Cookie": "session=ok"})
        args = appmod._relay_input_args(target)
        self.assertEqual(args[args.index("-user_agent") + 1], "SourceAgent")
        forwarded = args[args.index("-headers") + 1]
        self.assertNotIn("Referer", forwarded)
        self.assertIn("Cookie: session=ok", forwarded)

    async def test_non_range_stream_holds_job_until_response_closes(self):
        proc = _Proc(returncode=0)
        path = os.path.join(self.tmp.name, "stream.mkv")
        Path(path).write_bytes(b"mkv")
        job = {"proc": proc, "path": path, "ts": time.time(),
               "duration": 0, "size_est": 0, "active": 0}
        with patch.object(appmod, "_fixed_media_target", return_value=_target()), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)):
            response = await appmod.ios_relay("mg", _request())
            self.assertEqual(job.get("active"), 1)
            self.assertEqual(response.headers["File-Size"], "3")
            chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(chunks, [b"mkv"])
        self.assertEqual(job.get("active"), 0)

    def test_resume_lookup_uses_current_episode_and_explicit_zero(self):
        rec = {"ts": 1326, "duration": 2700}
        eg = appmod.episode_guid("og", "", 0)
        request = _request()
        request.scope["headers"].append((b"user-agent", b"com.trim.media.ios"))
        with patch.object(appmod.history, "get",
                          side_effect=lambda guid: rec if guid == eg else None), \
                patch.object(appmod.history, "latest_episode_record", return_value=None), \
                patch.object(appmod, "_relay_duration_fallback", return_value=2700):
            self.assertEqual(appmod._ios_resume_seconds(_target(), request), 1326)
            self.assertEqual(appmod._ios_resume_seconds(
                _target(), _request(b"start=0")), 0)
            self.assertEqual(appmod._ios_resume_seconds(
                _target(), _request(b"start=-3")), 0)
            self.assertEqual(appmod._ios_resume_seconds(
                _target(), _request(b"start=bad")), 0)

    async def test_resume_job_seeks_and_is_isolated_from_full_job(self):
        commands = []

        async def spawn(*args, **kwargs):
            commands.append(args)
            return _Proc()

        resumed = dict(_target(), _relay_start=1326)
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn), \
                patch.object(appmod, "_relay_duration_fallback", return_value=2700):
            job = await appmod._ensure_relay_job(resumed)
            full = await appmod._ensure_relay_job(_target())
        self.assertEqual(len(commands), 2)
        self.assertNotEqual(job["path"], full["path"])
        self.assertEqual(job["start"], 1326)
        self.assertEqual(full["start"], 0)
        self.assertIn("-ss", commands[0])
        self.assertEqual(commands[0][commands[0].index("-ss") + 1], "1326")
        self.assertLess(commands[0].index("-ss"), commands[0].index("-i"))
        self.assertNotIn("-output_ts_offset", commands[0])
        self.assertIn("-avoid_negative_ts", commands[0])
        self.assertTrue(job["zero_based"])
        self.assertNotIn("-ss", commands[1])
        self.assertNotIn("-output_ts_offset", commands[1])
        self.assertEqual(job["size_est"], int((2700 - 1326) * 4_000_000 / 8))
        self.assertEqual(full["size_est"], int(2700 * 4_000_000 / 8))

    async def test_failed_resume_job_falls_back_to_full_job(self):
        commands = []
        procs = []

        async def spawn(*args, **kwargs):
            commands.append(args)
            proc = _Proc()
            procs.append(proc)
            return proc

        resumed = dict(_target(), _relay_start=1326)
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn):
            first = await appmod._ensure_relay_job(resumed)
            procs[0].returncode = 1
            second = await appmod._ensure_relay_job(resumed)
            third = await appmod._ensure_relay_job(resumed)
            fourth = await appmod._ensure_relay_job(resumed)
        self.assertEqual(first["start"], 1326)
        self.assertEqual(second["start"], 0)
        self.assertIs(third, second)
        self.assertIs(fourth, second)
        self.assertEqual(len(commands), 2)
        self.assertEqual(len(procs), 2)
        self.assertNotEqual(first["path"], second["path"])
        self.assertIn("-ss", commands[0])
        self.assertNotIn("-ss", commands[1])

    async def test_partial_suffix_failure_never_switches_url_to_full(self):
        commands = []
        procs = []

        async def spawn(*args, **kwargs):
            commands.append(args)
            proc = _Proc()
            procs.append(proc)
            return proc

        resumed = dict(_target(), _relay_start=1326)
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn):
            first = await appmod._ensure_relay_job(resumed)
            Path(first["path"]).write_bytes(b"abcd")
            first["delivered"] = 4
            procs[0].returncode = 1
            second = await appmod._ensure_relay_job(resumed)
            with patch.object(appmod, "_pick_episode", return_value=resumed):
                query = f"start=1326&g={first['generation']}".encode()
                readable = _request(query)
                readable.scope["headers"].append((b"range", b"bytes=0-1"))
                partial = await appmod.ios_relay("mg", readable)
                request = _request(query)
                request.scope["headers"].append((b"range", b"bytes=10-20"))
                response = await appmod.ios_relay("mg", request)
        self.assertIs(second, first)
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.body, b"ab")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(commands), 1)
        self.assertFalse(appmod._RELAY_RESUME_FALLBACK)

    async def test_empty_successful_suffix_falls_back_once_to_full(self):
        target = _target()
        resume_key = appmod._relay_key(dict(target, _relay_start=1326))
        full_key = appmod._relay_key(target)
        empty = self._job(b"", returncode=0)
        empty.update({"start": 1326, "key": resume_key, "delivered": 0})
        full = self._job(b"\x1a\x45\xdf\xa3", returncode=0, size_est=4)
        full.update({"start": 0, "key": full_key, "delivered": 0})
        appmod._RELAY_JOBS[resume_key] = empty
        request = _request(b"start=1326")
        request.scope["headers"].append((b"range", b"bytes=0-1"))
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job",
                             new=AsyncMock(side_effect=[empty, full])) as ensure:
            response = await appmod.ios_relay("mg", request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ensure.await_count, 2)
        self.assertIn(resume_key, appmod._RELAY_RESUME_FALLBACK)
        self.assertEqual(full["delivered"], 0)
        appmod._RELAY_JOBS[full_key] = full
        location = urllib.parse.urlsplit(response.headers["location"])
        follow = _request(location.query.encode())
        follow.scope["headers"].append((b"range", b"bytes=0-1"))
        with patch.object(appmod, "_pick_episode", return_value=target):
            final = await appmod.ios_relay("mg", follow)
        self.assertEqual(final.status_code, 206)
        self.assertEqual(final.body, b"\x1a\x45")
        self.assertEqual(full["delivered"], 2)

    async def test_metadata_size_matches_resume_job_and_full_fallback(self):
        target = _target()  # 真实target通常不带_relay_start，续播点仅在query中。
        brief = {"title": "demo", "runtime": 60}
        resume_key = appmod._relay_key(dict(target, _relay_start=1200))
        resumed = self._job(b"abc", size_est=1_200_000_000)
        resumed.update({"start": 1200, "duration": 3600,
                        "duration_source": "ffprobe"})
        appmod._RELAY_JOBS[resume_key] = resumed
        sl = {"files": [{"path": "在线/demo.m3u8", "size": 0}],
              "video_streams": [{"duration": 0}]}
        appmod._apply_ios_relay_metadata(sl, brief, target, _request(b"start=1200"))
        self.assertEqual(sl["video_streams"][0]["duration"], 3600)
        self.assertEqual(sl["files"][0]["size"], 1_200_000_000)

        appmod._RELAY_JOBS.pop(resume_key)
        full_key = appmod._relay_key(dict(target, _relay_start=0))
        full = self._job(b"abc", size_est=1_800_000_000)
        full.update({"start": 0, "duration": 3600, "duration_source": "ffprobe"})
        appmod._RELAY_JOBS[full_key] = full
        appmod._RELAY_RESUME_FALLBACK[resume_key] = full_key
        sl = {"files": [{"path": "在线/demo.m3u8", "size": 0}],
              "video_streams": [{"duration": 0}]}
        appmod._apply_ios_relay_metadata(sl, brief, target, _request(b"start=1200"))
        self.assertEqual(sl["files"][0]["size"], 1_800_000_000)

    async def test_query_index_change_rebinds_episode_identity(self):
        base = dict(_target(), kind="title", ep_guid="episode-1", index=0)
        selected = dict(base, index=1, ep_guid="")
        with patch.object(appmod.store, "resolve_media_guid", return_value=base), \
                patch.object(appmod.store, "resolve_episode", return_value=selected), \
                patch.object(appmod.store, "episode_guid_at", return_value="episode-2"):
            picked = appmod._pick_episode("mg", _request(b"index=1"))
        self.assertEqual(picked["index"], 1)
        self.assertEqual(picked["ep_guid"], "episode-2")
        self.assertEqual(picked["kind"], "episode")

        def _get_history(guid):
            return {"ts": 900, "duration": 2000} if guid == "og" else None

        with patch.object(appmod.history, "get", side_effect=_get_history), \
                patch.object(appmod.history, "latest_episode_record", return_value=None), \
                patch.object(appmod, "_relay_duration_fallback", return_value=0):
            # 换集后绝不能用标题级og记录冒充当前集续播点。
            self.assertEqual(appmod._ios_resume_seconds(picked, _request()), 0)

    async def test_redirect_episode_never_borrows_title_history(self):
        appmod._MEDIA_REDIRECT["mg"] = "new-og"
        self.addCleanup(appmod._MEDIA_REDIRECT.pop, "mg", None)
        selected = dict(_target(), online_guid="new-og", kind="episode",
                        ep_guid="", index=1)
        with patch.object(appmod.store, "resolve_episode", return_value=selected), \
                patch.object(appmod.store, "episode_guid_at",
                             return_value="new-episode-1"):
            picked = appmod._pick_episode("mg", _request(b"index=1"))
        self.assertEqual(picked["kind"], "episode")
        self.assertEqual(picked["ep_guid"], "new-episode-1")

        def _get_history(guid):
            return {"ts": 900, "duration": 2000} if guid == "new-og" else None

        with patch.object(appmod.history, "get", side_effect=_get_history), \
                patch.object(appmod.history, "latest_episode_record", return_value=None), \
                patch.object(appmod, "_relay_duration_fallback", return_value=0):
            self.assertEqual(appmod._ios_resume_seconds(picked, _request()), 0)

    async def test_header_only_successful_suffix_falls_back_once_to_full(self):
        header_only = (b"\x1a\x45\xdf\xa3\x80"
                       + b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff"
                       + b"\x15\x49\xa9\x66\x80" + b"\x16\x54\xae\x6b\x80")
        clustered = header_only + b"\x1f\x43\xb6\x75\x80"
        target = _target()
        resume_key = appmod._relay_key(dict(target, _relay_start=1326))
        full_key = appmod._relay_key(target)
        empty = self._job(header_only, returncode=0, size_est=len(header_only))
        empty.update({"start": 1326, "key": resume_key, "delivered": 2,
                      "compatible_delivered": 2, "has_media": None})
        full = self._job(clustered, returncode=0, size_est=len(clustered))
        full.update({"start": 0, "key": full_key, "delivered": 0})
        appmod._RELAY_JOBS[resume_key] = empty
        self.assertFalse(appmod._mkv_has_media_cluster(empty["path"]))
        self.assertTrue(appmod._mkv_has_media_cluster(full["path"]))
        request = _request(b"start=1326")
        request.scope["headers"].append((b"range", b"bytes=0-1"))
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job",
                             new=AsyncMock(side_effect=[empty, full])) as ensure:
            response = await appmod.ios_relay("mg", request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ensure.await_count, 2)
        self.assertEqual(appmod._RELAY_RESUME_FALLBACK[resume_key], full_key)
        appmod._RELAY_JOBS[full_key] = full
        location = urllib.parse.urlsplit(response.headers["location"])
        follow = _request(location.query.encode())
        follow.scope["headers"].append((b"range", b"bytes=0-1"))
        with patch.object(appmod, "_pick_episode", return_value=target):
            final = await appmod.ios_relay("mg", follow)
        self.assertEqual(final.status_code, 206)
        self.assertEqual(final.body, b"\x1a\x45")

    async def test_media_cluster_detection_crosses_reserved_index_space(self):
        header_only = (b"\x1a\x45\xdf\xa3\x80"
                       + b"\x18\x53\x80\x67\x01" + b"\xff" * 7)
        reserved = appmod._ebml_encode_element(
            0xEC, b"\x00" * (1024 * 1024 + 2048))
        clustered = header_only + reserved + b"\x1f\x43\xb6\x75\x80"
        job = self._job(clustered, returncode=0, size_est=len(clustered))
        self.assertGreater(clustered.find(b"\x1f\x43\xb6\x75"), 1024 * 1024)
        self.assertTrue(appmod._mkv_has_media_cluster(job["path"]))
        response = await self._serve(job, f"bytes={len(clustered) - 1}-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"],
                         f"bytes {len(clustered) - 1}-{len(clustered) - 1}/{len(clustered)}")

    async def test_known_segment_size_expands_before_trailing_void(self):
        path = os.path.join(self.tmp.name, "known-segment.mkv")
        cluster = _index_test_cluster(0)
        segment = appmod._ebml_encode_element(0x18538067, cluster, size_width=8)
        Path(path).write_bytes(appmod._ebml_encode_element(0x1A45DFA3, b"") + segment)
        original = os.path.getsize(path)
        total = original + 1024
        key = appmod._relay_key(dict(_target(), _relay_start=0))
        job = dict(self._job(b"", returncode=0, size_est=total),
                   path=path, key=key, start=0, published_total=total)
        self.assertTrue(appmod._relay_finalize_file_size(job))
        self.assertEqual(os.path.getsize(path), total)
        with open(path, "rb") as src:
            ebml = appmod._ebml_file_next(src, 0, os.path.getsize(path))
            declared = appmod._ebml_file_next(src, ebml[3], os.path.getsize(path))
        self.assertEqual(declared[0], 0x18538067)
        self.assertEqual(declared[3], total)  # 声明边界必须覆盖尾部Void，而非beyond end of segment
        self.assertTrue(appmod._mkv_has_media_cluster(path))
        # 小尺寸VINT无法容纳扩展后的Segment时必须安全拒绝，不能覆盖媒体首字节。
        narrow = os.path.join(self.tmp.name, "narrow-segment.mkv")
        narrow_segment = appmod._ebml_encode_element(0x18538067, cluster)
        narrow_bytes = appmod._ebml_encode_element(0x1A45DFA3, b"") + narrow_segment
        Path(narrow).write_bytes(narrow_bytes)
        rejected = dict(job, path=narrow, key="narrow", size_finalized=False,
                        published_total=len(narrow_bytes) + 1024)
        self.assertFalse(appmod._relay_finalize_file_size(rejected))
        self.assertEqual(Path(narrow).read_bytes(), narrow_bytes)

    async def test_completed_file_pads_to_published_total_for_ranges(self):
        path = os.path.join(self.tmp.name, "published-total.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0)])
        original = os.path.getsize(path)
        total = original + 1024
        key = appmod._relay_key(dict(_target(), _relay_start=0))
        job = dict(self._job(b"", returncode=0, size_est=total),
                   path=path, key=key, start=0, published_total=total)
        start = original + 17
        response = await self._serve(job, f"bytes={start}-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(os.path.getsize(path), total)
        self.assertEqual(response.headers["Content-Range"],
                         f"bytes {start}-{total - 1}/{total}")
        self.assertEqual(response.headers["File-Size"], str(total))
        self.assertEqual(len(response.body), total - start)
        self.assertTrue(job.get("size_finalized"))
        self.assertTrue(appmod._mkv_has_media_cluster(path))
        before = Path(path).read_bytes()
        again = await self._serve(job, f"bytes={start}-")
        self.assertEqual(again.status_code, 206)
        self.assertEqual(Path(path).read_bytes(), before)
        # 已淘汰的旧worker即使持有相同path，也不能补写同key的新文件。
        replacement = os.path.join(self.tmp.name, "replacement.mkv")
        _write_index_test_mkv(replacement, [_index_test_cluster(0)])
        Path(path).write_bytes(Path(replacement).read_bytes())
        new_bytes = Path(path).read_bytes()
        job["index_task"] = object()
        job["size_finalized"] = False
        appmod._RELAY_JOBS[key] = {"index_task": None}
        self.assertFalse(appmod._relay_finalize_file_size(job))
        self.assertEqual(Path(path).read_bytes(), new_bytes)

    async def test_single_byte_gap_rounds_up_to_two_byte_void(self):
        path = os.path.join(self.tmp.name, "single-byte-gap.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0)])
        original = os.path.getsize(path)
        total = original + 2  # 单字节Void头不存在，向上1字节后写入合法的 ec 80。
        key = appmod._relay_key(dict(_target(), _relay_start=0))
        job = dict(self._job(b"", returncode=0, size_est=original + 1),
                   path=path, key=key, start=0, published_total=original + 1)
        self.assertTrue(appmod._relay_finalize_file_size(job))
        self.assertEqual(job.get("published_total"), total)
        self.assertEqual(os.path.getsize(path), total)
        self.assertEqual(Path(path).read_bytes()[-2:], b"\xec\x80")
        response = await self._serve(job, f"bytes={original}-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, b"\xec\x80")
        self.assertEqual(response.headers["Content-Range"],
                         f"bytes {original}-{total - 1}/{total}")
        suffix = await self._serve(job, "bytes=-1")
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.body, b"\x80")
        self.assertEqual(suffix.headers["Content-Range"],
                         f"bytes {total - 1}-{total - 1}/{total}")
        before = Path(path).read_bytes()
        self.assertTrue(appmod._relay_finalize_file_size(job))
        self.assertEqual(Path(path).read_bytes(), before)

    async def test_full_stream_finalizes_size_when_process_ends_between_reads(self):
        for gap in (1024, 1):
            with self.subTest(gap=gap):
                path = os.path.join(self.tmp.name, f"stream-eof-{gap}.mkv")
                _write_index_test_mkv(path, [_index_test_cluster(0)])
                original = os.path.getsize(path)
                total = original + gap
                target = dict(_target(), online_guid=f"stream-eof-{gap}")
                key = appmod._relay_key(dict(target, _relay_start=0))
                pending = asyncio.create_task(asyncio.sleep(60))
                job = dict(self._job(b"", size_est=total), path=path, key=key,
                           start=0, published_total=total, has_media=None,
                           index_task=pending)
                appmod._RELAY_JOBS[key] = job
                with patch.object(appmod, "_pick_episode", return_value=target), \
                        patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)):
                    response = await appmod.ios_relay("mg", _request())
                iterator = response.body_iterator.__aiter__()
                first = await iterator.__anext__()
                self.assertIsNone(job["proc"].returncode)
                job["proc"].returncode = 0  # 完成发生在首块与下一次EOF read之间
                rest = b"".join([chunk async for chunk in iterator])
                sent = first + rest
                expected_total = original + (2 if gap == 1 else gap)
                self.assertEqual(len(sent), expected_total)
                self.assertTrue(job.get("size_finalized"))
                self.assertEqual(os.path.getsize(path), expected_total)

    async def test_full_stream_binds_inode_before_first_iteration(self):
        path = os.path.join(self.tmp.name, "pre-first-evicted.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0)])
        original = Path(path).read_bytes()
        target = dict(_target(), online_guid="pre-first-evicted")
        key = appmod._relay_key(dict(target, _relay_start=0))
        pending = asyncio.create_task(asyncio.sleep(60))
        self.addCleanup(pending.cancel)
        job = dict(self._job(b"", size_est=len(original) + 1024), path=path, key=key,
                   start=0, published_total=len(original) + 1024, has_media=None,
                   index_task=pending)
        appmod._RELAY_JOBS[key] = job
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)):
            response = await appmod.ios_relay("mg", _request())
        # 响应已创建但body尚未首次迭代：此后淘汰/换inode也必须只读旧fd。
        appmod._relay_drop(key)
        replacement = b"NEW-INODE" * 20000
        Path(path).write_bytes(replacement)
        job["proc"].returncode = 0
        sent = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(sent, original)
        self.assertNotIn(b"NEW-INODE", sent)
        self.assertEqual(Path(path).read_bytes(), replacement)

    async def test_evicted_stream_uses_old_fd_and_never_new_path(self):
        path = os.path.join(self.tmp.name, "evicted-stream.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0)])
        original = os.path.getsize(path)
        target = dict(_target(), online_guid="evicted-stream")
        key = appmod._relay_key(dict(target, _relay_start=0))
        pending = asyncio.create_task(asyncio.sleep(60))
        job = dict(self._job(b"", size_est=original + 1024), path=path, key=key,
                   start=0, published_total=original + 1024, has_media=None,
                   index_task=pending)
        appmod._RELAY_JOBS[key] = job
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=job)):
            response = await appmod.ios_relay("mg", _request())
        iterator = response.body_iterator.__aiter__()
        first = await iterator.__anext__()
        job["proc"].returncode = 0
        appmod._relay_drop(key)
        replacement = b"NEW-INODE" * 20000
        Path(path).write_bytes(replacement)
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk)
        sent = len(first) + sum(map(len, chunks))
        self.assertEqual(sent, original)
        self.assertEqual(Path(path).read_bytes(), replacement)

    async def test_range_rechecks_job_generation_after_finalization(self):
        path = os.path.join(self.tmp.name, "stale-range.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(0)])
        target = dict(_target(), online_guid="stale-range")
        key = appmod._relay_key(dict(target, _relay_start=0))
        pending = asyncio.create_task(asyncio.sleep(60))
        self.addCleanup(pending.cancel)
        old_job = dict(self._job(b"", returncode=0, size_est=1024), path=path,
                       key=key, start=0, index_task=pending)
        appmod._RELAY_JOBS[key] = old_job
        replacement = b"NEW-INODE" * 1000

        def evict_and_replace(_job):
            # 模拟to_thread等待期间完成drop并在同path启动新producer。
            appmod._RELAY_JOBS.pop(key, None)
            os.unlink(path)
            Path(path).write_bytes(replacement)
            appmod._RELAY_JOBS[key] = {
                "path": path, "proc": _Proc(), "index_task": None,
            }
            return False

        request = _request()
        request.scope["headers"].append((b"range", b"bytes=-1"))
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job", new=AsyncMock(return_value=old_job)), \
                patch.object(appmod, "_relay_finalize_file_size", side_effect=evict_and_replace):
            response = await appmod.ios_relay("mg", request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertNotEqual(response.body, replacement[-1:])
        self.assertEqual(Path(path).read_bytes(), replacement)

    async def test_full_fallback_survives_idle_file_expiry(self):
        commands = []
        procs = []

        async def spawn(*args, **kwargs):
            commands.append(args)
            proc = _Proc()
            procs.append(proc)
            return proc

        resumed = dict(_target(), _relay_start=1326)
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn):
            await appmod._ensure_relay_job(resumed)
            procs[0].returncode = 1
            full = await appmod._ensure_relay_job(resumed)
            self.assertEqual(full["start"], 0)
            # full文件即使因空闲被清理，URL到full的映射也不能随之过期。
            full["ts"] = time.time() - getattr(appmod, "_RELAY_FILE_TTL", appmod._RELAY_TTL) - 1
            procs[1].returncode = 0
            rebuilt = await appmod._ensure_relay_job(resumed)
            again = await appmod._ensure_relay_job(resumed)
        self.assertIsNot(rebuilt, full)
        self.assertIs(again, rebuilt)
        self.assertEqual(rebuilt["start"], 0)
        self.assertEqual(len(commands), 3)
        self.assertIn("-ss", commands[0])
        self.assertNotIn("-ss", commands[1])
        self.assertNotIn("-ss", commands[2])

    async def test_completed_suffix_range_and_growing_suffix_contract(self):
        job = self._job(b"abcdef", returncode=0, size_est=6)
        response = await self._serve(job, "bytes=-2")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, b"ef")
        self.assertEqual(response.headers["Content-Range"], "bytes 4-5/6")
        growing = self._job(b"abcdef", size_est=6)
        response = await self._serve(growing, "bytes=-2")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")

    async def test_range_total_does_not_shrink_after_completion_or_rebuild(self):
        key = appmod._relay_key(_target())
        job = self._job(b"x" * 100, size_est=150)
        job["key"] = key
        running = await self._serve(job, "bytes=0-1")
        self.assertEqual(running.headers["Content-Range"], "bytes 0-1/150")
        job["proc"].returncode = 0
        completed = await self._serve(job, "bytes=2-3")
        self.assertEqual(completed.headers["Content-Range"], "bytes 2-3/150")
        beyond = await self._serve(job, "bytes=120-121")
        self.assertEqual(beyond.status_code, 416)
        self.assertEqual(beyond.headers["Content-Range"], "bytes */150")

        # 模拟同URL空闲TTL后的新job：即便新估值变小，也只能沿用已公布总长。
        rebuilt = self._job(b"x" * 100, returncode=0, size_est=50)
        rebuilt["key"] = key
        response = await self._serve(rebuilt, "bytes=4-5")
        self.assertEqual(response.headers["Content-Range"], "bytes 4-5/150")
        self.assertEqual(rebuilt["size_est"], 150)

    async def test_history_duration_rejects_only_position_beyond_end(self):
        target = dict(_target(), kind="episode", ep_guid="episode-1")
        with patch.object(appmod.history, "get",
                          return_value={"ts": 1450, "duration": 1500}), \
                patch.object(appmod, "_relay_duration_fallback", return_value=0):
            self.assertEqual(appmod._ios_resume_seconds(target, _request()), 1450)
        with patch.object(appmod.history, "get",
                          return_value={"ts": 1501, "duration": 1500}), \
                patch.object(appmod, "_relay_duration_fallback", return_value=0):
            self.assertEqual(appmod._ios_resume_seconds(target, _request()), 0)

    async def test_near_end_resume_is_stable_before_and_after_playlist_cache(self):
        target = dict(_target(), kind="episode", ep_guid="episode-1")
        with patch.object(appmod.history, "get",
                          return_value={"ts": 2779, "duration": 0}):
            with patch.object(appmod, "_relay_duration_fallback", return_value=0):
                self.assertEqual(appmod._ios_resume_seconds(target, _request()), 2779)
            # 真实清单 2779.916 秒经整数兜底为 2779；不能因为达到 95% 改回 0。
            with patch.object(appmod, "_relay_duration_fallback", return_value=2779):
                self.assertEqual(appmod._ios_resume_seconds(target, _request()), 2779)

    async def test_resume_reuses_indexed_coverage_and_keeps_persistent_alias(self):
        base = _target()
        earlier_target = dict(base, _relay_start=3)
        earlier_key = appmod._relay_key(earlier_target)
        earlier = self._job(b"mkv", returncode=None)
        earlier.update(key=earlier_key, base_key=appmod._relay_key(base),
                       start=3, media_end=300.0, cues=[(1, 40, 100, 1)],
                       duration=600)
        appmod._RELAY_JOBS[earlier_key] = earlier
        query = urllib.parse.urlencode(
            {"line": base["line"], "index": 0, "auto": 1, "start": 222}).encode()
        request = _request(query)
        request.scope["headers"].append((
            b"user-agent",
            b"1.12.2 (com.trim.media.ios; build:1122011; iOS 27.2) Flutter/3.11.5"))
        requested = dict(base, _relay_start=222)
        requested_key = appmod._relay_key(requested)
        with patch.object(appmod.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            selected = await appmod._ensure_relay_job(requested, request)
        self.assertIs(selected, earlier)
        spawn.assert_not_awaited()
        self.assertEqual(appmod._RELAY_REUSE_ALIAS[requested_key], (earlier_key, 3))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(
            appmod._stream_play_link(base, "mg", request)).query)["start"], ["3"])

        # 即使原job后来被清理，旧start=222 URL也只能按alias的start=3重建。
        appmod._RELAY_JOBS.clear()
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=_Proc())) as spawn:
            rebuilt = await appmod._ensure_relay_job(requested, request)
        self.assertEqual(rebuilt["start"], 3)
        cmd = spawn.await_args.args
        self.assertEqual(cmd[cmd.index("-ss") + 1], "3")
        self.assertNotIn("-output_ts_offset", cmd)
        self.assertTrue(rebuilt["zero_based"])

    async def test_resume_beyond_indexed_coverage_starts_its_own_job(self):
        base = _target()
        earlier = self._job(b"mkv", returncode=None)
        earlier.update(key=appmod._relay_key(dict(base, _relay_start=3)),
                       base_key=appmod._relay_key(base), start=3,
                       media_end=100.0, cues=[(1, 40, 100, 1)], duration=600)
        appmod._RELAY_JOBS[earlier["key"]] = earlier
        requested = dict(base, _relay_start=222)
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(return_value=_Proc())) as spawn:
            selected = await appmod._ensure_relay_job(requested)
        self.assertIsNot(selected, earlier)
        self.assertEqual(selected["start"], 222)
        spawn.assert_awaited_once()

    async def test_completed_ffmpeg_index_is_left_untouched(self):
        path = os.path.join(self.tmp.name, "finalized.mkv")
        Path(path).write_bytes(b"\x00" * 74)
        original = Path(path).read_bytes()
        job = {"proc": _Proc(returncode=0), "path": path,
               "index_start": 10, "index_end": 74, "cues": [(1, 40, 100, 1)]}
        await appmod._relay_refresh_index(job, force=True)
        self.assertEqual(Path(path).read_bytes(), original)

    def test_non_keyframe_cluster_is_not_added_to_cues(self):
        path = os.path.join(self.tmp.name, "non-keyframe.mkv")
        _write_index_test_mkv(path, [
            _index_test_cluster(40, keyframe=False), _index_test_cluster(80)])
        proc = _Proc()
        proc.pid = 12345
        job = {"proc": proc, "path": path, "cues": [], "index_scan": 0,
               "media_end": 0.0, "index_full": False}
        with patch.object(appmod, "_wait_process_stopped", return_value=True), \
                patch.object(appmod.os, "kill"):
            self.assertTrue(appmod._mkv_update_index(job))
        self.assertEqual(job["cues"], [(80, job["cues"][0][1])])

    def test_large_cluster_is_indexed_without_payload_read_limit(self):
        path = os.path.join(self.tmp.name, "large-cluster.mkv")
        first = _index_test_cluster(40, 9 * 1024 * 1024)
        second = _index_test_cluster(80)
        _write_index_test_mkv(path, [first, second])
        proc = _Proc()
        proc.pid = 12345
        job = {"proc": proc, "path": path, "cues": [], "index_scan": 0,
               "media_end": 0.0, "index_full": False}
        with patch.object(appmod, "_wait_process_stopped", return_value=True), \
                patch.object(appmod.os, "kill") as pause:
            self.assertTrue(appmod._mkv_update_index(job))
        self.assertEqual([cue[0] for cue in job["cues"]], [40, 80])
        self.assertGreater(job["index_scan"], 9 * 1024 * 1024)
        self.assertEqual(pause.call_count, 2)  # SIGSTOP + SIGCONT
        with open(path, "ab") as out:
            out.write(_index_test_cluster(120))
        with patch.object(appmod, "_wait_process_stopped", return_value=True), \
                patch.object(appmod.os, "kill"):
            self.assertTrue(appmod._mkv_update_index(job))
        self.assertEqual([cue[0] for cue in job["cues"]], [40, 80, 120])

    def test_late_ffmpeg_final_cues_are_never_overwritten(self):
        path = os.path.join(self.tmp.name, "late-final.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(40), _index_test_cluster(80)])
        proc = _Proc()  # 模拟asyncio尚未发布returncode，缓存值一直为None。
        proc.pid = 12345
        job = {"proc": proc, "path": path, "cues": [], "index_scan": 0,
               "media_end": 0.0, "index_full": False}
        final = b"\x1c\x53\xbb\x6bFINAL-CUES"

        def ffmpeg_finished_before_write(_pid, sig):
            if sig == appmod.signal.SIGSTOP:
                with open(path, "r+b", buffering=0) as out:
                    out.seek(job["index_start"])
                    out.write(final)

        with patch.object(appmod, "_wait_process_stopped", return_value=True), \
                patch.object(appmod.os, "kill", side_effect=ffmpeg_finished_before_write) as pause:
            self.assertFalse(appmod._mkv_update_index(job))
        with open(path, "rb") as src:
            src.seek(job["index_start"])
            self.assertEqual(src.read(len(final)), final)
        self.assertNotIn("index_written", job)
        self.assertEqual([call.args[1] for call in pause.call_args_list],
                         [appmod.signal.SIGSTOP, appmod.signal.SIGCONT])

    def test_unconfirmed_sigstop_never_writes_index(self):
        path = os.path.join(self.tmp.name, "not-stopped.mkv")
        _write_index_test_mkv(path, [_index_test_cluster(40), _index_test_cluster(80)])
        original = Path(path).read_bytes()
        proc = _Proc()
        proc.pid = 12345
        job = {"proc": proc, "path": path, "cues": [], "index_scan": 0,
               "media_end": 0.0, "index_full": False}
        with patch.object(appmod, "_wait_process_stopped", return_value=False), \
                patch.object(appmod.os, "kill") as pause:
            self.assertFalse(appmod._mkv_update_index(job))
        self.assertEqual(Path(path).read_bytes(), original)
        self.assertEqual([call.args[1] for call in pause.call_args_list],
                         [appmod.signal.SIGSTOP, appmod.signal.SIGCONT])
        layout = appmod._mkv_index_header(original, len(original))
        self.assertEqual(job["index_scan"], layout["first_cluster"],
                         "写索引失败不能提交扫描游标，否则后续永远跳过Cues")
        with patch.object(appmod, "_wait_process_stopped", return_value=True), \
                patch.object(appmod.os, "kill"):
            self.assertTrue(appmod._mkv_update_index(job),
                            "瞬时SIGSTOP超时后，下次必须重扫并成功写入")
        self.assertEqual([t for t, _p in job["cues"]], [40, 80])
        self.assertEqual(job["index_scan"], len(original))

    async def test_relay_generation_is_shared_and_rotates_after_rebuild(self):
        target = _target()
        request = _request()
        request.scope["headers"].append((
            b"user-agent", b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"))

        def link_generation(link):
            return urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)["g"][0]

        # 尚未创建job时，链接先预留代际；随后预热必须消费同一token。
        first_generation = link_generation(appmod._stream_play_link(target, "mg", request))
        with patch.object(appmod.asyncio, "create_subprocess_exec",
                          new=AsyncMock(side_effect=[_Proc(), _Proc()])):
            first = await appmod._ensure_relay_job(target, request)
            self.assertEqual(first["generation"], first_generation)
            with patch.object(appmod.store, "resolve_media_guid", return_value=target), \
                    patch.object(appmod, "_pick_episode", return_value=target):
                redirect = await appmod.media_range("mg", request)
            self.assertEqual(link_generation(redirect.headers["location"]), first_generation)

            appmod._relay_drop(first["key"])
            second_generation = link_generation(appmod._stream_play_link(target, "mg", request))
            self.assertNotEqual(second_generation, first_generation)
            second = await appmod._ensure_relay_job(target, request)
            self.assertEqual(second["generation"], second_generation)

        # 旧fncache持有的上一代高Range不得读新job的字节。
        stale_request = _request(f"start=0&g={first_generation}".encode())
        stale_request.scope["headers"].append((b"range", b"bytes=75239628-"))
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod, "_ensure_relay_job",
                             new=AsyncMock(return_value=second)):
            rejected = await appmod.ios_relay("mg", stale_request)
        self.assertEqual(rejected.status_code, 410)

    async def test_empty_resume_fallback_redirects_to_full_generation(self):
        """真实带g链路：空suffix必须跳到full的新代际，不能递归后410。"""
        target = _target()
        ua = b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"
        link_request = _request()
        link_request.scope["headers"].append((b"user-agent", ua))
        with patch.object(appmod.history, "get", return_value={"ts": 1420, "duration": 3000}), \
                patch.object(appmod.history, "latest_episode_record", return_value=None):
            suffix_link = appmod._stream_play_link(target, "mg", link_request)
        suffix_g = urllib.parse.parse_qs(urllib.parse.urlsplit(suffix_link).query)["g"][0]
        ebml = appmod._ebml_encode_element(0x1A45DFA3, b"")
        segment = appmod._ebml_encode_uint(0x18538067) + b"\x01" + b"\xff" * 7
        header_only = ebml + segment + appmod._ebml_encode_element(0x1549A966, b"")
        with_media = ebml + segment + _index_test_cluster(0)
        spawned = []

        async def spawn(*cmd, **_kwargs):
            path = cmd[-1]
            Path(path).write_bytes(header_only if "-ss" in cmd else with_media)
            proc = _Proc(0)
            spawned.append((cmd, proc))
            return proc

        request = _request(f"start=1420&g={suffix_g}".encode())
        request.scope["headers"].extend([(b"user-agent", ua), (b"range", b"bytes=0-1")])
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn), \
                patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            first = await appmod.ios_relay("mg", request)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.headers["cache-control"], "no-store")
        location = urllib.parse.urlsplit(first.headers["location"])
        query = urllib.parse.parse_qs(location.query)
        full_g = query["g"][0]
        self.assertNotEqual(full_g, suffix_g)
        self.assertEqual(query["start"], ["0"])
        self.assertEqual(len(spawned), 2)

        follow = _request(location.query.encode())
        follow.scope["headers"].extend([(b"user-agent", ua), (b"range", b"bytes=0-1")])
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            final = await appmod.ios_relay("mg", follow)
        self.assertEqual(final.status_code, 206)
        self.assertEqual(final.body, with_media[:2])

    async def test_failed_resume_redirects_to_full_generation(self):
        """ensure内部失败回退full时，也必须把旧suffix g重定向到full g。"""
        target = _target()
        ua = b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"
        link_request = _request()
        link_request.scope["headers"].append((b"user-agent", ua))
        with patch.object(appmod.history, "get", return_value={"ts": 1420, "duration": 3000}), \
                patch.object(appmod.history, "latest_episode_record", return_value=None):
            suffix_link = appmod._stream_play_link(target, "mg", link_request)
        suffix_g = urllib.parse.parse_qs(urllib.parse.urlsplit(suffix_link).query)["g"][0]
        ebml = appmod._ebml_encode_element(0x1A45DFA3, b"")
        segment = appmod._ebml_encode_uint(0x18538067) + b"\x01" + b"\xff" * 7
        with_media = ebml + segment + _index_test_cluster(0)
        spawned = []

        async def spawn(*cmd, **_kwargs):
            if "-ss" not in cmd:
                Path(cmd[-1]).write_bytes(with_media)
            proc = _Proc(0)
            spawned.append((cmd, proc))
            return proc

        request = _request(f"start=1420&g={suffix_g}".encode())
        request.scope["headers"].extend([(b"user-agent", ua), (b"range", b"bytes=0-1")])
        with patch.object(appmod.asyncio, "create_subprocess_exec", side_effect=spawn), \
                patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            suffix = await appmod._ensure_relay_job(target, request)
            self.assertEqual(suffix["generation"], suffix_g)
            suffix["proc"].returncode = 1
            first = await appmod.ios_relay("mg", request)
        self.assertEqual(first.status_code, 302)
        location = urllib.parse.urlsplit(first.headers["location"])
        query = urllib.parse.parse_qs(location.query)
        self.assertNotEqual(query["g"][0], suffix_g)
        self.assertEqual(query["start"], ["0"])
        self.assertEqual(len(spawned), 2)

        follow = _request(location.query.encode())
        follow.scope["headers"].extend([(b"user-agent", ua), (b"range", b"bytes=0-1")])
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            final = await appmod.ios_relay("mg", follow)
        self.assertEqual(final.status_code, 206)
        self.assertEqual(final.body, with_media[:2])

    async def test_covering_alias_redirects_to_actual_generation(self):
        """较早start任务覆盖新续播点时，旧别名URL也统一302到实际g。"""
        target = _target()
        request = _request(b"start=222")
        _resume, alias_g = appmod._ios_relay_url_identity(target, request)
        earlier = self._job(b"mkv", returncode=None)
        earlier.update(key=appmod._relay_key(dict(target, _relay_start=3)),
                       base_key=appmod._relay_key(target), start=3,
                       generation="covering-generation", media_end=300.0,
                       cues=[(40, 100)], duration=600)
        appmod._RELAY_JOBS[earlier["key"]] = earlier
        request = _request(f"start=222&g={alias_g}".encode())
        request.scope["headers"].extend([
            (b"user-agent", b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"),
            (b"range", b"bytes=0-1")])
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            response = await appmod.ios_relay("mg", request)
        self.assertEqual(response.status_code, 302)
        location = urllib.parse.urlsplit(response.headers["location"])
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual(query["g"], ["covering-generation"])
        self.assertEqual(query["start"], ["3"])

        follow = _request(location.query.encode())
        follow.scope["headers"].extend([
            (b"user-agent", b"1.13.0 (com.trim.media.ios; build:1130000; iOS 27.2)"),
            (b"range", b"bytes=0-1")])
        with patch.object(appmod, "_pick_episode", return_value=target), \
                patch.object(appmod.store, "resolve_media_guid", return_value=target):
            final = await appmod.ios_relay("mg", follow)
        self.assertEqual(final.status_code, 206)
        self.assertEqual(final.body, b"mk")

    def test_completed_real_index_is_adopted_read_only(self):
        path = os.path.join(self.tmp.name, "real-final.mkv")
        first = _index_test_cluster(40)
        second = _index_test_cluster(80)
        _write_index_test_mkv(path, [first, second])
        raw = Path(path).read_bytes()
        layout = appmod._mkv_index_header(raw, len(raw))
        cues = [(40, layout["first_cluster"] - layout["segment_data"]),
                (80, layout["first_cluster"] + len(first) - layout["segment_data"])]
        payload = appmod._mkv_index_payload(
            layout["segment_data"], layout["index_start"],
            layout["index_end"] - layout["index_start"], cues, 1)
        with open(path, "r+b", buffering=0) as out:
            out.seek(layout["index_start"])
            out.write(payload)
        original = Path(path).read_bytes()
        job = {"proc": _Proc(returncode=0), "path": path, "cues": [],
               "index_scan": 0, "media_end": 0.0, "index_full": False}
        self.assertTrue(appmod._mkv_update_index(job))
        self.assertEqual(Path(path).read_bytes(), original)
        self.assertEqual(job["cues"], cues)
        self.assertAlmostEqual(job["media_end"], 0.08)
        self.assertTrue(job["index_finalized"])

    def test_zero_based_final_index_media_end_uses_global_coordinate(self):
        path = os.path.join(self.tmp.name, "zero-based-final.mkv")
        first, second = _index_test_cluster(40), _index_test_cluster(80)
        _write_index_test_mkv(path, [first, second])
        raw = Path(path).read_bytes()
        layout = appmod._mkv_index_header(raw, len(raw))
        cues = [(40, layout["first_cluster"] - layout["segment_data"]),
                (80, layout["first_cluster"] + len(first) - layout["segment_data"])]
        payload = appmod._mkv_index_payload(
            layout["segment_data"], layout["index_start"],
            layout["index_end"] - layout["index_start"], cues, 1)
        with open(path, "r+b", buffering=0) as out:
            out.seek(layout["index_start"])
            out.write(payload)
        job = {"proc": _Proc(returncode=0), "path": path, "cues": [],
               "index_scan": 0, "media_end": 1420.0, "index_full": False,
               "zero_based": True, "start": 1420, "timeline_start": 1417.6}
        self.assertTrue(appmod._mkv_update_index(job))
        self.assertAlmostEqual(job["media_end"], 1417.68)

    def test_completed_job_coverage_uses_actual_media_end_not_duration(self):
        base = _target()
        job = self._job(b"mkv", returncode=0)
        job.update(key=appmod._relay_key(base), base_key=appmod._relay_key(base),
                   start=0, media_end=5.0, duration=600, cues=[(40, 100)],
                   index_finalized=True)
        appmod._RELAY_JOBS[job["key"]] = job
        with patch.object(appmod, "_relay_has_media", return_value=True):
            self.assertEqual(appmod._relay_job_media_end(job), 5.0)
            self.assertIsNone(appmod._relay_covering_job(base, 500))
            self.assertIs(appmod._relay_covering_job(base, 5), job)


if __name__ == "__main__":
    unittest.main(verbosity=2)
