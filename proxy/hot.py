#!/usr/bin/env python3
"""在线热门媒体库的后台刷新与本地缓存。

设计要点（方案 A）：
- 后台请求各资源站的最近更新详情列表，不阻塞飞牛影视首页；
- 成功时原子写入 state/hot.json；失败时继续使用旧缓存；
- 跨源按“规范化片名 + 年份”聚合，避免同一部片重复出现；
- 热门缓存中的完整 brief 会写入 store，保证详情、选集、播放链可直接复用。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import asdict

import httpx
from datetime import datetime
from pathlib import Path
from typing import Any

import providers

STATE = Path(__file__).resolve().parent.parent / "state" / "hot.json"
REFRESH_TIMEOUT = 180.0


def norm_title(title: str) -> str:
    """与搜索聚合一致的规范化标题。"""
    t = (title or "").strip().lower()
    t = re.sub(r"[\s·・:：,，.。!！?？'’\"\-—_()（）\[\]【】]+", "", t)
    t = re.sub(r"(19|20)\d{2}$", "", t)
    return t


def parse_time(value: str) -> float:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:len(fmt) + 2].strip(), fmt).timestamp()
        except ValueError:
            pass
    return 0.0


def playable_count(t: providers.OnlineTitle) -> int:
    return sum(1 for eps in t.sources.values() for e in eps if e.direct or e.resolve)


def direct_count(t: providers.OnlineTitle) -> int:
    return sum(1 for eps in t.sources.values() for e in eps if e.direct)


async def fetch_source_hot(client: httpx.AsyncClient, source: dict,
                           limit: int, pages: int = 2) -> list[providers.OnlineTitle]:
    """取一个资源站的最近更新详情。苹果 CMS 多数接口固定每页约 20 条。"""
    if not source.get("enabled", True) or source.get("kind") == "t4":
        return []
    out: list[providers.OnlineTitle] = []
    seen: set[tuple[str, str]] = set()
    for pg in range(1, max(1, int(pages)) + 1):
        if len(out) >= limit:
            break
        try:
            r = await client.get(
                source["api"],
                params={"ac": "detail", "pg": pg, "h": 24},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=12,
            )
            if r.status_code != 200:
                continue
            j = r.json()
        except Exception:
            continue
        if not isinstance(j, dict) or j.get("code") not in (1, "1"):
            continue
        page_items = j.get("list") or []
        if not page_items:
            break
        for v in page_items:
            if len(out) >= limit:
                break
            key = (str(source.get("id") or source.get("api")), str(v.get("vod_id")))
            if key in seen:
                continue
            seen.add(key)
            try:
                t = providers._map_vod(v, source)
                if t and t.title and playable_count(t) > 0:
                    out.append(t)
            except Exception:
                continue
        if len(page_items) < 10:
            break
    return out[:limit]


async def collect_hot(sources: list[dict], limit_per_source: int,
                      max_items: int, is_junk, pages_per_source: int = 2) -> dict[str, Any]:
    sem = asyncio.Semaphore(5)
    seen_url: set[str] = set()

    async def bounded(source):
        async with sem:
            return await fetch_source_hot(client, source, limit_per_source,
                                          pages_per_source)

    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        results = await asyncio.gather(*[bounded(s) for s in sources],
                                       return_exceptions=True)

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    provider_count = 0
    for result in results:
        if not isinstance(result, list) or not result:
            continue
        provider_count += 1
        for t in result:
            title = (t.title or "").strip()
            if not title or is_junk(title, t.remarks):
                continue
            key = (norm_title(title), (t.year or "").strip())
            g = groups.setdefault(key, {
                "rep": t, "members": [], "lines": {},
                "sources": set(), "providers": [], "score": 0.0,
                "updated_ts": 0.0, "direct": 0, "count": 0,
            })
            g["members"].append(t)
            g["sources"].add(t.provider)
            g["providers"].append(t.provider)
            g["direct"] += direct_count(t)
            g["count"] += playable_count(t)
            g["updated_ts"] = max(g["updated_ts"], parse_time(t.updated))
            try:
                g["score"] = max(g["score"], float(t.score or 0))
            except (TypeError, ValueError):
                pass
            for lname, eps in t.sources.items():
                play = [e for e in eps if e.direct or e.resolve]
                if not play:
                    continue
                lkey = f"{t.provider}::{lname}"
                line = []
                for e in play:
                    if e.url in seen_url:
                        continue
                    seen_url.add(e.url)
                    line.append(asdict(e))
                if not line:
                    continue
                cur = g["lines"].get(lkey)
                nd = sum(1 for e in line if e.get("direct"))
                cur_direct = sum(1 for e in (cur or []) if e.get("direct"))
                if cur is None or nd > cur_direct or (nd == cur_direct and len(line) > len(cur)):
                    g["lines"][lkey] = line

    ordered = sorted(
        groups.values(),
        key=lambda g: (
            len(g["sources"]),
            g["direct"] > 0,
            g["updated_ts"],
            g["direct"],
            g["count"],
            g["score"],
        ),
        reverse=True,
    )

    briefs: list[dict] = []
    now = time.time()
    for rank, g in enumerate(ordered[:max_items], 1):
        rep = g["rep"]
        brief = {
            "guid": "",
            "online_id": rep.online_id,
            "title": rep.title,
            "type_name": rep.type_name,
            "year": rep.year,
            "poster": rep.poster,
            "overview": rep.overview,
            "remarks": rep.remarks,
            "score": rep.score,
            "actor": rep.actor,
            "director": rep.director,
            "area": rep.area,
            "updated": rep.updated,
            "lines": g["lines"],
            "hot": {
                "rank": rank,
                "source_count": len(g["sources"]),
                "direct_count": g["direct"],
                "updated_ts": g["updated_ts"],
                "updated": rep.updated,
                "score": g["score"],
            },
        }
        # 与搜索注入一致：稳定 md5("on_<provider>_<vodid>")。聚合时以代表源为身份，
        # 后续 store 会保存全部线路。
        from store import make_guid
        brief["guid"] = make_guid(rep)
        # 最近更新没有时间时保持一个稳定的衰减排序值。
        if not brief["hot"]["updated_ts"]:
            brief["hot"]["updated_ts"] = now - rank
        briefs.append(brief)

    return {
        "updated_at": int(now),
        "updated_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "provider_ok": provider_count,
        "total_sources": len(sources),
        "count": len(briefs),
        "items": briefs,
        "last_error": "",
    }


class HotCache:
    def __init__(self):
        self._lock = threading.RLock()
        # _items 是只读快照：刷新时整体替换引用，绝不在原地修改，
        # 因此列表请求可直接读引用而无需对数百条 brief 做整包深拷贝。
        self._meta: dict[str, Any] = {"updated_at": 0, "updated_at_text": "",
                                      "provider_ok": 0, "total_sources": 0,
                                      "last_error": ""}
        self._items: list[dict] = []
        self._refreshing = False
        self._task: asyncio.Future | None = None
        self._index: dict[str, dict] = {}
        self.load()

    def load(self):
        with self._lock:
            try:
                j = json.loads(STATE.read_text())
                if isinstance(j, dict) and isinstance(j.get("items"), list):
                    self._apply(j)
                    return
            except Exception:
                pass
            self._meta = {"updated_at": 0, "updated_at_text": "", "provider_ok": 0,
                          "total_sources": 0, "last_error": "尚未刷新"}
            self._items = []
            self._index = {}

    def _apply(self, data: dict[str, Any]):
        self._items = list(data.get("items") or [])
        self._index = {b.get("guid"): b for b in self._items if b.get("guid")}
        self._meta = {k: data.get(k, "") for k in
                      ("updated_at", "updated_at_text", "provider_ok",
                       "total_sources", "last_error")}

    def meta(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._meta)

    def get(self) -> dict[str, Any]:
        with self._lock:
            d = dict(self._meta)
            d["items"] = self._items
            d["count"] = len(self._items)
            return d

    def items(self) -> list[dict]:
        with self._lock:
            return self._items

    def get_guid(self, guid: str) -> dict | None:
        with self._lock:
            return self._index.get(guid)

    def posters(self, limit: int = 4) -> list[str]:
        out = []
        for b in self.items():
            if b.get("poster"):
                out.append(b["poster"])
            if len(out) >= limit:
                break
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(config_hot_enabled()),
                "refreshing": self._refreshing,
                "updated_at": self._meta.get("updated_at", 0),
                "updated_at_text": self._meta.get("updated_at_text", ""),
                "count": len(self._items),
                "provider_ok": self._meta.get("provider_ok", 0),
                "total_sources": self._meta.get("total_sources", 0),
                "last_error": self._meta.get("last_error", ""),
            }

    def stale(self, interval: float, grace: float = 0.0) -> bool:
        """缓存是否已超过 interval(+grace) 秒。无缓存视为过期。"""
        with self._lock:
            ts = float(self._meta.get("updated_at") or 0)
        if not ts:
            return True
        return (time.time() - ts) >= max(0.0, interval - grace)

    def maybe_refresh_in_background(self, store_obj=None, min_age: float = 900):
        """过期则在后台触发一次刷新（stale-while-revalidate）。

        页面请求只读缓存、立即返回；这里只负责“顺手”安排一次后台更新，
        min_age 限制两次自动刷新的最小间隔，避免连续翻页打爆资源站。
        """
        if self._refreshing:
            return False
        with self._lock:
            ts = float(self._meta.get("updated_at") or 0)
            age = time.time() - ts if ts else 1e18
        if age < min_age:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        async def _run():
            try:
                await asyncio.wait_for(self.refresh(store_obj, force=True),
                                       timeout=REFRESH_TIMEOUT)
            except Exception:
                pass

        loop.create_task(_run())
        return True

    def _persist(self, data: dict[str, Any]):
        tmp = STATE.with_suffix(".tmp")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        # 大缓存用紧凑 JSON 落盘，体积可下降 15-25%。
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(STATE)

    async def refresh(self, store_obj=None, force: bool = False) -> dict[str, Any]:
        from config import config
        if self._refreshing and not force:
            return self.status()
        loop = asyncio.get_running_loop()
        if self._task and not self._task.done() and not force:
            return await self._task
        self._refreshing = True
        self._task = loop.create_future()
        try:
            cfg = config.get()
            if not cfg.get("hot_library_enabled", True):
                with self._lock:
                    self._meta["last_error"] = "热门媒体库已关闭"
                    result = self.get()
                self._task.set_result(result)
                return result
            sources = [s for s in config.enabled_sources() if s.get("kind") != "t4"]
            old = self.get()
            data = await asyncio.wait_for(
                collect_hot(
                    sources=sources,
                    limit_per_source=int(cfg.get("hot_per_source", 120)),
                    max_items=int(cfg.get("hot_limit", 300)),
                    is_junk=config.is_junk,
                    pages_per_source=int(cfg.get("hot_pages_per_source", 6)),
                ),
                timeout=REFRESH_TIMEOUT,
            )
            # 所有线路失败，或刷新结果异常为空时，绝不能用空结果覆盖旧缓存。
            if sources and int(data.get("provider_ok", 0)) == 0:
                raise RuntimeError("所有线路刷新失败，保留旧缓存")
            if not data.get("items") and old.get("items"):
                raise RuntimeError("热门刷新结果为空，保留旧缓存")
            if store_obj is not None and data.get("items"):
                # 只保存热门缓存中仍存在的条目，避免热门刷新一次就重写全部标题库。
                store_obj.remember_merged(data["items"])
            with self._lock:
                self._persist(data)
                self._apply(data)
            result = data
        except Exception as exc:
            # 关键容错：任一资源站、整体超时或写盘失败都不清空旧缓存。
            err = f"{type(exc).__name__}: {str(exc)[:120]}"
            with self._lock:
                self._meta["last_error"] = err
                result = self.get()
        finally:
            self._refreshing = False
            if self._task and not self._task.done():
                self._task.set_result(result)
        return result


def config_hot_enabled() -> bool:
    try:
        from config import config
        return bool(config.get().get("hot_library_enabled", True))
    except Exception:
        return True


hot_cache = HotCache()
