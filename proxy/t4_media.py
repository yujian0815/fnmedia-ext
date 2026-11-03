"""Bounded header-aware network delivery; no transcoding or video decoding."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from urllib.parse import urljoin

import httpx
from fastapi.responses import RedirectResponse, Response, StreamingResponse

import t4
from t4_metadata import MediaProbe

PREFIX = "/v/media/t4-resource"
MAX_MANIFEST = 2 * 1024 * 1024
_log = logging.getLogger("fnmedia.t4.media")


class T4Media:
    def __init__(self):
        self._resources = OrderedDict()
        self._resource_key = secrets.token_bytes(32)
        self.metadata = MediaProbe()

    def resource(self, url: str, playback: t4.Playback, *, manifest=False) -> str:
        # HLS implicit byte ranges require repeated URIs to stay identical.
        identity = json.dumps([url, sorted(playback.headers.items()),
                               playback.wrapper, manifest], separators=(",", ":"))
        token = hashlib.blake2s(identity.encode(), key=self._resource_key,
                               digest_size=18).hexdigest()
        now = time.monotonic()
        self._resources.pop(token, None)
        while self._resources and (len(self._resources) >= 4096 or
                                  next(iter(self._resources.values()))[0] <= now):
            self._resources.popitem(last=False)
        self._resources[token] = now + 6 * 3600, url, playback, manifest
        return f"{PREFIX}/{token}"

    async def resource_response(self, client, token: str, request):
        hit = self._resources.get(token)
        if not hit or hit[0] <= time.monotonic():
            self._resources.pop(token, None)
            return Response("T4 播放会话已过期，请重新播放", status_code=410)
        _, url, playback, manifest = hit
        return await self.deliver(client, url, playback, request, manifest=manifest)

    async def identify(self, client, playback: t4.Playback) -> t4.Playback:
        if playback.identified or playback.wrapper == "DASH":
            return playback
        # Cloud links can be named .mp4 and even advertise MP4 while serving MKV.
        # Inspect at most 4 KiB once per short-lived playback, never decode video.
        headers = httpx.Headers(playback.headers)
        if playback.wrapper != "HLS":
            headers["Range"] = "bytes=0-4095"
        try:
            async with client.stream("GET", playback.url, headers=headers,
                                     timeout=10, follow_redirects=True) as response:
                response.raise_for_status()
                prefix = await anext(response.aiter_bytes(chunk_size=4096), b"")
                wrapper = ""
                if prefix.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
                    wrapper = "HLS"
                elif len(prefix) >= 12 and prefix[4:8] == b"ftyp":
                    wrapper = "MP4"
                elif prefix.startswith(b"\x1a\x45\xdf\xa3") and b"matroska" in prefix:
                    wrapper = "MKV"
                if wrapper:
                    if wrapper in ("MP4", "MKV"):
                        span = re.fullmatch(
                            r"bytes \d+-\d+/(\d+)",
                            response.headers.get("content-range", ""))
                        size = ""
                        if span and response.status_code == 206:
                            size = span[1]
                        elif response.status_code == 200:
                            size = response.headers.get("content-length", "")
                        if size.isdigit():
                            playback.size = int(size)
                    _log.info("T4 media identified method=GET declared=%s wrapper=%s",
                              playback.wrapper or "unknown", wrapper)
                    playback.wrapper = wrapper
                    playback.identified = True
                    return playback
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
            _log.info("T4 media identify method=GET http=%s error=%s",
                      status, type(exc).__name__)
        raise t4.T4Error("无法识别 T4 媒体格式，请检查媒体 GET 响应或服务端 type")

    async def deliver(self, client, url: str, playback: t4.Playback, request,
                      *, manifest: bool | None = None):
        if not playback.needs_proxy:
            return RedirectResponse(url, status_code=302,
                                    headers={"Cache-Control": "no-store"})
        headers = httpx.Headers(playback.headers)
        for name in ("range", "if-range"):
            if name in request.headers:
                headers[name] = request.headers[name]
        upstream = None
        try:
            req = client.build_request(request.method, url, headers=headers,
                                       timeout=httpx.Timeout(60, connect=10))
            upstream = await client.send(req, stream=True, follow_redirects=True)
            if upstream.status_code >= 400:
                status = upstream.status_code
                _log.warning("T4 media delivery method=%s http=%s wrapper=%s",
                             request.method, status, playback.wrapper)
                extra = {"Content-Range": upstream.headers["content-range"]} \
                    if status == 416 and "content-range" in upstream.headers else {}
                await upstream.aclose()
                return Response(f"T4 媒体返回 HTTP {status}", status_code=status, headers=extra)
            ctype = upstream.headers.get("content-type", "").lower()
            is_manifest = (playback.wrapper == "HLS" if manifest is None else manifest) or \
                "mpegurl" in ctype or \
                upstream.url.path.lower().endswith(".m3u8")
            if is_manifest and request.method != "HEAD":
                data = bytearray()
                async for chunk in upstream.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_MANIFEST:
                        raise t4.T4Error("T4 HLS 清单超过大小限制")
                base = str(upstream.url)
                text = data.decode("utf-8-sig")
                if not text.lstrip().startswith("#EXTM3U"):
                    raise t4.T4Error("T4 返回的不是 HLS 清单")
                lines = []
                master = any(line.strip().startswith("#EXT-X-STREAM-INF:")
                             for line in text.splitlines())
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        line = self.resource(urljoin(base, stripped), playback,
                                             manifest=master)
                    elif 'URI="' in line:
                        playlist = stripped.startswith((
                            "#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:",
                            "#EXT-X-IMAGE-STREAM-INF:", "#EXT-X-RENDITION-REPORT:"))
                        line = re.sub(r'URI="([^"]+)"', lambda match:
                                      'URI="' + self.resource(
                                          urljoin(base, match[1]), playback,
                                          manifest=playlist) + '"', line)
                    lines.append(line)
                await upstream.aclose()
                return Response("\n".join(lines) + "\n",
                                media_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-store"})
            forwarded = {k: v for k, v in upstream.headers.items() if k.lower() in (
                "content-type", "content-length", "content-range", "accept-ranges",
                "content-encoding", "etag", "last-modified")}
            if url == playback.url and ctype.split(";", 1)[0] in (
                    "", "application/octet-stream", "binary/octet-stream"):
                mime = {"MP4": "video/mp4", "MKV": "video/x-matroska"}.get(playback.wrapper)
                if mime:
                    forwarded["content-type"] = mime
            forwarded["Cache-Control"] = "no-store"
            if request.method == "HEAD":
                await upstream.aclose()
                if is_manifest:
                    response = Response(status_code=upstream.status_code,
                                        media_type="application/vnd.apple.mpegurl",
                                        headers={"Cache-Control": "no-store"})
                    # GET rewrites the manifest, so the origin length is not ours.
                    if "content-length" in response.headers:
                        del response.headers["content-length"]
                    return response
                return Response(status_code=upstream.status_code, headers=forwarded)
        except (httpx.HTTPError, UnicodeError, t4.T4Error) as exc:
            _log.warning("T4 media delivery method=%s error=%s wrapper=%s",
                         request.method, type(exc).__name__, playback.wrapper)
            if upstream is not None:
                await upstream.aclose()
            return Response("T4 媒体网络请求失败", status_code=502)
        except BaseException:
            if upstream is not None:
                await upstream.aclose()
            raise

        async def pump():
            try:
                async for chunk in upstream.aiter_raw(chunk_size=256 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(pump(), status_code=upstream.status_code,
                                 headers=forwarded)
