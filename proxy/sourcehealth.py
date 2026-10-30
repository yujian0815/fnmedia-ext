#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线源健康画像：按源（source key）统计成功率/延迟/连续失败，用于排序与自动降权。

设计目标：轻量、无外部依赖、进程内热路径零 IO（统计落盘由后台/低频触发）。
- record(key, ok, latency) 记录一次探测/播放结果；
- weight(key) 给出排序权重（越小越靠前）：健康且快的源在前，死源沉底；
- usable(key) 在“死源冷却窗”内返回 False，供搜索/候选短路跳过高失败源；
- 数据持久化到 state/source_health.json，重启不丢历史画像。

这是通用运维能力（不含任何凭据/私有源信息），公开构建保留。
"""
from __future__ import annotations

import json
import os
import threading
import time

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "source_health.json")
_LOCK = threading.RLock()

# 连续失败达到阈值进入“死亡冷却”：该窗口内该源降权/可被跳过
DEAD_FAILS = 3
DEAD_COOLDOWN = 15 * 60          # 判死后冷却 15 分钟，之后给一次试探
DEGRADED_FAIL_RATE = 0.6         # 样本足够时，失败率高于此判差
MIN_SAMPLES = 6                  # 低于此样本不做失败率判级，避免偶发误杀
SLOW_LATENCY = 12.0              # 平均延迟超过此秒视为慢源
_MAX_KEYS = 2000


def _blank() -> dict:
    return {"n": 0, "ok": 0, "lat": 0.0, "fails": 0,
            "last_ok": 0.0, "last_fail": 0.0, "dead_until": 0.0}


def _load() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_STATS: dict[str, dict] = _load()


def _save_locked() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATS, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def source_key_of(line: str | None, picked: dict | None = None) -> str:
    """从线路名（"src::显示名"）或 picked 记录取稳定的源 key。"""
    if picked:
        for k in ("source", "src", "source_key", "spider_src", "provider_key",
                  "play_from"):
            v = picked.get(k)
            if v:
                return str(v)
    if line and "::" in line:
        return line.split("::", 1)[0]
    return line or ""


def record(key: str, ok: bool, latency: float = 0.0) -> None:
    key = (key or "").strip()
    if not key:
        return
    now = time.time()
    with _LOCK:
        s = _STATS.get(key)
        if s is None:
            if len(_STATS) >= _MAX_KEYS:
                # 超容量：淘汰最久没动静的
                old = min(_STATS, key=lambda k: max(
                    _STATS[k].get("last_ok", 0), _STATS[k].get("last_fail", 0)))
                _STATS.pop(old, None)
            s = _blank()
            _STATS[key] = s
        s["n"] += 1
        if latency and latency > 0:
            s["lat"] += float(latency)
        if ok:
            s["ok"] += 1
            s["fails"] = 0
            s["last_ok"] = now
            s["dead_until"] = 0.0
        else:
            s["fails"] = int(s.get("fails", 0)) + 1
            s["last_fail"] = now
            if s["fails"] >= DEAD_FAILS:
                s["dead_until"] = now + DEAD_COOLDOWN


def stats(key: str) -> dict:
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return {}
        n = max(1, s.get("n", 0))
        return {
            "key": key,
            "samples": s.get("n", 0),
            "success_rate": round(s.get("ok", 0) / n, 3),
            "avg_latency": round(s.get("lat", 0.0) / n, 2),
            "fails": s.get("fails", 0),
            "last_ok": s.get("last_ok", 0),
            "last_fail": s.get("last_fail", 0),
            "dead_until": s.get("dead_until", 0),
        }


def is_dead(key: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return False
        if now < s.get("dead_until", 0):
            return True
        # 冷却已过：清除死亡标记，放行一次试探（但保留长期失败率供排序）
        if s.get("dead_until"):
            s["dead_until"] = 0.0
        return False


def usable(key: str, now: float | None = None) -> bool:
    """死源冷却窗内返回 False（供搜索阶段短路跳过）。"""
    return not is_dead(key, now)


def _grade(s: dict, now: float) -> int:
    """0 健康 / 1 慢 / 2 高失败率 / 3 死亡冷却。"""
    if now < s.get("dead_until", 0):
        return 3
    n = s.get("n", 0)
    if n >= MIN_SAMPLES:
        if (s.get("ok", 0) / max(1, n)) < (1 - DEGRADED_FAIL_RATE):
            return 2
    if n >= MIN_SAMPLES and (s.get("lat", 0.0) / max(1, n)) > SLOW_LATENCY:
        return 1
    return 0


def weight(key: str, now: float | None = None) -> tuple:
    """排序权重，元组越小越优先：健康等级 → 平均延迟 → 成功率。无画像给中性值。"""
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return (1, 5.0, 1.0)  # 未知源给中间档，给一次机会
        grade = _grade(s, now)
        n = max(1, s.get("n", 0))
        avg = s.get("lat", 0.0) / n
        rate = s.get("ok", 0) / n
        # 死亡冷却最末；高失败率次之；慢源次之；健康按延迟/成功率细排
        return (grade, round(avg, 2), round(1 - rate, 3))


def ordered(keys, now: float | None = None) -> list:
    """按健康权重排序一组源 key（稳定排序，未知源保持相对靠中）。"""
    now = now if now is not None else time.time()
    return sorted(keys, key=lambda k: weight(k, now))


def display_grade(key: str, now: float | None = None):
    """海报/UI 角标用等级：无任何统计样本时返回 None（不显示点），
    避免给从未探测过的新源误标黄/红点。返回 0~3 或 None。"""
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s or s.get("n", 0) == 0:
            return None
        return _grade(s, now)


def snapshot() -> dict:
    """管理页用：全部源画像。"""
    now = time.time()
    with _LOCK:
        out = {}
        for k in _STATS:
            st = stats(k)
            st["grade"] = _grade(_STATS[k], now)
            out[k] = st
        return out


def reset(key: str | None = None) -> None:
    with _LOCK:
        if key:
            _STATS.pop(key, None)
        else:
            _STATS.clear()
        _save_locked()


def flush() -> None:
    with _LOCK:
        _save_locked()
