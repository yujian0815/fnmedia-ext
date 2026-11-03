"""Real loopback HTTP verification with synthetic media, not a NAS acceptance."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import t4
from t4_media import T4Media


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_hls_audio_key_and_byte_ranges_over_real_tcp(self):
        media = bytes(range(256)) * 64
        documents = {
            "/master": (b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio"\n'
                        b'#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="a"\nchild\n'),
            "/audio": b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\naudio.aac\n",
            "/child": (b'#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:6\n'
                       b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n'
                       b'#EXT-X-MAP:URI="video.mp4",BYTERANGE="16@0"\n'
                       b'#EXTINF:6,\n#EXT-X-BYTERANGE:16@16\nvideo.mp4\n'
                       b'#EXTINF:6,\n#EXT-X-BYTERANGE:16\nvideo.mp4\n#EXT-X-ENDLIST\n'),
            "/key": b"0123456789abcdef", "/video.mp4": media,
            "/audio.aac": b"synthetic-audio",
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                if self.headers.get("Referer") != "fixture":
                    self.send_error(403)
                    return
                body = documents[urlsplit(self.path).path]
                start, end = 0, len(body) - 1
                span = self.headers.get("Range")
                if span:
                    left, right = span.removeprefix("bytes=").split("-")
                    start, end = int(left), int(right)
                self.send_response(206 if span else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if span:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body[start:end+1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        playback = t4.Playback(f"http://127.0.0.1:{server.server_port}/master",
                               {"Referer": "fixture"}, "HLS")
        proxy = T4Media()
        get = SimpleNamespace(method="GET", headers={})

        async def read(client, uri, headers=None):
            response = await proxy.resource_response(
                client, uri.rsplit("/", 1)[-1],
                SimpleNamespace(method="GET", headers=headers or {}))
            body = response.body if hasattr(response, "body") else b"".join(
                [chunk async for chunk in response.body_iterator])
            return response, body

        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                master = await proxy.deliver(client, playback.url, playback, get)
                audio_uri = re.search(r'URI="([^"]+)"', master.body.decode())[1]
                _, audio = await read(client, audio_uri)
                _, audio_data = await read(client, audio.decode().splitlines()[-1])
                self.assertEqual(audio_data, b"synthetic-audio")
                _, child = await read(client, master.body.decode().splitlines()[-1])
                key_uri = re.search(r'URI="([^"]+)"', child.decode())[1]
                _, key = await read(client, key_uri)
                self.assertEqual(key, documents["/key"])
                segments = [line for line in child.decode().splitlines()
                            if line and not line.startswith("#")]
                self.assertEqual(segments[0], segments[1])
                for start, end in ((0, 15), (16, 31), (32, 47)):
                    response, data = await read(client, segments[0], {
                        "range": f"bytes={start}-{end}"})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(data, media[start:end+1])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    async def test_t4_and_header_media_over_real_tcp(self):
        calls = []
        media = bytes(range(256)) * 4096

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                calls.append((parsed.path, query, self.headers.get("Range")))
                if parsed.path == "/api":
                    if "play" in query:
                        payload = {"url": f"http://127.0.0.1:{self.server.server_port}/video",
                                   "type": "video/mp4", "header": {"Referer": "fixture"}}
                    elif "ids" in query:
                        payload = {"list": [{"vod_id": "1", "vod_name": "Fixture",
                                             "vod_play_from": "line",
                                             "vod_play_url": "正片$opaque"}]}
                    else:
                        payload = {"list": [{"vod_id": "1", "vod_name": "Fixture"}]}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.headers.get("Referer") != "fixture":
                    self.send_error(403)
                    return
                span = self.headers.get("Range", "").removeprefix("bytes=").split("-")
                start, end = int(span[0]), int(span[1])
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(media)}")
                self.end_headers()
                self.wfile.write(media[start:end + 1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        source = {"id": "loopback", "api": f"http://127.0.0.1:{server.server_port}/api",
                  "kind": "t4", "enabled": True}
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                titles = await t4.search(client, source, "Fixture")
                title = await t4.detail(client, source, titles[0].locator)
                episode = title.sources["line"][0]
                result = await t4.player(client, source, episode.url, episode.play_from)
                proxy = T4Media()
                for start, end in [(0, 15), (500000, 500015), (len(media) - 16, len(media) - 1)]:
                    response = await proxy.deliver(client, result.url, result,
                                                  SimpleNamespace(method="GET", headers={
                                                      "range": f"bytes={start}-{end}"}))
                    data = b"".join([chunk async for chunk in response.body_iterator])
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(data, media[start:end + 1])
                    self.assertEqual(response.headers["content-range"],
                                     f"bytes {start}-{end}/{len(media)}")
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(len(calls), 6)


if __name__ == "__main__":
    unittest.main()
