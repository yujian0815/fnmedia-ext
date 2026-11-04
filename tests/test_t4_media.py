"""Offline module contracts; no production services or credentials."""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import t4
import t4_media

SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_private_media_stays_server_side(self):
        for url in ("http://127.0.0.1/a.mp4", "http://192.168.1.2/a.mp4",
                    "http://[::1]/a.mp4", "http://nas.local/a.mp4"):
            with self.subTest(url=url):
                self.assertTrue(t4.Playback(url, {}, "MP4").needs_proxy)
        self.assertFalse(t4.Playback("https://media.test/a.mp4", {}, "MP4").needs_proxy)


    async def test_public_media_redirect_without_fetch(self):
        media = t4_media.T4Media()
        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: self.fail("must not proxy a public URL"))) as client:
            response = await media.deliver(client, "https://media.test/v.mkv",
                                          t4.Playback("https://media.test/v.mkv", {}, "MKV"), request)
        self.assertEqual(response.status_code, 302)


    async def test_range_head_and_stream_cleanup(self):
        calls = []

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"abcd"

            async def aclose(self):
                self.closed = True

        stream = Stream()

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Cookie"], "session")
            return httpx.Response(206, headers={
                "Content-Range": "bytes 100-103/1000", "Content-Length": "4",
                "Content-Type": "video/mp4", "Accept-Ranges": "bytes"}, stream=stream)

        playback = t4.Playback("http://media.test/v.mp4",
                               {"Cookie": "session", "Range": "bytes=0-"}, "MP4")
        media = t4_media.T4Media()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="GET", headers={"range": "bytes=100-103"}))
            content = b"".join([chunk async for chunk in response.body_iterator])
            self.assertEqual(content, b"abcd")
            self.assertTrue(stream.closed)
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="HEAD", headers={}))
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 100-103/1000")
        self.assertEqual(calls[0].headers["Range"], "bytes=100-103")
        self.assertEqual(calls[0].headers.get_list("Range"), ["bytes=100-103"])
        self.assertEqual(calls[1].method, "HEAD")


    async def test_cancelled_stream_closes_upstream(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.sleep(10)
                yield b"data"

            async def aclose(self):
                closed.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Stream()))) as client:
            response = await t4_media.T4Media().deliver(
                client, "http://media.test/v.mp4",
                t4.Playback("http://media.test/v.mp4", {"Referer": "test"}, "MP4"),
                SimpleNamespace(method="GET", headers={}))
            task = asyncio.create_task(anext(response.body_iterator))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())


    async def test_identify_uses_bounded_get_and_closes_media(self):
        calls, streams = [], []

        class PrefixStream(httpx.AsyncByteStream):
            closed = False
            requested_more = False

            async def __aiter__(self):
                yield b"\x00\x00\x00\x18ftypisom" + bytes(4096 - 12)
                self.requested_more = True
                raise AssertionError("format identification must not read the video")

            async def aclose(self):
                self.closed = True

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Referer"], "needed")
            if request.method == "HEAD":
                return httpx.Response(405)
            self.assertEqual(request.headers["Range"], "bytes=0-4095")
            stream = PrefixStream()
            streams.append(stream)
            return httpx.Response(206, stream=stream,
                                  headers={"Content-Type": "application/octet-stream"})

        playback = t4.Playback("https://media.test/signed", {"Referer": "needed"}, "")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertIs(await t4_media.T4Media().identify(client, playback), playback)
        self.assertEqual(playback.wrapper, "MP4")
        self.assertEqual([r.method for r in calls], ["GET"])
        self.assertTrue(streams[0].closed)
        self.assertFalse(streams[0].requested_more)
        self.assertEqual(playback.headers, {"Referer": "needed"})


    async def test_identify_generic_head_checks_real_get_prefix_without_guessing(self):
        for prefix, wrapper in (
            (b"#EXTM3U\n#EXTINF:6,\nsegment.ts\n", "HLS"),
            (b"\x1a\x45\xdf\xa3\x80matroska", "MKV"),
            (b"<html>upstream error</html>", ""),
            (b'{"error":"no media"}', ""),
        ):
            with self.subTest(wrapper=wrapper, prefix=prefix[:8]):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(200, content=prefix if request.method == "GET" else b"",
                                          headers={"Content-Type": "application/octet-stream"})

                playback = t4.Playback("https://media.test/signed", {}, "")
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    if wrapper:
                        await t4_media.T4Media().identify(client, playback)
                    else:
                        with self.assertRaises(t4.T4Error):
                            await t4_media.T4Media().identify(client, playback)
                self.assertEqual(playback.wrapper, wrapper)
                self.assertEqual([r.method for r in calls], ["GET"])


    async def test_identify_get_still_rejects_real_http_failures(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403)

        playback = t4.Playback("https://media.test/signed", {}, "")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(t4.T4Error):
                await t4_media.T4Media().identify(client, playback)
        self.assertEqual(playback.wrapper, "")
        self.assertEqual([r.method for r in calls], ["GET"])


    async def test_identify_checks_actual_container_not_url_mime_or_declared_type(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(206, content=b"\x1a\x45\xdf\xa3\x80matroska",
                                  headers={"Content-Type": "video/mp4"})

        playback = t4.Playback("https://media.test/wrong.mp4", {}, "MP4")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = t4_media.T4Media()
            await media.identify(client, playback)
            await media.identify(client, playback)
        self.assertEqual(playback.wrapper, "MKV")
        self.assertEqual(len(calls), 1)


    async def test_identify_rejects_html_even_with_mp4_filename_and_mime(self):
        playback = t4.Playback("https://media.test/error.mp4", {}, "MP4")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:
                httpx.Response(200, content=b"<html>error</html>",
                               headers={"Content-Type": "video/mp4"}))) as client:
            with self.assertRaises(t4.T4Error):
                await t4_media.T4Media().identify(client, playback)
        self.assertFalse(playback.identified)


    async def test_hls_nested_and_key_network_only(self):
        media = t4_media.T4Media()
        manifest = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nchild/list.m3u8\n'
        child = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="../key"\n#EXTINF:6,\npart.ts\n'

        def handler(request):
            self.assertEqual(request.headers["Referer"], "needed")
            body = child if "/child/" in request.url.path else manifest
            return httpx.Response(200, text=body,
                                   headers={"Content-Type": "application/vnd.apple.mpegurl"})

        playback = t4.Playback("http://media.test/root.m3u8", {"Referer": "needed"}, "HLS")
        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback, request)
            token = response.body.decode().strip().split("/")[-1]
            response = await media.resource_response(client, token, request)
        text = response.body.decode()
        self.assertIn("METHOD=AES-128", text)
        self.assertNotIn("../key", text)
        self.assertIn('URI="/v/media/t4-resource/', text)
        self.assertEqual(len(media._resources), 3)


    async def test_extensionless_hls_preserves_playlist_roles(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/master", {"Referer": "needed"}, "HLS")
        manifest = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio"\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=1000\nchild\n')
        child = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:6,\nsegment\n'
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Referer"], "needed")
            body = {"/master": manifest.encode(), "/child": child.encode(),
                    "/audio": b"#EXTM3U\n#EXTINF:6,\nsegment\n",
                    "/segment": b"synthetic-media", "/key": b"0123456789abcdef"}[request.url.path]
            return httpx.Response(200, stream=FixtureStream(body), headers={
                "Content-Type": "application/octet-stream"})

        class FixtureStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield self.body

            def __init__(self, body):
                self.body = body

        request = SimpleNamespace(method="GET", headers={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await media.deliver(client, playback.url, playback, request)
            self.assertIsInstance(response, t4_media.Response)
            self.assertTrue(response.headers["content-type"].startswith("application/vnd.apple"))
            lines = response.body.decode().splitlines()
            audio_token = lines[1].split('URI="', 1)[1].split('"')[0].rsplit("/", 1)[-1]
            child_token = lines[-1].rsplit("/", 1)[-1]
            audio = await media.resource_response(client, audio_token, request)
            self.assertIn("/v/media/t4-resource/", audio.body.decode())
            child_response = await media.resource_response(client, child_token, request)
            segment_token = child_response.body.decode().strip().splitlines()[-1].rsplit("/", 1)[-1]
            segment = await media.resource_response(client, segment_token, request)
            self.assertEqual(b"".join([part async for part in segment.body_iterator]),
                             b"synthetic-media")
        self.assertEqual(len(calls), 4)


    async def test_hls_repeated_byterange_uri_keeps_one_resource_identity(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/video.m3u8",
                               {"Referer": "needed"}, "HLS")
        manifest = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MAP:URI="video.mp4",BYTERANGE="16@0"\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16@16\nvideo.mp4\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16\nvideo.mp4\n#EXT-X-ENDLIST\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=manifest))) as client:
            first = await media.deliver(client, playback.url, playback,
                                        SimpleNamespace(method="GET", headers={}))
            second = await media.deliver(client, playback.url, playback,
                                         SimpleNamespace(method="GET", headers={}))
        uris = [line for line in first.body.decode().splitlines()
                if line and not line.startswith("#")]
        self.assertEqual(uris[0], uris[1])
        self.assertIn(f'URI="{uris[0]}"', first.body.decode())
        self.assertIn("#EXT-X-BYTERANGE:16\n", first.body.decode())
        self.assertEqual(first.body, second.body)
        self.assertEqual(len(media._resources), 1)


    def test_hls_resource_identity_includes_headers_and_role(self):
        media = t4_media.T4Media()
        url = "https://media.test/resource"
        first = t4.Playback(url, {"Cookie": "session-a"}, "HLS")
        second = t4.Playback(url, {"Cookie": "session-b"}, "HLS")
        self.assertEqual(media.resource(url, first), media.resource(url, first))
        self.assertNotEqual(media.resource(url, first), media.resource(url, second))
        self.assertNotEqual(media.resource(url, first),
                            media.resource(url, first, manifest=True))


    async def test_hls_head_exposes_playlist_mime_for_native_players(self):
        media = t4_media.T4Media()
        playback = t4.Playback("https://media.test/manifest",
                               {"Referer": "needed"}, "HLS")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "100"}))) as client:
            response = await media.deliver(client, playback.url, playback,
                                          SimpleNamespace(method="HEAD", headers={}))
        self.assertTrue(response.headers["content-type"].startswith(
            "application/vnd.apple.mpegurl"))
        self.assertNotIn("content-length", response.headers)
        self.assertEqual(response.body, b"")


    async def test_resource_expiration_and_limit(self):
        media = t4_media.T4Media()
        playback = t4.Playback("http://media.test/v.mp4", {}, "MP4")
        with patch("t4_media.time.monotonic", return_value=0):
            first = media.resource(playback.url, playback)
            for i in range(4100):
                media.resource(f"http://media.test/{i}.mp4", playback)
        self.assertEqual(len(media._resources), 4096)
        with patch("t4_media.time.monotonic", return_value=21601):
            response = await media.resource_response(None, first.rsplit("/", 1)[-1], None)
        self.assertEqual(response.status_code, 410)


    async def test_oversized_hls_rejected(self):
        data = b"#EXTM3U\n" + b"x" * 1024
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=data, headers={
                    "Content-Type": "application/vnd.apple.mpegurl"}))) as client:
            with patch.object(t4_media, "MAX_MANIFEST", 100):
                result = await t4_media.T4Media().deliver(
                    client, "http://media.test/v.m3u8",
                    t4.Playback("http://media.test/v.m3u8", {"Referer": "test"}, "HLS"),
                    SimpleNamespace(method="GET", headers={}))
        self.assertEqual(result.status_code, 502)
