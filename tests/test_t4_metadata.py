"""Metadata budgets, exact-resource cache and native response contracts."""
import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import inject
import t4
import t4_media
import t4_metadata as meta


def box(kind, payload=b""):
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


INFO = {"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main",
     "width": 3840, "height": 2024, "duration": "2860.698", "bit_rate": "4588470",
     "pix_fmt": "yuv420p", "r_frame_rate": "25/1", "avg_frame_rate": "25/1",
     "color_transfer": "bt709"},
    {"index": 1, "codec_type": "audio", "codec_name": "aac", "profile": "LC",
     "channels": 2, "sample_rate": "44100", "bit_rate": "189588",
     "tags": {"language": "chi"}}], "format": {"duration": "2860.698", "bit_rate": "7829"}}


class Blob(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed = True


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.streams = [], []
        self.data = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 20000) + box(b"moov", b"fixture")
        self.ignore_range = False
        self.bad_range = False
        self.playback = t4.Playback("http://media.test/movie.mp4", {"Referer": "private"}, "MP4")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)

    def handle(self, request):
        self.calls.append(request)
        self.assertEqual(request.headers["referer"], "private")
        self.assertEqual(request.headers["accept-encoding"], "identity")
        start, end = map(int, request.headers["range"][6:].split("-"))
        end = min(end, len(self.data) - 1)
        raw = self.data if self.ignore_range else self.data[start:end + 1]
        stream = Blob(raw)
        self.streams.append(stream)
        return httpx.Response(
            200 if self.ignore_range else 206, stream=stream,
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(len(raw)),
                     "Content-Range": f"bytes {start + int(self.bad_range)}-{end}/{len(self.data)}"})

    async def test_trailing_moov_skips_media_and_singleflights(self):
        probe = meta.MediaProbe()
        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", new_callable=AsyncMock, return_value=INFO) as inspect:
            results = await asyncio.gather(*[probe.probe(self.client, self.playback) for _ in range(12)])
        self.assertTrue(all(r["video"]["codec_name"] == "hevc" for r in results))
        inspect.assert_awaited_once()
        self.assertEqual(inspect.call_args.args[0],
                         box(b"ftyp", b"isom") + box(b"moov", b"fixture"))
        requested = sum(int(r.headers["range"].split("-")[1]) -
                        int(r.headers["range"].split("=")[1].split("-")[0]) + 1 for r in self.calls)
        self.assertLess(requested, 256)
        self.assertTrue(all(s.closed for s in self.streams))
        self.assertFalse(probe._locks)
        self.assertEqual(results[0]["size"], len(self.data))
        self.assertEqual(results[0]["video"]["bps"], 4588470)

    async def test_handoff_deadline_keeps_bounded_probe_and_reuses_completed_metadata(self):
        probe = meta.MediaProbe()
        self.addAsyncCleanup(probe.close)
        entered, unblock = asyncio.Event(), asyncio.Event()

        async def inspect(*_):
            entered.set()
            await unblock.wait()
            return INFO

        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", side_effect=inspect) as mock:
            self.assertEqual(await probe.for_playback(
                self.client, self.playback, wait=0.01), {})
            await entered.wait()
            self.assertEqual(await probe.for_playback(
                self.client, self.playback, wait=0), {})
            self.assertEqual(len(probe._pending), 1)
            mock.assert_awaited_once()
            unblock.set()
            await asyncio.gather(*probe._pending.values())
            result = await probe.for_playback(self.client, self.playback, wait=0)
            self.assertEqual(result["video"]["codec_name"], "hevc")
            self.assertFalse(probe._pending)
            mock.assert_awaited_once()

    async def test_background_probe_limit_request_cancel_and_shutdown_cleanup(self):
        probe = meta.MediaProbe()
        self.addAsyncCleanup(probe.close)
        entered = asyncio.Event()
        active = 0

        async def inspect(*_):
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            await asyncio.Event().wait()

        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", side_effect=inspect):
            for i in range(20):
                await probe.for_playback(self.client, t4.Playback(
                    f"http://media.test/{i}.mp4", self.playback.headers, "MP4"), wait=0)
            self.assertEqual(len(probe._pending), 2)
            await asyncio.wait_for(entered.wait(), 1)
            waiting = asyncio.create_task(probe.for_playback(self.client, t4.Playback(
                "http://media.test/0.mp4", self.playback.headers, "MP4"), wait=20))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(len(probe._pending), 2)
            await probe.close()
        self.assertFalse(probe._pending)
        self.assertFalse(probe._locks)
        self.assertEqual(probe._slots._value, 2)
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_optional_probe_failure_does_not_fail_handoff_or_log_secrets(self):
        probe = meta.MediaProbe()
        self.addAsyncCleanup(probe.close)
        with patch.object(probe, "probe", side_effect=RuntimeError("fixture-secret")):
            with self.assertLogs("fnmedia.t4.metadata", level="WARNING") as logs:
                self.assertEqual(await probe.for_playback(
                    self.client, self.playback, wait=1), {})
        self.assertIn("RuntimeError", str(logs.output))
        self.assertNotIn("fixture-secret", str(logs.output))
        self.assertFalse(probe._pending)

    async def test_no_wrong_offsets_when_origin_ignores_range(self):
        self.ignore_range = True
        probe = meta.MediaProbe()
        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", new_callable=AsyncMock) as inspect:
            self.assertEqual(await probe.probe(self.client, self.playback), {})
            count = len(self.calls)
            self.assertEqual(await probe.probe(self.client, self.playback), {})
            self.assertEqual(len(self.calls), count)
            inspect.assert_not_called()
        self.assertLessEqual(count, 3)
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_invalid_content_range_and_huge_box_are_not_followed(self):
        for mode in ("range", "box"):
            self.calls.clear()
            self.bad_range = mode == "range"
            if mode == "box":
                self.data = (meta.MAX_BYTES * 2).to_bytes(4, "big") + b"moov" + b"x" * 100
            with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                    patch("t4_metadata.inspect_bytes", new_callable=AsyncMock) as inspect:
                self.assertEqual(await meta.MediaProbe().probe(self.client, self.playback), {})
                inspect.assert_not_called()
            self.assertEqual(len(self.calls), 1)

    async def test_cache_never_reuses_another_episode_or_headers(self):
        probe = meta.MediaProbe()
        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", new_callable=AsyncMock, return_value=INFO) as inspect:
            await probe.probe(self.client, self.playback)
            second = t4.Playback("http://media.test/episode2.mp4", self.playback.headers, "MP4")
            self.assertIsNone(probe.cached(second))
            changed = t4.Playback(self.playback.url, {"Referer": "different"}, "MP4")
            self.assertIsNone(probe.cached(changed))
            await probe.probe(self.client, second)
            self.assertEqual(inspect.await_count, 2)

    async def test_probe_concurrency_is_bounded_and_cancellation_cleans_up(self):
        probe = meta.MediaProbe()
        entered, unblock = asyncio.Event(), asyncio.Event()
        active = 0

        async def inspect(*_):
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            await unblock.wait()
            return INFO

        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", side_effect=inspect):
            tasks = [asyncio.create_task(probe.probe(self.client, t4.Playback(
                f"http://media.test/{i}.mp4", self.playback.headers, "MP4"))) for i in range(2)]
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(await probe.probe(self.client, self.playback), {})
            tasks[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await tasks[0]
            unblock.set()
            await tasks[1]
        self.assertEqual(active, 2)
        self.assertFalse(probe._locks)
        self.assertEqual(probe._slots._value, 2)
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_subprocess_is_killed_and_reaped_on_cancel(self):
        proc = SimpleNamespace(returncode=None, communicate=AsyncMock(), kill=lambda: None,
                               wait=AsyncMock())
        entered = asyncio.Event()

        async def communicate():
            entered.set()
            await asyncio.Event().wait()

        proc.communicate.side_effect = communicate
        with patch("t4_metadata.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=proc) as spawn, patch.object(proc, "kill") as kill:
            task = asyncio.create_task(meta.inspect_bytes(b"fixture", "ffprobe"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once()
            proc.wait.assert_awaited_once()
            self.assertIn("-nofind_stream_info", spawn.call_args.args)
            self.assertIn("-protocol_whitelist", spawn.call_args.args)

    async def test_missing_ffprobe_does_not_fetch_or_fail_playback(self):
        with patch("t4_metadata.shutil.which", return_value=None):
            self.assertEqual(await meta.MediaProbe().probe(self.client, self.playback), {})
        self.assertEqual(self.calls, [])

    async def test_probe_does_not_require_python311_timeout_api(self):
        with patch.object(meta.asyncio, "timeout", None, create=True), \
                patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.inspect_bytes", new_callable=AsyncMock, return_value=INFO):
            result = await meta.MediaProbe().probe(self.client, self.playback)
        self.assertEqual(result["video"]["codec_name"], "hevc")
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_budget_and_deadline_do_not_block_original_delivery(self):
        reader = meta.RangeReader(self.client, self.playback)
        reader.used = meta.MAX_BYTES - 4
        with self.assertRaises(meta.ProbeUnavailable):
            await reader.read(0, 16)
        self.assertEqual(self.calls, [])
        probe = meta.MediaProbe()

        async def slow(*_):
            await asyncio.Event().wait()

        with patch("t4_metadata.shutil.which", return_value="ffprobe"), \
                patch("t4_metadata.TIME_BUDGET", 0.01), \
                patch("t4_metadata.inspect_bytes", side_effect=slow):
            self.assertEqual(await probe.probe(self.client, self.playback), {})
        self.assertFalse(probe._locks)
        self.assertEqual(probe._slots._value, 2)
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_generic_mime_is_corrected_without_changing_media_bytes(self):
        media = t4_media.T4Media()
        async def handler(request):
            return httpx.Response(206, stream=Blob(b"unchanged"), headers={
                "Content-Type": "application/octet-stream", "Content-Range": "bytes 0-8/100"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, self.playback.url, self.playback,
                SimpleNamespace(method="GET", headers={"range": "bytes=0-8"}))
            self.assertEqual(response.headers["content-type"], "video/mp4")
            self.assertEqual(b"".join([part async for part in response.body_iterator]), b"unchanged")
            self.assertEqual(response.headers["content-range"], "bytes 0-8/100")

    async def test_identification_preserves_total_size_not_partial_content_length(self):
        data = box(b"ftyp", b"isom")
        for status, headers, expected in (
                (206, {"Content-Range": "bytes 0-11/987654321",
                       "Content-Length": "12"}, 987654321),
                (206, {"Content-Length": "12"}, 0),
                (200, {"Content-Length": "987654321"}, 987654321)):
            with self.subTest(status=status, headers=headers):
                async def handler(request):
                    return httpx.Response(status, content=data, headers=headers)

                playback = t4.Playback(self.playback.url, self.playback.headers, "MP4")
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await t4_media.T4Media().identify(client, playback)
                self.assertEqual(playback.size, expected)

    def test_unknown_fields_are_not_fabricated_and_audio_has_stable_identity(self):
        sl = inject.stream_list({"guid": "fixture", "title": "Fixture"})
        meta.apply(sl, "MP4")
        v = sl["video_streams"][0]
        for key in ("resolution_type", "codec_name", "color_range_type", "pix_fmt"):
            self.assertEqual(v[key], "")
        for key in ("bit_depth", "width", "height", "bps", "duration"):
            self.assertEqual(v[key], 0)
        metadata = meta.normalize(INFO, 1711362455)
        meta.apply(sl, "MP4", metadata)
        self.assertEqual(v["resolution_type"], "4K")
        self.assertEqual(v["duration"], 2861)
        self.assertEqual(sl["files"][0]["size"], 1711362455)
        self.assertEqual(sl["audio_streams"][0]["codec_name"], "aac")
        guid = sl["audio_streams"][0]["guid"]
        meta.apply(sl, "MP4", metadata)
        self.assertEqual(sl["audio_streams"][0]["guid"], guid)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg fixture unavailable")
    async def test_real_ffprobe_reads_a_tail_moov_without_media_payload(self):
        # Generate synthetic media only for a deterministic container fixture.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.mp4"
            await asyncio.to_thread(subprocess.run, [
                shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "color=c=black:s=320x240:r=25", "-t", "0.2", "-c:v", "libx264",
                "-preset", "ultrafast", str(path)],
                check=True, capture_output=True, timeout=15)
            self.data = path.read_bytes()
            probe = meta.MediaProbe()
            result = await probe.probe(self.client, self.playback)
            self.assertEqual(result["video"]["codec_name"], "h264")
            self.assertEqual(result["video"]["width"], 320)
            self.assertAlmostEqual(result["video"]["duration"], 0.2)
            self.assertEqual(result["size"], len(self.data))


if __name__ == "__main__":
    unittest.main()
