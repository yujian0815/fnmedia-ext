#!/usr/bin/env python3
"""在线播放进度（继续观看）。state/history.json。

记录由前端高频 POST /play/record 驱动，内存更新 + 限频落盘；
GET /play/list 时与官方本地历史合并，让在线条目出现在「继续观看」。

多用户（issue #5）：在线历史按飞牛账号分区，避免一人看片全家共享进度。
当前账号通过 ``set_current_user``（每个请求由中间件按 token 解析后设置）传递；
未识别身份（本地/无 token/解析失败）落到全局分区 ""，行为与旧版一致。
升级时旧的全局记录会在某个已识别账号首次访问时被其「继承」一次，之后各账号独立。
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "history.json"
_LOCK = threading.RLock()
_FLUSH_INTERVAL = 3.0
_MAX = 500

# 每个请求设置的当前飞牛账号 guid；"" = 未识别，使用全局分区
_current_user: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "fnmedia_history_user", default="")
_current_admin: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "fnmedia_history_admin", default=False)


def set_current_user(guid: str | None, is_admin: bool = False):
    """设置当前请求的账号分区，返回 (user_token, admin_token) 以便 reset。"""
    return (_current_user.set(str(guid or "")),
            _current_admin.set(bool(is_admin)))


def reset_current_user(tokens) -> None:
    try:
        _current_admin.reset(tokens[1])
        _current_user.reset(tokens[0])
    except (ValueError, LookupError, IndexError, TypeError):
        pass


def current_user() -> str:
    return _current_user.get() or ""


class History:
    def __init__(self):
        # {user_guid: {item_guid: rec}}；"" 是全局分区（未识别身份 / 旧数据）
        self._users: dict[str, dict[str, dict]] = {}
        # 已从旧全局分区继承过历史的账号（持久化，避免每次重启重复继承）
        self._adopted: set[str] = set()
        self._last_flush = 0.0
        self._dirty = False
        self._load()

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
        except Exception:
            j = {}
        if "users" in j and isinstance(j.get("users"), dict):
            self._users = {u: dict(v) for u, v in j["users"].items()
                           if isinstance(v, dict) and not u.startswith("__adopted_")}
            # 已继承标记单独恢复（持久化在顶层 adopted 列表）
            self._adopted = set(j.get("adopted", []) or [])
        else:
            # 旧版扁平结构 {"records": {...}} → 归入全局分区
            recs = j.get("records", {})
            self._users = {"": dict(recs) if isinstance(recs, dict) else {}}
            self._adopted = set()

    def _bucket(self, user: str | None = None) -> dict[str, dict]:
        u = current_user() if user is None else str(user or "")
        return self._users.setdefault(u, {})

    def _flush_locked(self, force: bool = False):
        now = time.time()
        if not self._dirty:
            return
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            users: dict[str, dict] = {}
            for u, recs in self._users.items():
                if not recs:
                    continue
                if len(recs) > _MAX:
                    keep = sorted(recs.values(),
                                  key=lambda r: r.get("updated_at", 0),
                                  reverse=True)[:_MAX]
                    recs = {r["item_guid"]: r for r in keep}
                    self._users[u] = recs
                users[u] = recs
            tmp = _STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"users": users, "adopted": sorted(self._adopted)},
                ensure_ascii=False))
            tmp.replace(_STATE)
            self._dirty = False
            self._last_flush = now
        except Exception:
            pass

    def flush(self):
        with _LOCK:
            self._flush_locked(force=True)

    def _adopt_global_if_needed(self, u: str, bucket: dict):
        """升级迁移：旧版历史是全家共享的全局记录。v0.2.48 起按账号隔离，
        只让管理员在其首次访问时把旧全局记录继承一次（管理员是原来的主要使用者），
        子账号从空白开始、不再互相串进度。"""
        if u and not bucket and _current_admin.get() and u not in self._adopted:
            g = self._users.get("", {})
            if g:
                bucket.update(json.loads(json.dumps(g)))
                self._adopted.add(u)
                self._dirty = True

    def record(self, body: dict, user: str | None = None) -> bool:
        """处理一次 play/record 上报，返回是否为我们的在线条目。"""
        ig = body.get("item_guid", "")
        if not ig:
            return False
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._bucket(u)
            self._adopt_global_if_needed(u, bucket)
            prev = bucket.get(ig, {})
            ts = int(float(body.get("ts", 0) or 0))
            duration = int(float(body.get("duration", 0) or 0))
            rec = {
                "item_guid": ig,
                "media_guid": body.get("media_guid", prev.get("media_guid", "")),
                "video_guid": body.get("video_guid", prev.get("video_guid", "")),
                "audio_guid": body.get("audio_guid", prev.get("audio_guid", "")),
                "subtitle_guid": body.get("subtitle_guid",
                                          prev.get("subtitle_guid", "")),
                "resolution": body.get("resolution", prev.get("resolution", "")),
                "ts": max(ts, int(prev.get("ts", 0))),
                "duration": duration or int(prev.get("duration", 0)),
                "updated_at": time.time(),
            }
            # 只有上报进度回退（重新播放）才允许变小：以更大 ts 为准，除非显式重置
            if ts < int(prev.get("ts", 0)) and body.get("reset"):
                rec["ts"] = ts
            bucket[ig] = rec
            self._dirty = True
            self._flush_locked()
        return True

    def set_position(self, item_guid: str, ts: int, duration: int = 0,
                     user: str | None = None):
        with _LOCK:
            bucket = self._bucket(user)
            prev = bucket.get(item_guid, {})
            prev.update({"item_guid": item_guid, "ts": int(ts),
                         "updated_at": time.time()})
            if duration:
                prev["duration"] = int(duration)
            bucket[item_guid] = prev
            self._dirty = True
            self._flush_locked(force=True)

    def get(self, item_guid: str, user: str | None = None) -> dict | None:
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._users.get(u, {})
            if u:
                self._adopt_global_if_needed(u, self._bucket(u))
                bucket = self._users.get(u, {})
            return dict(bucket.get(item_guid) or {}) or None

    def all(self, user: str | None = None) -> list[dict]:
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            self._flush_locked(force=True)
            return [dict(r) for r in bucket.values()]

    def resume_episode(self, ep_guids: list[str],
                       user: str | None = None, *,
                       latest_interaction: bool = False) -> tuple[str | None, int]:
        """给定一部剧按顺序的全部集 guid，决定"该播哪一集"。

        - 从没看过 → 第一集（ts=0）
        - 有未看完的集（进度<95%）→ 默认按【集序】续播靠后的未看完集（CMS 原语义）；
          latest_interaction=True 时改为按【最近一次交互】选（T4 回看语义）。
        - 看过的都看完了 → 下一集（ts=0）；全看完 → 最后一集
        返回 (ep_guid 或 None 表示无记录, ts)。
        """
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            # 有序地只保留有记录的集
            watched = [(i, bucket[g]) for i, g in enumerate(ep_guids)
                       if g in bucket]
            if not watched:
                return (ep_guids[0] if ep_guids else None), 0
            # 回看较早一集时，也要恢复到最近一次交互的进度
            unfinished = [(i, r) for i, r in watched
                          if not self._finished(r)]
            if unfinished:
                # CMS 默认按集序续播；仅 T4 回看(latest_interaction)按最近交互时间。
                i, r = max(unfinished, key=lambda x: (
                    x[1].get("updated_at", 0) if latest_interaction else x[0]))
                return ep_guids[i], int(r.get("ts", 0) or 0)
            # 都看完了 → 下一集
            last_i = max(i for i, _ in watched)
            nxt = min(last_i + 1, len(ep_guids) - 1)
            return ep_guids[nxt], 0

    @staticmethod
    def _finished(r: dict) -> bool:
        ts = int(r.get("ts", 0) or 0)
        dur = int(r.get("duration", 0) or 0)
        return bool(dur) and ts >= dur * 0.95

    def delete(self, item_guids, user: str | None = None) -> int:
        ids = item_guids if isinstance(item_guids, (list, tuple)) else [item_guids]
        n = 0
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._bucket(u)
            for g in ids:
                if g in bucket:
                    del bucket[g]
                    n += 1
            if n:
                self._dirty = True
                self._flush_locked(force=True)
        return n


history = History()
