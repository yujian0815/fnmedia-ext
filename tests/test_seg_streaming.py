"""普通 HLS 分片真实流式与播放吞吐诊断回归。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import linepredict
from starlette.requests import Request
from starlette.responses import StreamingResponse


class FakeResponse:
    def __init__(self, chunks=(b"abc", b"def"), headers=None, status=200):
        self.chunks = list(chunks)
        self.headers = headers or {"content-type": "video/mp2t"}
        self.status_code = status
        self.closed = False
        self.aread_called = False
        self.iter_started = False
        self._content = None

    async def aiter_raw(self, chunk_size=None):
        self.iter_started = True
        for chunk in self.chunks:
            yield chunk

    async def aread(self):
        self.aread_called = True
        self._content = b"".join(self.chunks)
        return self._content

    @property
    def content(self):
        return self._content if self._content is not None else b"".join(self.chunks)

    async def aclose(self):
        self.closed = True


def _request(src="https://cdn.example.com/a.ts", extra=""):
    from urllib.parse import urlencode
    qs = urlencode({"u": appmod.b64e(src)}) + extra
    return Request({"type": "http", "method": "GET", "path": "/seg",
                    "headers": [], "query_string": qs.encode()})


class SegmentStreamingTests(unittest.IsolatedAsyncioTestCase):
    def _patches(self, response):
        return (
            patch.object(appmod, "_await_pending_chart", new=AsyncMock(return_value="")),
            patch.object(appmod, "_pick_episode", return_value={
                "headers": {}, "line": "lzi::lzm3u8",
                "online_guid": "og", "index": 0}),
            patch.object(appmod, "ext_get_stream_retry",
                         new=AsyncMock(return_value=response)),
            patch.object(appmod, "ext_get_retry", new=AsyncMock()),
            patch.object(appmod.store, "online_guid_by_media", return_value="og"),
            patch.object(appmod.linepredict, "record_seg"),
        )

    async def test_plain_segment_streams_without_buffered_get(self):
        upstream = FakeResponse()
        ps = self._patches(upstream)
        with ps[0], ps[1], ps[2] as stream_get, ps[3] as buffered_get, ps[4], ps[5] as record:
            resp = await appmod.hls_seg("mg", _request())
            self.assertIsInstance(resp, StreamingResponse)
            self.assertFalse(upstream.iter_started)  # 返回前未预读分片
            body = b"".join([chunk async for chunk in resp.body_iterator])
        self.assertEqual(body, b"abcdef")
        stream_get.assert_awaited_once()
        buffered_get.assert_not_called()
        self.assertTrue(upstream.closed)
        self.assertEqual(record.call_args.kwargs["nbytes"], 6)

    async def test_encrypted_segment_keeps_buffered_path(self):
        upstream = FakeResponse()
        ps = self._patches(upstream)
        with ps[0], ps[1], ps[2] as stream_get, ps[3] as buffered_get, ps[4], ps[5]:
            buffered_get.return_value = upstream
            resp = await appmod.hls_seg("mg", _request(extra="&dec=1"))
        self.assertNotIsInstance(resp, StreamingResponse)
        stream_get.assert_not_called()
        buffered_get.assert_awaited_once()

    async def test_non_identity_encoding_reads_before_return(self):
        upstream = FakeResponse(headers={"content-encoding": "gzip"})
        ps = self._patches(upstream)
        with ps[0], ps[1], ps[2], ps[3] as buffered_get, ps[4], ps[5]:
            resp = await appmod.hls_seg("mg", _request())
        self.assertNotIsInstance(resp, StreamingResponse)
        self.assertTrue(upstream.aread_called)
        buffered_get.assert_not_called()

    async def test_stream_retry_uses_send_stream_true(self):
        upstream = FakeResponse()

        class Client:
            def build_request(self, method, url, headers=None, timeout=None):
                return {"method": method, "url": url}

            async def send(self, request, stream=False):
                self.stream = stream
                return upstream

        cli = Client()
        with patch.object(appmod, "ext_client", return_value=cli):
            r = await appmod.ext_get_stream_retry(_request(), "https://cdn.example.com/a.ts")
        self.assertIs(r, upstream)
        self.assertTrue(cli.stream)
        self.assertFalse(upstream.aread_called)


class ThroughputTests(unittest.TestCase):
    def tearDown(self):
        linepredict.reset_seg_timing()

    def test_actual_bytes_produce_median_throughput_only_for_diagnostics(self):
        linepredict.record_seg("og", 0, "line-a", 2.0, nbytes=2000)
        linepredict.record_seg("og", 0, "line-a", 1.0, nbytes=2000)
        self.assertEqual(linepredict.throughput("og", 0, "line-a"), 1500.0)
        self.assertIsNone(linepredict.throughput("og", 0, "line-b"))
        # 旧的秒数样本与切换判断仍保持原语义，不被吞吐诊断改写。
        self.assertIsNone(linepredict.pick_faster_line("og", 0, "line-a"))


if __name__ == "__main__":
    unittest.main()
