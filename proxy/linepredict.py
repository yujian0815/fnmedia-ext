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

import json
import threading
import time
from collections import OrderedDict
from pathlib import Path

# ep_key -> (ts, line, latency_seconds)
_FAST: "OrderedDict[str, tuple[float, str, float]]" = OrderedDict()
_LOCK = threading.Lock()
_TTL = 30 * 60          # 本片最快线记忆 30 分钟（预热与播放竞速共用）
_MAX = 4000             # 最多记忆的集数，超出淘汰最旧

# ---- 冠军线记忆持久化（方案B，仅 persist 模式启用）----
_STATE = Path(__file__).resolve().parent.parent / "state" / "linepredict_fast.json"
_persist_enabled = False
_flush_timer: "threading.Timer | None" = None
_FLUSH_DEBOUNCE = 5.0
_dirty = False  # 仅在有新记忆/变更时才落盘，避免 Timer 永久空转
_FLUSH_LOCK = threading.Lock()  # 独立于 _LOCK，防止持锁重入死锁


def set_persist(enabled: bool) -> None:
    """开关冠军线记忆持久化；开启时先从磁盘恢复。"""
    global _persist_enabled
    _persist_enabled = bool(enabled)
    if _persist_enabled:
        load_persisted()


def load_persisted() -> int:
    """启动恢复冠军线记忆，返回恢复条数。"""
    try:
        raw = json.loads(_STATE.read_text(encoding="utf-8"))
        items = raw.get("fast") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return 0
        n = 0
        with _LOCK:
            for k, v in items.items():
                try:
                    ts, line, lat = v[0], str(v[1]), float(v[2])
                    # 过期记忆不恢复（避免重启后钉在已失效的线上）
                    if time.time() - float(ts) < _TTL:
                        _FAST[str(k)] = (float(ts), line, lat)
                        n += 1
                except Exception:
                    continue
        return n
    except Exception:
        return 0


def _persist_now() -> None:
    try:
        with _LOCK:
            snapshot = {k: list(v) for k, v in _FAST.items()}
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fast": snapshot}, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        tmp.replace(_STATE)
    except Exception:
        pass


def _schedule_flush() -> None:
    global _dirty, _flush_timer
    if not _persist_enabled:
        return
    # 用独立锁，避免 remember 已持 _LOCK 时再嵌套获取普通 Lock 造成自死锁
    with _FLUSH_LOCK:
        _dirty = True
        if _flush_timer is not None:
            return

        def _fire():
            global _flush_timer, _dirty
            with _FLUSH_LOCK:
                _flush_timer = None
                need = _dirty
                _dirty = False
            if need:
                _persist_now()

        t = threading.Timer(_FLUSH_DEBOUNCE, _fire)
        t.daemon = True
        _flush_timer = t
        t.start()


# 正在预热的 ep_key，避免同片重复打源
_INFLIGHT: set[str] = set()
# 预热并发闸（由 app 注入事件循环后实际限并发，这里只做去重与轻量计数）
_INFLIGHT_LOCK = threading.Lock()


def ep_key(og: str, index: int) -> str:
    return f"{og}|{int(index or 0)}"


# ===== 播放中实时分片速度表（按 集+线路 滑动） =====
# 用于“放到一半某条线持续变慢”时，在播放器下次刷新 media playlist 时静默
# 切到同片更快的线。只统计实际播放（seg 端点）产生的样本，TTL 很短（播放会话级）。
# key = "og|index|line" -> (最近时间戳, 最近若干分片耗时秒的列表)
_SEG: "OrderedDict[str, tuple[float, list[float]]]" = OrderedDict()
_SEG_LOCK = threading.Lock()
_SEG_WIN = 6                # 每条线只看最近 6 个分片
_SEG_TTL = 180.0            # 3 分钟没新样本即丢弃
_SEG_MAX = 3000
# 判定“当前线持续慢”的阈值：最近窗口中位数 ≥ 该秒数
SEG_SLOW_SEC = 2.2
# 备选线必须比当前线快出该比例才切（避免抖动来回切）
SEG_SWITCH_RATIO = 0.55
# 至少累计这么多个慢分片才允许切（避免偶发一两个慢片误切）
SEG_SLOW_MIN = 3
# 两次静默切线的最小间隔（每集），防频繁切换
_SWITCH_COOLDOWN = 25.0
_LAST_SWITCH: dict[str, float] = {}


def _seg_key(og: str, index: int, line: str) -> str:
    return f"{og}|{int(index or 0)}|{line}"


def record_seg(og: str, index: int, line: str, seconds: float) -> None:
    """记录一次实际播放分片的完整下载耗时（秒）。"""
    if not og or not line or seconds is None or seconds < 0:
        return
    key = _seg_key(og, index, line)
    now = time.time()
    with _SEG_LOCK:
        old = _SEG.get(key)
        samples = (old[1] if old else []) + [float(seconds)]
        samples = samples[-_SEG_WIN:]
        _SEG[key] = (now, samples)
        _SEG.move_to_end(key)
        while len(_SEG) > _SEG_MAX:
            _SEG.popitem(last=False)


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def pick_faster_line(og: str, index: int, cur_line: str) -> str | None:
    """播放中：若当前线最近分片持续慢，且另一条线有明显更快的新鲜样本，
    返回应切换到的线路名；否则返回 None。按 集 做切换冷却。"""
    if not og or not cur_line:
        return None
    now = time.time()
    cool_key = ep_key(og, index)
    with _SEG_LOCK:
        if now - _LAST_SWITCH.get(cool_key, 0) < _SWITCH_COOLDOWN:
            return None
        # 清理过期
        for k in list(_SEG.keys()):
            if now - _SEG[k][0] > _SEG_TTL:
                _SEG.pop(k, None)
        pref = f"{og}|{int(index or 0)}|"
        cur = _SEG.get(_seg_key(og, index, cur_line))
        if not cur:
            return None
        cur_v = cur[1]
        slow_n = sum(1 for x in cur_v if x >= SEG_SLOW_SEC)
        if len(cur_v) < SEG_SLOW_MIN or slow_n < SEG_SLOW_MIN:
            return None
        cur_med = _median(cur_v)
        if cur_med < SEG_SLOW_SEC:
            return None
        best_line, best_med = None, cur_med * SEG_SWITCH_RATIO
        for k, (_, v) in _SEG.items():
            if not k.startswith(pref):
                continue
            line = k[len(pref):]
            if line == cur_line or len(v) < 2:
                continue
            m = _median(v)
            # 备选线既要明显更快，其最近窗口也不能整体很慢
            if m <= best_med and m < SEG_SLOW_SEC:
                best_line, best_med = line, m
        if best_line:
            _LAST_SWITCH[cool_key] = now
        return best_line


def reset_seg_timing() -> None:
    with _SEG_LOCK:
        _SEG.clear()
        _LAST_SWITCH.clear()


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
        _schedule_flush()


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
    if _persist_enabled:
        _persist_now()
