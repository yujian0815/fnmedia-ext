#!/usr/bin/env python3
"""片段跳过与自动连播标记（按飞牛账号、季/剧 guid 持久化）。

原生飞牛播放器只理解 play_config.skip_opening / skip_ending（秒），并通过
POST /play/setConfigByItem 写入。这里同时保留 LibreTV 风格的通用 segments
（opening / middle / ending），供校验、展示及后续客户端能力扩展；不虚构
飞牛尚未支持的 middle 原生字段。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import history as _history

_STATE = Path(__file__).resolve().parent.parent / "state" / "skip_marks.json"
_LOCK = threading.RLock()
_TYPES = {"opening", "middle", "ending"}
MIN_SEGMENT_SECONDS = 3
MIN_SEGMENT_GAP_SECONDS = 5
END_GUARD_SECONDS = 2
MAX_NATIVE_SKIP_SECONDS = 600


def _default() -> dict:
    return {"skip_opening": 0, "skip_ending": 0, "segments": [],
            "enabled": True, "auto_skip_segments": True,
            "autoplay_next": True, "custom_hls_skip": False, "updated_at": 0}


def _load() -> dict:
    try:
        if _STATE.exists():
            data = json.loads(_STATE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("users") or {}
    except Exception:
        pass
    return {}


def _save(users: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_STATE)


def _num(value, default=0) -> int:
    try:
        n = int(float(value))
        return max(0, min(MAX_NATIVE_SKIP_SECONDS, n))
    except (TypeError, ValueError):
        return default


def _clean_segment(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or raw.get("type") not in _TYPES:
        return None
    try:
        start = float(raw.get("start", 0))
        end = float(raw.get("end", -1))
    except (TypeError, ValueError):
        return None
    typ = raw["type"]
    if start < 0:
        return None
    if typ == "opening":
        start = 0.0
    if typ == "ending":
        end = -1.0
    elif end <= start or end - start < MIN_SEGMENT_SECONDS:
        return None
    return {"type": typ, "start": start, "end": end}


def get(guid: str) -> dict:
    key = str(guid or "")
    with _LOCK:
        raw = (_load().get(_history.current_user()) or {}).get(key) or {}
    out = _default()
    if isinstance(raw, dict):
        out["skip_opening"] = _num(raw.get("skip_opening"))
        out["skip_ending"] = _num(raw.get("skip_ending"))
        out["segments"] = [s for s in (_clean_segment(x) for x in
                                      (raw.get("segments") or [])) if s]
        out["enabled"] = raw.get("enabled") is not False
        out["auto_skip_segments"] = raw.get("auto_skip_segments") is not False
        out["autoplay_next"] = raw.get("autoplay_next") is not False
        out["custom_hls_skip"] = raw.get("custom_hls_skip") is True
        out["updated_at"] = int(raw.get("updated_at") or 0)
    return out


def set_mark(guid: str, payload: dict) -> dict:
    key = str(guid or "").strip()
    if not key:
        raise ValueError("缺少 guid")
    p = payload if isinstance(payload, dict) else {}
    mark = get(key)
    if "skip_opening" in p:
        mark["skip_opening"] = _num(p.get("skip_opening"))
    if "skip_ending" in p:
        mark["skip_ending"] = _num(p.get("skip_ending"))
    if "segments" in p:
        segments = [s for s in (_clean_segment(x)
                               for x in (p.get("segments") or [])) if s]
        # 与 LibreTV 一致：按 start 排序，重叠或过近时保留靠前者。
        segments.sort(key=lambda s: s["start"])
        clean: list[dict] = []
        for seg in segments:
            if clean and seg["start"] - clean[-1]["end"] < MIN_SEGMENT_GAP_SECONDS:
                continue
            clean.append(seg)
        mark["segments"] = clean
    if "enabled" in p:
        mark["enabled"] = bool(p["enabled"])
    if "auto_skip_segments" in p:
        mark["auto_skip_segments"] = bool(p["auto_skip_segments"])
    if "autoplay_next" in p:
        mark["autoplay_next"] = bool(p["autoplay_next"])
    if "custom_hls_skip" in p:
        mark["custom_hls_skip"] = bool(p["custom_hls_skip"])
    mark["updated_at"] = int(time.time() * 1000)
    with _LOCK:
        users = _load()
        mine = dict(users.get(_history.current_user()) or {})
        mine[key] = mark
        users[_history.current_user()] = mine
        _save(users)
    return mark


def delete(guid: str) -> None:
    key = str(guid or "")
    with _LOCK:
        users = _load()
        mine = dict(users.get(_history.current_user()) or {})
        mine.pop(key, None)
        users[_history.current_user()] = mine
        _save(users)


def play_config(guid: str) -> dict:
    """返回飞牛原生 play_config；禁用或未设置时均为 0，不触发自动跳过。"""
    mark = get(guid)
    if not mark["enabled"] or not mark["auto_skip_segments"]:
        return {"guid": str(guid or ""), "skip_opening": 0, "skip_ending": 0}
    return {"guid": str(guid or ""), "skip_opening": mark["skip_opening"],
            "skip_ending": mark["skip_ending"]}


def resolve_segments(guid: str, duration: float) -> list[dict]:
    """LibreTV 风格的读取侧校验：过滤越界、过短、重叠与过近片段。"""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return []
    mark = get(guid)
    if not mark["enabled"] or not mark["auto_skip_segments"] or duration <= 0:
        return []
    resolved: list[dict] = []
    for seg in mark["segments"]:
        start = 0.0 if seg["type"] == "opening" else seg["start"]
        end = duration if seg["type"] == "ending" else seg["end"]
        if end <= start or end - start < MIN_SEGMENT_SECONDS:
            continue
        if seg["type"] == "ending":
            if start > duration - END_GUARD_SECONDS:
                continue
        elif end > duration - END_GUARD_SECONDS:
            continue
        resolved.append({"type": seg["type"], "start": start, "end": end})
    resolved.sort(key=lambda s: s["start"])
    out: list[dict] = []
    for seg in resolved:
        if out and seg["start"] - out[-1]["end"] < MIN_SEGMENT_GAP_SECONDS:
            continue
        out.append(seg)
    return out


def hls_ranges(guid: str) -> list[tuple[float, float]]:
    """用户显式开启时，返回需在 HLS 清单中跳过的 middle 时间区间。

    opening/ending 交给飞牛原生 play_config，不在这里重复处理。默认空列表，
    对 v0.2.42 播放路径零影响。
    """
    mark = get(guid)
    if not mark["custom_hls_skip"] or not mark["enabled"] \
            or not mark["auto_skip_segments"]:
        return []
    return [(s["start"], s["end"]) for s in mark["segments"]
            if s["type"] == "middle" and s["end"] > s["start"]]


def find_active_segment(guid: str, position: float, duration: float) -> dict | None:
    try:
        position = float(position)
    except (TypeError, ValueError):
        return None
    for seg in resolve_segments(guid, duration):
        if seg["start"] <= position < seg["end"] - 1:
            return seg
    return None
