"""Lazy T4 detail and playback lifecycle, independent of the host socket."""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict

import t4


class T4Service:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self._locks = {}
        self._fresh = OrderedDict()
        self._playbacks = OrderedDict()
        self._play_locks = {}
        self._sessions = OrderedDict()

    def source(self, source_id: str) -> dict:
        source = next((s for s in self.config.all_sources()
                       if s["id"] == source_id and s.get("kind") == "t4"), None)
        if not source or not source.get("enabled"):
            raise t4.T4Error("T4 来源已移除或停用")
        return source

    async def ensure_detail(self, client, guid: str, *, force=False):
        brief = self.store.get_title_by_guid(guid)
        if not brief or brief.get("locator", {}).get("kind") != "t4":
            return brief
        source = self.source(brief["locator"]["source_id"])
        now = time.monotonic()
        if not force and now - self._fresh.get(guid, float("-inf")) < 300:
            return brief
        entry = self._locks.setdefault(guid, [asyncio.Lock(), 0])
        entry[1] += 1
        lock = entry[0]
        try:
            async with lock:
                brief = self.store.get_title_by_guid(guid)
                if not force and time.monotonic() - self._fresh.get(
                    guid, float("-inf")) < 300:
                    return brief
                try:
                    title = await t4.detail(client, source, brief["locator"], fallback=brief)
                except t4.T4Error as exc:
                    # A runtime-local Sidecar ID cannot be recovered safely by title search.
                    if str(brief["locator"]["vod_id"]).startswith("t4_"):
                        raise t4.T4Error(
                            str(exc) + "；若 T4 已重启，请重新搜索，临时标识不能按片名恢复") from None
                    raise
                brief = self.store.replace_t4_detail(guid, title)
                for key in list(self._playbacks):
                    if key[0] == guid:
                        self._playbacks.pop(key)
                if not self.store.tv_episode_guids(guid):
                    raise t4.T4Error("T4 分集没有唯一名称，无法安全选集")
                self._fresh[guid] = time.monotonic()
                self._fresh.move_to_end(guid)
                while len(self._fresh) > 512:
                    self._fresh.popitem(last=False)
                return brief
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(guid, None)

    async def resolve(self, client, target: dict) -> t4.Playback:
        guid = target["online_guid"]
        await self.ensure_detail(client, guid)
        source = self.source(self.store.get_title_by_guid(guid)["locator"]["source_id"])
        key = (guid, target.get("episode_key") or str(target.get("index", 0)),
               target.get("line", ""))
        entry = self._play_locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                hit = self._playbacks.get(key)
                if hit and time.monotonic() - hit[0] < 30:
                    return hit[1]
                result = await self._player(client, source, target)
                self._playbacks[key] = time.monotonic(), result
                self._playbacks.move_to_end(key)
                while len(self._playbacks) > 128:
                    self._playbacks.popitem(last=False)
                return result
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._play_locks.pop(key, None)

    def invalidate_playback(self, target: dict):
        key = (target["online_guid"], target.get("episode_key") or
               str(target.get("index", 0)), target.get("line", ""))
        self._playbacks.pop(key, None)

    def cached_playback(self, target: dict):
        key = (target["online_guid"], target.get("episode_key") or
               str(target.get("index", 0)), target.get("line", ""))
        hit = self._playbacks.get(key)
        return hit[1] if hit else None

    def cached_line_for_url(self, target: dict, url: str) -> str | None:
        episode = target.get("episode_key") or str(target.get("index", 0))
        lines = {key[2] for key, (_, result) in self._playbacks.items()
                 if key[:2] == (target["online_guid"], episode) and result.url == url}
        # The URL selects a line only; resolve() still enforces freshness.
        return next(iter(lines)) if len(lines) == 1 else None

    def owns_playback_url(self, url: str) -> bool:
        return any(result.url == url for _, result in self._playbacks.values())

    def create_session(self, media_guid: str, target: dict) -> str:
        now = time.monotonic()
        while self._sessions and (len(self._sessions) >= 128 or
                                 next(iter(self._sessions.values()))[0] <= now):
            self._sessions.popitem(last=False)
        key = secrets.token_urlsafe(18)
        self._sessions[key] = now + 6 * 3600, media_guid, dict(target)
        return key

    def session_target(self, media_guid: str, key: str) -> dict | None:
        hit = self._sessions.get(key)
        if not hit or hit[0] <= time.monotonic():
            self._sessions.pop(key, None)
            return None
        if hit[1] != media_guid:
            return None
        self._sessions.move_to_end(key)
        self._sessions[key] = time.monotonic() + 6 * 3600, media_guid, hit[2]
        return dict(hit[2])

    def set_session_target(self, media_guid: str, key: str, target: dict):
        if self.session_target(media_guid, key) is not None:
            self._sessions[key] = time.monotonic() + 6 * 3600, media_guid, dict(target)

    def close_session(self, media_guid: str, key: str):
        if self.session_target(media_guid, key) is not None:
            self._sessions.pop(key, None)

    async def _player(self, client, source: dict, target: dict) -> t4.Playback:
        guid = target["online_guid"]
        for attempt in range(2):
            eg = target.get("ep_guid")
            if target.get("episode_key"):
                eg = self.store.t4_episode_guid(guid, target["episode_key"])
            current = self.store.get_episode_target(eg, target.get("line")) if eg else \
                self.store.resolve_episode(guid, target.get("line"), target.get("index", 0))
            if not current:
                raise t4.T4Error("该分集或线路已变化，请重新打开详情")
            try:
                return await t4.player(client, source, current["url"], current["play_from"])
            except t4.T4Error as exc:
                if attempt or not exc.refresh_detail:
                    raise
                await self.ensure_detail(client, guid, force=True)
