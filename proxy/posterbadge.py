#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海报角标合成：在海报图上叠加「网盘资源 ☁️」与「源健康点」角标。

飞牛原生媒体库海报墙不接受自定义 UI 标记，唯一能被用户直接看到的途径就是把角标
烧进海报图片本身。这里用 Pillow 合成：
  - 左上：网盘资源徽标（☁ + 网盘名，金底）；
  - 右上：健康点（绿/黄/红 = 该资源当前最优线路的健康画像）；
合成结果按 (图片字节哈希, 角标参数) 做内存 LRU 缓存，海报请求高频、绝大多数命中缓存，
PIL 只在首张合成时跑一次。通用能力（不含任何私有源信息），公开构建保留。
"""
from __future__ import annotations

import io
import threading

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # noqa: BLE001
    Image = None  # type: ignore

_CACHE: dict[str, bytes] = {}
_CACHE_MAX = 400
_LOCK = threading.Lock()

# 健康等级 -> (圆点色, 中文)
_HEALTH_COLOR = {
    0: ((74, 222, 128), "优"),    # 健康 绿
    1: ((250, 204, 21), "健"),    # 慢 黄
    2: ((251, 146, 60), "差"),    # 高失败率 橙
    3: ((248, 113, 113), "死"),   # 死亡冷却 红
}
_PAN_GOLD = (218, 165, 32)


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _cjk_font(size: int):
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return _font(size)


def available() -> bool:
    return Image is not None


def badge(poster_bytes: bytes, pan_label: str = "",
          health_grade: int | None = None) -> bytes | None:
    """在海报上合成角标，返回 JPEG 字节；无法处理时返回 None（调用方回退原图）。"""
    if Image is None or not poster_bytes:
        return None
    if not pan_label and health_grade is None:
        return poster_bytes
    cache_key = f"{hash(poster_bytes)}|{pan_label}|{health_grade}"
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None:
            return hit
    try:
        im = Image.open(io.BytesIO(poster_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = im.size
    draw = ImageDraw.Draw(im, "RGBA")
    scale = max(1.0, w / 300.0)  # 以 300px 宽海报为基准缩放角标

    def _round_rect(xy, radius, fill):
        draw.rounded_rectangle(xy, radius=radius, fill=fill)

    # —— 左上：网盘徽标 ——
    if pan_label:
        text = "☁ " + pan_label
        fs = int(20 * scale)
        f = _cjk_font(fs)
        try:
            tb = draw.textbbox((0, 0), text, font=f)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except Exception:  # noqa: BLE001
            tw, th = int(len(text) * fs * 0.7), fs
        pad = int(7 * scale)
        x0, y0 = int(8 * scale), int(8 * scale)
        x1, y1 = x0 + tw + pad * 2, y0 + th + pad * 2
        _round_rect((x0, y0, x1, y1), int(7 * scale),
                    _PAN_GOLD + (235,))
        draw.text((x0 + pad, y0 + pad - int(2 * scale)), text,
                  fill=(20, 16, 4, 255), font=f)

    # —— 右上：健康点 ——
    if health_grade is not None and health_grade in _HEALTH_COLOR:
        color, _cn = _HEALTH_COLOR[health_grade]
        r = int(11 * scale)
        cx, cy = w - int(12 * scale) - r, int(12 * scale) + r
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     fill=color + (235,), outline=(0, 0, 0, 160),
                     width=max(1, int(2 * scale)))

    out = io.BytesIO()
    im.save(out, format="JPEG", quality=88)
    data = out.getvalue()
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for k in list(_CACHE)[:_CACHE_MAX // 4]:
                _CACHE.pop(k, None)
        _CACHE[cache_key] = data
    return data
