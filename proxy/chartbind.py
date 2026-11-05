"""榜单（热门）片绑定结果持久化（方案 B）。

热门库卡片只有片名/海报、没有播放线路，resolve_chart 全网搜源后才得到带线路
的 brief。默认这些绑定只活在进程内存（store），重启即丢、要重新搜源（3.5–9s）。

本模块把 chart guid -> 已绑定 brief 单独落盘到 state/chart_bindings.json：
  - 独立文件，不受 store titles.json 的 4MB / 条数 LRU 淘汰影响；
  - 带 TTL：过期不直接删除，而是标记需懒校验（线路签名 URL 可能失效），
    命中时先恢复，播放链若发现死链仍会触发重新 resolve；
  - 防抖落盘，避免高频绑定打爆磁盘；
  - 只存 chart guid（32 位 hex）对应的 brief，体量可控。

开关：config["prewarm_mode"] = "persist"（方案B）或 "realtime"（方案A，默认）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state" / "chart_bindings.json"

# 绑定保鲜期：超过后视为“需懒校验”，恢复进内存但不强制信任，播放失败会重搜。
BIND_TTL = 6 * 3600
# 单文件条数上限（防止无限增长；超出丢最旧）
_MAX_ENTRIES = 2000
_FLUSH_DEBOUNCE = 5.0


class ChartBindStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # guid -> {"ts": float, "brief": dict}
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._flush_at = 0.0
        self._flusher: threading.Timer | None = None
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(STATE.read_text(encoding="utf-8"))
                data = raw.get("bindings") if isinstance(raw, dict) else None
                if isinstance(data, dict):
                    self._data = {
                        str(k): v for k, v in data.items()
                        if isinstance(v, dict) and v.get("brief")
                    }
            except Exception:
                self._data = {}

    def _persist_locked(self) -> None:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            payload = {"updated_at": time.time(), "bindings": self._data}
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            tmp.replace(STATE)
        except Exception:
            pass

    def _schedule_flush_locked(self) -> None:
        self._dirty = True
        now = time.monotonic()
        if self._flusher is not None:
            self._flush_at = now + _FLUSH_DEBOUNCE  # 顺延，合并写
            return
        self._flush_at = now + _FLUSH_DEBOUNCE

        def _fire():
            with self._lock:
                self._flusher = None
                if not self._dirty:
                    return
                self._persist_locked()
                self._dirty = False
            # 顺延期间又变脏：再起一轮
            with self._lock:
                if self._dirty:
                    self._schedule_flush_locked()

        t = threading.Timer(_FLUSH_DEBOUNCE, _fire)
        t.daemon = True
        self._flusher = t
        t.start()

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._persist_locked()
                self._dirty = False

    # ---------- 读写 ----------
    def remember(self, guid: str, brief: dict) -> None:
        if not guid or not isinstance(brief, dict):
            return
        with self._lock:
            self._data[guid] = {"ts": time.time(), "brief": brief}
            # 超容量丢最旧（dict 保序）
            if len(self._data) > _MAX_ENTRIES:
                for old in list(self._data)[:len(self._data) - _MAX_ENTRIES]:
                    self._data.pop(old, None)
            self._schedule_flush_locked()

    def get(self, guid: str) -> dict | None:
        """返回绑定 brief；不存在返回 None。是否过期由 is_fresh 区分。"""
        with self._lock:
            rec = self._data.get(guid)
            return dict(rec["brief"]) if rec else None

    def is_fresh(self, guid: str) -> bool:
        with self._lock:
            rec = self._data.get(guid)
            if not rec:
                return False
            return (time.time() - float(rec.get("ts") or 0)) < BIND_TTL

    def has(self, guid: str) -> bool:
        with self._lock:
            return guid in self._data

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def forget(self, guid: str) -> None:
        with self._lock:
            if self._data.pop(guid, None) is not None:
                self._schedule_flush_locked()

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._dirty = True
            self._persist_locked()
            self._dirty = False
        try:
            STATE.unlink(missing_ok=True)
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            fresh = sum(1 for r in self._data.values()
                        if (now - float(r.get("ts") or 0)) < BIND_TTL)
            return {"entries": len(self._data), "fresh": fresh,
                    "stale": len(self._data) - fresh, "ttl_sec": BIND_TTL}


# 进程内单例
bind_store = ChartBindStore()
