#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把网盘登录二维码烧录进 HLS 占位 TS 分片。

场景：用户在播放页点了「网盘高清（PanSou）」线路，但对应网盘（夸克/UC/阿里）
尚未登录，转存必然失败。飞牛播放器只能解码 HLS 视频分片、无法渲染 HTML，因此
这里用 ffmpeg 动态生成一张「带登录二维码 + 提示文字」的 640x360 / 2s TS，
让用户直接在播放画面里扫码登录，扫码成功后下一次 main 轮询即可自动转正片。

负担控制（实测单次 libx264 veryfast 编码约 76ms）：
- 按「二维码图片字节 + 文案」哈希做内存 LRU 缓存，同一二维码无论分片被拉多少次
  只编码一次，命中缓存 0 开销；
- 单飞：同一 key 的并发请求只编码一次，其余等待缓存；
- 全局信号量把 ffmpeg 并发压到 1，杜绝多会话同时编码；
- ffmpeg 缺失/失败/超时返回 None，调用方回退普通占位分片，绝不阻断播放。
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import tempfile
import threading

# 与 assets/placeholder/placeholder.ts 同规格，保证无缝衔接直播式占位窗口。
_W, _H, _DUR, _FPS = 640, 360, 2.0, 25
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_CACHE: dict[str, bytes] = {}
_CACHE_MAX = 24
_LOCK = threading.Lock()
# 单飞：同一二维码并发只编码一次；全局编码并发上限 1。
_INFLIGHT: dict[str, threading.Event] = {}
_ENCODE_SEM = threading.Semaphore(1)


def _font() -> str:
    import os
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _esc_text(s: str) -> str:
    # drawtext 文本转义，避免中文/特殊字符破坏 filter 表达式。
    return (s.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "\\'").replace("%", "\\%"))


def build_login_ts(qr_png: bytes, title: str = "", subtitle: str = "") -> bytes | None:
    """生成带二维码的 TS 字节；带缓存 + 单飞，失败返回 None。"""
    if not qr_png or not ffmpeg_available():
        return None
    key = hashlib.md5(qr_png).hexdigest() + "|" + hashlib.md5(
        (title + "|" + subtitle).encode("utf-8")).hexdigest()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        waiter = _INFLIGHT.get(key)
        if waiter is None:
            waiter = threading.Event()
            _INFLIGHT[key] = waiter
            leader = True
        else:
            leader = False

    if not leader:
        waiter.wait(timeout=15)
        with _LOCK:
            return _CACHE.get(key)

    try:
        return _encode_login_ts(key, qr_png, title, subtitle)
    finally:
        with _LOCK:
            ev = _INFLIGHT.pop(key, None)
        if ev is not None:
            ev.set()


def _encode_login_ts(key: str, qr_png: bytes, title: str,
                     subtitle: str) -> bytes | None:
    """实际调用 ffmpeg（信号量串行），结果写入缓存。"""
    font = _font()
    title = title or "请使用对应网盘 App 扫码登录"
    subtitle = subtitle or "登录成功后将自动开始播放"
    qr_size, qr_y = 200, 70
    work = tempfile.mkdtemp(prefix="fnqr-")
    try:
        if not _ENCODE_SEM.acquire(timeout=20):
            return None
        try:
            qr_path = f"{work}/qr.png"
            out_path = f"{work}/out.ts"
            with open(qr_path, "wb") as f:
                f.write(qr_png)
            fc = [
                f"[1:v]scale={qr_size}:{qr_size}[qr]",
                f"[0:v][qr]overlay=(W-w)/2:{qr_y}",
            ]
            if font:
                fc.append(
                    f"drawtext=fontfile='{font}':text='{_esc_text(title)}':"
                    f"fontcolor=white:fontsize=22:x=(w-text_w)/2:y=24")
                fc.append(
                    f"drawtext=fontfile='{font}':text='{_esc_text(subtitle)}':"
                    f"fontcolor=0x9ca3af:fontsize=16:x=(w-text_w)/2:y=h-40")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i",
                f"color=c=0x0f1115:s={_W}x{_H}:d={_DUR}:r={_FPS}",
                "-i", qr_path,
                "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex", ",".join(fc) + "[v]",
                "-map", "[v]", "-map", "2:a",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-t", str(_DUR), "-c:a", "aac", "-shortest",
                "-f", "mpegts", out_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            if proc.returncode != 0:
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            if not data:
                return None
        finally:
            _ENCODE_SEM.release()
        with _LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                for old in list(_CACHE)[:_CACHE_MAX // 4]:
                    _CACHE.pop(old, None)
            _CACHE[key] = data
        return data
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def build_login_ts_async(qr_png: bytes, title: str = "",
                               subtitle: str = "") -> bytes | None:
    """在线程池跑 ffmpeg，避免阻塞事件循环。"""
    try:
        return await asyncio.to_thread(build_login_ts, qr_png, title, subtitle)
    except Exception:  # noqa: BLE001
        return None
