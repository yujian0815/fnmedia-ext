"""Bounded container inspection. No media decoding, remote ffprobe or transcoding."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
from collections import OrderedDict

import httpx

_log = logging.getLogger("fnmedia.t4.metadata")
MAX_BYTES = 8 * 1024 * 1024
TIME_BUDGET = 8.0


class ProbeUnavailable(Exception):
    pass


class RangeReader:
    def __init__(self, client, playback):
        self.client, self.playback = client, playback
        self.used = 0
        self.total = 0

    async def read(self, start: int, length: int) -> bytes:
        if length <= 0 or length > MAX_BYTES - self.used:
            raise ProbeUnavailable()
        headers = httpx.Headers(self.playback.headers)
        headers["Range"] = f"bytes={start}-{start + length - 1}"
        headers["Accept-Encoding"] = "identity"
        async with self.client.stream("GET", self.playback.url, headers=headers,
                                      follow_redirects=True, timeout=TIME_BUDGET) as response:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ProbeUnavailable()
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                     response.headers.get("content-range", ""))
                if not match:
                    raise ProbeUnavailable()
                left, right, total = map(int, match.groups())
                if left != start or right != min(start + length, total) - 1 or \
                        total <= right or (self.total and total != self.total):
                    raise ProbeUnavailable()
                self.total = total
                length = right - left + 1
            elif response.status_code == 200 and start == 0:
                # Read only the bounded prefix when an origin ignores Range.
                value = response.headers.get("content-length", "")
                self.total = int(value) if value.isdigit() else 0
            else:
                raise ProbeUnavailable()
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=min(length, 64 * 1024)):
                take = chunk[:length - len(data)]
                data.extend(take)
                self.used += len(take)
                if len(data) >= length:
                    break
            return bytes(data)

    async def mp4(self) -> bytes:
        offset, ftyp = 0, b""
        # Follow box sizes, never scan or download mdat to find a trailing moov.
        for _ in range(32):
            head = await self.read(offset, 16)
            if len(head) < 8:
                raise ProbeUnavailable()
            size, kind = int.from_bytes(head[:4], "big"), head[4:8]
            header_size = 16 if size == 1 else 8
            if size == 1:
                size = int.from_bytes(head[8:16], "big")
            if size < header_size or not self.total or offset + size > self.total:
                raise ProbeUnavailable()
            if kind in (b"ftyp", b"moov"):
                if kind == b"ftyp" and size > 65536:
                    raise ProbeUnavailable()
                box = await self.read(offset, size)
                if len(box) != size:
                    raise ProbeUnavailable()
                if kind == b"ftyp":
                    ftyp = box
                elif ftyp:
                    return ftyp + box
                else:
                    raise ProbeUnavailable()
            offset += size
            if offset >= self.total:
                break
        raise ProbeUnavailable()


def _number(value) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else 0
    except (ValueError, TypeError):
        return 0


def normalize(info: dict, size: int) -> dict:
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and
                  not s.get("disposition", {}).get("attached_pic")), {})
    if not video.get("codec_name"):
        return {}
    duration = _number(video.get("duration")) or _number(
        info.get("format", {}).get("duration"))
    # The probe file omits mdat. Its format.bit_rate is not the media bitrate.
    bps = int(_number(video.get("bit_rate")))
    depth = int(_number(video.get("bits_per_raw_sample")))
    pix = video.get("pix_fmt") or ""
    if not depth:
        match = re.search(r"(?:p|gray)(10|12|16)", pix)
        depth = int(match[1]) if match else (8 if pix in (
            "yuv420p", "yuv422p", "yuv444p", "nv12", "yuvj420p") else 0)
    width, height = int(_number(video.get("width"))), int(_number(video.get("height")))
    resolution = ("4K" if width >= 3800 or height >= 2100 else
                  "2K" if width >= 2500 or height >= 1400 else
                  "1080P" if width >= 1900 or height >= 1000 else
                  "720P" if width >= 1250 or height >= 700 else
                  f"{height}P" if height else "")
    v = {key: video[key] for key in (
        "codec_name", "profile", "pix_fmt", "display_aspect_ratio", "color_range",
        "color_space", "color_transfer", "color_primaries") if key in video}
    v.update(width=width, height=height, duration=duration, bps=bps,
             level=str(video.get("level") or ""), bit_depth=depth,
             bits_per_raw_sample=str(depth) if depth else "", resolution_type=resolution,
             r_frame_rate=_frame_rate(video.get("r_frame_rate")),
             avg_frame_rate=_frame_rate(video.get("avg_frame_rate")))
    transfer = video.get("color_transfer", "")
    v["color_range_type"] = ("HDR" if transfer in ("smpte2084", "arib-std-b67")
                              else "SDR" if transfer == "bt709" else "")
    return {"size": size, "video": v, "audio": [
        {key: s[key] for key in (
            "index", "codec_name", "codec_type", "profile", "sample_rate",
            "channels", "channel_layout", "bit_rate", "tags") if key in s}
        for s in streams if s.get("codec_type") == "audio"][:32]}


def _frame_rate(value) -> str:
    if not value:
        return ""
    try:
        left, _, right = str(value).partition("/")
        fps = float(left) / float(right or 1)
        return f"{fps:g} fps" if 0 < fps < 1000 else ""
    except (ValueError, ZeroDivisionError):
        return ""


async def inspect_bytes(data: bytes, binary: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="fnmedia-t4-meta-") as temp:
        path = Path(temp) / "header"
        path.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            binary, "-v", "error", "-max_alloc", str(32 * 1024 * 1024),
            "-nofind_stream_info",
            "-probesize", str(MAX_BYTES), "-analyzeduration", "0",
            "-protocol_whitelist", "file", "-show_entries",
            "format=duration:stream=index,codec_name,codec_type,profile,width,height,"
            "pix_fmt,level,duration,bit_rate,r_frame_rate,avg_frame_rate,bits_per_raw_sample,"
            "display_aspect_ratio,color_range,color_space,color_transfer,color_primaries,"
            "sample_rate,channels,channel_layout:stream_tags=language,title:"
            "stream_disposition=attached_pic",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            return json.loads(out) if proc.returncode == 0 else {}
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()


class MediaProbe:
    def __init__(self):
        self._cache = OrderedDict()
        self._locks = {}
        self._slots = asyncio.Semaphore(2)
        self._pending = {}

    @staticmethod
    def _key(playback):
        return hashlib.sha256(json.dumps(
            [playback.url, sorted(httpx.Headers(playback.headers).items())],
            separators=(",", ":")).encode()).digest()

    def cached(self, playback):
        key = self._key(playback)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            self._cache.move_to_end(key)
            return hit[1]
        self._cache.pop(key, None)
        return None

    async def for_playback(self, client, playback, *, wait: float) -> dict:
        """Optional inspection must not extend a native player's response deadline."""
        if playback.wrapper not in ("MP4", "MKV"):
            return {}
        hit = self.cached(playback)
        if hit is not None:
            return hit
        key = self._key(playback)
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= 2:
                return {}
            task = asyncio.create_task(self.probe(client, playback))
            self._pending[key] = task

            def finished(done):
                self._pending.pop(key, None)
                if not done.cancelled() and done.exception() is not None:
                    _log.warning("T4 metadata background failed error=%s",
                                 type(done.exception()).__name__)

            task.add_done_callback(finished)
        if wait <= 0:
            return {}
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except asyncio.TimeoutError:
            _log.info("T4 metadata deferred wrapper=%s", playback.wrapper)
            return {}
        except Exception:
            # The client can inspect original bytes; optional metadata cannot fail handoff.
            return {}

    async def close(self):
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()

    async def probe(self, client, playback) -> dict:
        if playback.wrapper not in ("MP4", "MKV"):
            return {}
        hit = self.cached(playback)
        if hit is not None:
            return hit
        binary = shutil.which("ffprobe")
        if not binary:
            return {}
        key = self._key(playback)
        if key not in self._locks and self._slots.locked():
            return {}
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                hit = self.cached(playback)
                if hit is not None:
                    return hit
                if self._slots.locked():
                    return {}
                async with self._slots:
                    reader, result = RangeReader(client, playback), {}
                    start = time.monotonic()
                    reason = "no_tracks"

                    async def inspect():
                        data = await reader.mp4() if playback.wrapper == "MP4" else \
                            await reader.read(0, 1024 * 1024)
                        return normalize(await inspect_bytes(data, binary), reader.total)

                    try:
                        result = await asyncio.wait_for(inspect(), timeout=TIME_BUDGET)
                    except (ProbeUnavailable, httpx.HTTPError, OSError, ValueError,
                            asyncio.TimeoutError) as exc:
                        reason = type(exc).__name__
                    self._cache[key] = time.monotonic() + (900 if result else 30), result
                    self._cache.move_to_end(key)
                    while len(self._cache) > 128:
                        self._cache.popitem(last=False)
                    video = result.get("video", {})
                    _log.info("T4 metadata wrapper=%s status=%s bytes=%s ms=%s "
                              "codec=%s width=%s height=%s",
                              playback.wrapper, "ok" if result else reason, reader.used,
                              int((time.monotonic() - start) * 1000),
                              video.get("codec_name", ""), video.get("width", 0),
                              video.get("height", 0))
                    return result
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)


def apply(sl: dict, wrapper: str = "", metadata: dict | None = None):
    """Replace only the synthetic media fields, preserving GUIDs and line selectors."""
    f, v = sl["files"][0], sl["video_streams"][0]
    suffix = {"HLS": "m3u8", "MP4": "mp4", "MKV": "mkv"}.get(wrapper, "")
    name = "T4" + ("." + suffix if suffix else "")
    f.update(path="在线/" + name, file_name=name, size=0)
    for key in ("codec_name", "profile", "level", "resolution_type", "color_range_type",
                "color_range", "display_aspect_ratio", "pix_fmt", "color_space",
                "color_transfer", "color_primaries", "r_frame_rate", "avg_frame_rate",
                "bits_per_raw_sample"):
        v[key] = ""
    for key in ("width", "height", "coded_width", "coded_height", "duration", "bps",
                "bit_depth", "refs", "dv_profile"):
        v[key] = 0
    v["wrapper"] = wrapper
    sl["audio_streams"] = []
    if not metadata:
        return
    f["size"] = metadata["size"]
    v.update(metadata["video"])
    # The host UI formats seconds using integer remainders.
    v["duration"] = int(round(_number(v.get("duration"))))
    for track in metadata.get("audio", []):
        index = track.get("index", 0)
        tags = track.get("tags") or {}
        sl["audio_streams"].append({
            "guid": hashlib.md5(f"{f['guid']}:audio:{index}".encode()).hexdigest(),
            "media_guid": f["guid"], "index": index,
            "codec_name": track.get("codec_name", ""), "codec_type": "audio",
            "profile": track.get("profile", ""), "channels": track.get("channels", 0),
            "channel_layout": track.get("channel_layout", ""),
            "sample_rate": str(track.get("sample_rate") or ""),
            "bps": int(_number(track.get("bit_rate"))),
            "title": tags.get("title", ""), "language": tags.get("language", ""),
            "duration": v["duration"],
        })
