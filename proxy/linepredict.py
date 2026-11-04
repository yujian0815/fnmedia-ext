"""本片线路预测（linepredict）

取代旧的全局源测速 speedbench：源级速度代表不了每部片各自的分片 CDN，
因此不再做“全站定时测速→全局排名”，改为对【具体某一集】做轻量探活：

- 浏览海报 / 搜索结果时，对首屏可见影片的少数候选线做一次轻量探测
  （只抓播放清单 / Range 首字节，不转码不转存），限并发、短缓存；
- 探测出的本片最快线写入记忆，点播放时直接零竞速起播，真正做到秒播；
- 播放时的 per-title 竞速（app._race_fastest）仍是最终裁决，二者共用记忆。

仅保存在内存（重启清空，随后由浏览预热自然回填），不写盘、不打全站。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

# ep_key -> (ts, line, latency_seconds)
_FAST: "OrderedDict[str, tuple[float, str, float]]" = OrderedDict()
_LOCK = threading.Lock()
_TTL = 30 * 60          # 本片最快线记忆 30 分钟（预热与播放竞速共用）
_MAX = 4000             # 最多记忆的集数，超出淘汰最旧

# 正在预热的 ep_key，避免同片重复打源
_INFLIGHT: set[str] = set()
# 预热并发闸（由 app 注入事件循环后实际限并发，这里只做去重与轻量计数）
_INFLIGHT_LOCK = threading.Lock()


def ep_key(og: str, index: int) -> str:
    return f"{og}|{int(index or 0)}"


# 慢档阈值：实测延迟 ≥ 该值视为“不够秒播”，允许后续重探纠正（首探时绑定尚未
# 拿全线路、只探到慢线的情况）。
SLOW_SEC = 1.5
_RETRY_AFTER = 90.0


def remember(ep_key: str, line: str, latency: float) -> None:
    """记录某集实测/预热最快线。

    保留该集延迟最低的线：只有新结果更快，或旧结果是慢档且新线不同（明显更快）时
    才覆盖，避免网络瞬时抖动把已经验证的快线换成慢线。"""
    latency = float(latency)
    with _LOCK:
        old = _FAST.get(ep_key)
        if old is not None:
            _ts, old_line, old_lat = old
            better = latency < old_lat - 0.05
            slow_switch = old_lat >= SLOW_SEC and line != old_line and latency < old_lat - 0.3
            if not (better or slow_switch):
                _FAST.move_to_end(ep_key)
                return
        _FAST[ep_key] = (time.time(), line, latency)
        _FAST.move_to_end(ep_key)
        while len(_FAST) > _MAX:
            _FAST.popitem(last=False)


def ready_or_reprobe(ep_key: str) -> bool:
    """该集是否已有“够快”的预测、无需再探。

    快/中档直接就绪；慢档在重试冷却期内也视为就绪（避免每次浏览都重探），冷却
    过后允许再探一次以拿到后绑定进来的更快线。"""
    with _LOCK:
        rec = _FAST.get(ep_key)
        if not rec:
            return False
        ts, _line, lat = rec
        if time.time() - ts >= _TTL:
            _FAST.pop(ep_key, None)
            return False
        if lat < SLOW_SEC:
            return True
        return (time.time() - ts) < _RETRY_AFTER


def fastest(ep_key: str) -> str | None:
    """取某集记忆的最快线（过期/无记录返回 None）。"""
    with _LOCK:
        rec = _FAST.get(ep_key)
        if not rec:
            return None
        ts, line, _lat = rec
        if time.time() - ts >= _TTL:
            _FAST.pop(ep_key, None)
            return None
        _FAST.move_to_end(ep_key)
        return line


def latency(ep_key: str) -> float | None:
    with _LOCK:
        rec = _FAST.get(ep_key)
        if not rec:
            return None
        ts, _line, lat = rec
        if time.time() - ts >= _TTL:
            _FAST.pop(ep_key, None)
            return None
        return lat


def grade(ep_key: str) -> int | None:
    """海报角标档位：1=快(首字节<0.8s) 2=中(<2.5s) 3=慢/勉强(>=2.5s)；无记录 None。"""
    lat = latency(ep_key)
    if lat is None:
        return None
    if lat < 0.8:
        return 1
    if lat < 2.5:
        return 2
    return 3


def invalidate(ep_key: str) -> None:
    with _LOCK:
        _FAST.pop(ep_key, None)


def try_mark_inflight(ep_key: str) -> bool:
    """尝试登记一个在途预热；已在途返回 False（调用方应跳过）。"""
    with _INFLIGHT_LOCK:
        if ep_key in _INFLIGHT:
            return False
        _INFLIGHT.add(ep_key)
        return True


def clear_inflight(ep_key: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(ep_key)


def inflight_count() -> int:
    with _INFLIGHT_LOCK:
        return len(_INFLIGHT)


def snapshot(limit: int = 40) -> list[dict]:
    """最近预测结果（管理页实时查看）。"""
    now = time.time()
    with _LOCK:
        items = list(_FAST.items())[-limit:]
    out = []
    for key, (ts, line, lat) in reversed(items):
        if now - ts >= _TTL:
            continue
        og, _, index = key.partition("|")
        out.append({"og": og, "index": int(index or 0),
                    "line": line, "latency": round(lat, 3),
                    "grade": grade(key), "age": round(now - ts, 1)})
    return out


def count() -> int:
    with _LOCK:
        return len(_FAST)


def reset() -> None:
    with _LOCK:
        _FAST.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()
