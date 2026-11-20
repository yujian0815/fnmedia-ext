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
        if hasattr(appmod, "_RELAY_LOCK"):
            appmod._RELAY_LOCK = asyncio.Lock()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
