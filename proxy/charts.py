#!/usr/bin/env python3
"""真实热门榜单（方案 B）：展示与播放解耦。

思路对齐 LibreTV：
- 展示层的数据来自真实热榜（豆瓣周榜，经 60s API 免 key 转发；辅以豆瓣官方
  tag 推荐接口补足数量/动漫），只提供 片名/海报/评分/年份 等元数据；
- 播放层不在刷新时解析：榜单条目带稳定的 chart guid，用户点进详情/播放时
  才用片名去苹果 CMS 源聚合搜索（复用 online_search 的跨源合并与相关性过滤），
  命中后把可播放 brief 挂到该 chart guid，复用现有整条播放链。

后台定时刷新榜单元数据到 state/charts.json，失败保留旧缓存；榜单只读、秒开。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import hashlib
from pathlib import Path
from typing import Any

import httpx

STATE = Path(__file__).resolve().parent.parent / "state" / "charts.json"
REFRESH_TIMEOUT = 60.0

# 60s API：主用社区实例，官方公共实例兜底（数据中心 IP 常被 Cloudflare 拦）。
SIXTY_BASES = ("https://60s.crystelf.top", "https://60s.viki.moe")
DOUBAN_SUBJECTS = "https://movie.douban.com/j/search_subjects"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# chart key -> (媒体库展示名, fnos MediaDB category)
CHART_CATEGORIES = [
    ("movie", "🎬热门电影", "Movie"),
    ("tv", "📺热门剧集", "TV"),
    ("anime", "🌸动漫新番", "Mix"),
    ("variety", "🎤热门综艺", "Mix"),
]

# 每个库的取数配方：60s 周榜 + 豆瓣 tag 推荐（tag 可翻多页，pages 每标签取页数，
# 每页 20 条）。去重合并：周榜固定排在最前，其余按配方/页序补足。
# kind: weekly=60s 豆瓣周榜；tag=豆瓣官方 search_subjects。
_CHART_RECIPES: dict[str, list[dict]] = {
    "movie": [
        {"kind": "weekly", "path": "movie"},
        {"kind": "tag", "type": "movie", "tag": "热门", "pages": 4},
        {"kind": "tag", "type": "movie", "tag": "最新", "pages": 3},
        {"kind": "tag", "type": "movie", "tag": "豆瓣高分", "pages": 2},
        {"kind": "tag", "type": "movie", "tag": "华语", "pages": 2},
        {"kind": "tag", "type": "movie", "tag": "欧美", "pages": 2},
    ],
    "tv": [
        {"kind": "weekly", "path": "tv_chinese"},
        {"kind": "weekly", "path": "tv_global"},
        {"kind": "tag", "type": "tv", "tag": "热门", "pages": 4},
        {"kind": "tag", "type": "tv", "tag": "美剧", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "国产剧", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "韩剧", "pages": 2},
        {"kind": "tag", "type": "tv", "tag": "日剧", "pages": 2},
    ],
    "anime": [
        {"kind": "tag", "type": "tv", "tag": "日本动画", "pages": 5},
        {"kind": "tag", "type": "tv", "tag": "动画", "pages": 4},
        {"kind": "tag", "type": "movie", "tag": "动画", "pages": 3},
    ],
    "variety": [
        {"kind": "weekly", "path": "show_chinese"},
        {"kind": "weekly", "path": "show_global"},
        {"kind": "tag", "type": "tv", "tag": "综艺", "pages": 6},
        {"kind": "tag", "type": "tv", "tag": "真人秀", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "脱口秀", "pages": 2},
    ],
}

# 每个库最终保留的条数（去重后截断）
CHART_LIMIT = 160
TAG_PAGE_SIZE = 20
# 豆瓣同一 host 的请求并发上限，避免触发限流
_TAG_CONCURRENCY = 4

_IS_TV_LIB = {"tv", "anime", "variety"}


def chart_guid(cat: str, douban_id: str) -> str:
    return hashlib.md5(f"fnmedia-chart:{cat}:{douban_id}".encode()).hexdigest()


def chart_cat_title(cat: str) -> str:
    for c, title, _ in CHART_CATEGORIES:
        if c == cat:
            return title
    return "🌐在线热门"


def chart_cat_mdb(cat: str) -> str:
    for c, _, mdb in CHART_CATEGORIES:
        if c == cat:
            return mdb
    return "Mix"


CHART_GUID_CAT: dict[str, str] = {}  # 运行期由缓存数据反填；也提供静态命名空间判断
_CHART_NS = "fnmedia-chart:"


def is_chart_namespace_guid(guid: str) -> bool:
    """guid 本身是 md5 无法反解，需查缓存索引（见 ChartCache.get_guid）。"""
    return False


def _year_from_subtitle(sub: str) -> str:
    m = re.search(r"(19|20)\d{2}", sub or "")
    return m.group(0) if m else ""


async def _get_json(client: httpx.AsyncClient, url: str, *,
                    referer: str = "", timeout: float = 10.0) -> Any:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    r = await client.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def _fetch_weekly(client: httpx.AsyncClient, path: str) -> list[dict]:
    """60s 豆瓣周榜。依次尝试各实例。"""
    last = None
    for base in SIXTY_BASES:
        try:
            j = await _get_json(client, f"{base}/v2/douban/weekly/{path}")
            data = j.get("data") if isinstance(j, dict) else None
            if isinstance(data, list) and data:
                return data
        except Exception as exc:  # noqa: BLE001
            last = exc
    if last:
        raise last
    return []


async def _fetch_tag(client: httpx.AsyncClient, typ: str, tag: str,
                     pages: int = 1) -> list[dict]:
    """豆瓣官方 tag 推荐，可翻多页（每页 TAG_PAGE_SIZE）。某页失败/为空即停。"""
    out: list[dict] = []
    for pg in range(max(1, int(pages))):
        params = {"type": typ, "tag": tag, "sort": "recommend",
                  "page_limit": TAG_PAGE_SIZE, "page_start": pg * TAG_PAGE_SIZE}
        try:
            r = await client.get(
                DOUBAN_SUBJECTS, params=params,
                headers={"User-Agent": _UA,
                         "Referer": "https://movie.douban.com/",
                         "Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            subjects = (r.json() or {}).get("subjects") or []
        except Exception:
            break
        if not subjects:
            break
        for s in subjects:
            if not s.get("title") or not s.get("cover"):
                continue
            out.append({
                "id": str(s.get("id")),
                "title": s.get("title"),
                "rating": _to_float(s.get("rate")),
                "cover": s.get("cover"),
                "card_subtitle": (s.get("episodes_info") or "").strip(),
                "is_tv": typ == "tv",
                "origin": "douban-tag",
                "tag": tag,
            })
    return out


def _normalize_tag(raw: dict, cat: str) -> dict | None:
    did = str(raw.get("id") or "")
    title = (raw.get("title") or "").strip()
    cover = raw.get("cover") or ""
    if not did or not title or not cover:
        return None
    return {
        "id": did,
        "title": title,
        "rating": _to_float(raw.get("rating")),
        "cover": cover,
        "subtitle": (raw.get("card_subtitle") or "").strip(),
        "year": _year_from_subtitle(raw.get("card_subtitle") or ""),
        "desc": "",
        "tags": [raw["tag"]] if raw.get("tag") else [],
        "url": f"https://movie.douban.com/subject/{did}/",
        "is_tv": cat != "movie",
        "origin": "douban-tag",
    }


def _to_float(v) -> float:
    try:
        f = float(v)
        return f if f > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_weekly(raw: dict, cat: str) -> dict | None:
    did = str(raw.get("id") or "")
    title = (raw.get("title") or "").strip()
    cover = raw.get("cover") or raw.get("cover_proxy") or ""
    if not did or not title or not cover:
        return None
    return {
        "id": did,
        "title": title,
        "rating": _to_float(raw.get("rating")),
        "cover": cover,
        "subtitle": (raw.get("card_subtitle") or "").strip(),
        "year": _year_from_subtitle(raw.get("card_subtitle") or ""),
        "desc": (raw.get("description") or "").strip(),
        "tags": raw.get("tags") or [],
        "url": raw.get("url") or f"https://movie.douban.com/subject/{did}/",
        "is_tv": cat != "movie",
        "origin": "douban-weekly",
    }


async def collect_charts() -> dict[str, Any]:
    """并发拉取所有配方，去重合并为 4 个库。返回可持久化结构。"""
    sem = asyncio.Semaphore(8)
    tag_sem = asyncio.Semaphore(_TAG_CONCURRENCY)  # 豆瓣 search_subjects 限流
    provider_ok = 0
    provider_total = 0

    async def run_weekly(coro):
        async with sem:
            return await coro

    async def run_tag(coro):
        # 周榜走 60s 实例，豆瓣 tag 直连 movie.douban.com，分别限流
        async with tag_sem:
            return await coro

    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        # 收集 cat -> 配方结果
        recipe_jobs: dict[str, list[asyncio.Task]] = {}
        recipe_meta: dict[asyncio.Task, tuple[str, dict]] = {}
        for cat, recipes in _CHART_RECIPES.items():
            for rc in recipes:
                provider_total += 1
                if rc["kind"] == "weekly":
                    t = asyncio.ensure_future(
                        run_weekly(_fetch_weekly(client, rc["path"])))
                else:
                    t = asyncio.ensure_future(
                        run_tag(_fetch_tag(client, rc["type"], rc["tag"],
                                           pages=int(rc.get("pages", 1)))))
                recipe_jobs.setdefault(cat, []).append(t)
                recipe_meta[t] = (cat, rc)

        raw_by_cat: dict[str, list[dict]] = {c: [] for c, _, _ in CHART_CATEGORIES}
        await asyncio.gather(*[t for ts in recipe_jobs.values() for t in ts],
                             return_exceptions=True)
        for t, (cat, rc) in recipe_meta.items():
            if t.cancelled() or isinstance(t.exception(), Exception):
                continue
            provider_ok += 1
            rows = t.result()
            for raw in rows:
                item = (_normalize_weekly(raw, cat) if rc["kind"] == "weekly"
                        else _normalize_tag(raw, cat))
                if item:
                    raw_by_cat[cat].append(item)

    charts: dict[str, list[dict]] = {}
    for cat, _, _ in CHART_CATEGORIES:
        seen: set[str] = set()
        merged: list[dict] = []
        for it in raw_by_cat.get(cat, []):
            did = str(it["id"])
            if did in seen:
                continue
            seen.add(did)
            it = dict(it)
            it["guid"] = chart_guid(cat, did)
            it["cat"] = cat
            merged.append(it)
        # 周榜在前（已按 rank），tag 在后；整体按 (周榜优先, 评分) 稳定排序：
        # 保持首次出现顺序即可（周榜配方排在 tag 前），评分作为同分稳定参考。
        charts[cat] = merged[:CHART_LIMIT]

    return {
        "updated_at": int(time.time()),
        "provider_ok": provider_ok,
        "provider_total": provider_total,
        "charts": charts,
    }


class ChartCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._meta: dict[str, Any] = {"updated_at": 0, "provider_ok": 0,
                                      "provider_total": 0, "last_error": "尚未刷新"}
        self._charts: dict[str, list[dict]] = {}
        self._index: dict[str, dict] = {}
        self._refreshing = False
        self._task: asyncio.Future | None = None
        self.load()

    def load(self):
        with self._lock:
            try:
                j = json.loads(STATE.read_text())
                if isinstance(j, dict) and isinstance(j.get("charts"), dict):
                    self._apply(j)
                    return
            except Exception:
                pass
            self._charts = {c: [] for c, _, _ in CHART_CATEGORIES}
            self._index = {}

    def _apply(self, data: dict[str, Any]):
        self._charts = {c: list((data.get("charts") or {}).get(c) or [])
                        for c, _, _ in CHART_CATEGORIES}
        self._index = {it["guid"]: it for lst in self._charts.values()
                       for it in lst if it.get("guid")}
        self._meta = {
            "updated_at": data.get("updated_at", 0),
            "provider_ok": data.get("provider_ok", 0),
            "provider_total": data.get("provider_total", 0),
            "last_error": data.get("last_error", ""),
        }

    def items(self, cat: str) -> list[dict]:
        with self._lock:
            return list(self._charts.get(cat, ()))

    def reset(self) -> None:
        """清空榜单缓存（管理页一键清缓存）：内存置空并删除落盘文件，
        下次访问由后台任务重新抓取。"""
        with self._lock:
            self._charts = {c: [] for c, _, _ in CHART_CATEGORIES}
            self._index = {}
            self._meta = {"updated_at": 0, "provider_ok": 0,
                          "provider_total": 0, "last_error": "已手动清空"}
            try:
                STATE.unlink(missing_ok=True)
            except Exception:
                pass

    def get_guid(self, guid: str) -> dict | None:
        with self._lock:
            return self._index.get(guid)

    def is_chart_guid(self, guid: str) -> bool:
        with self._lock:
            return guid in self._index

    def cat_of(self, guid: str) -> str | None:
        it = self.get_guid(guid)
        return it.get("cat") if it else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "refreshing": self._refreshing,
                "updated_at": self._meta.get("updated_at", 0),
                "provider_ok": self._meta.get("provider_ok", 0),
                "provider_total": self._meta.get("provider_total", 0),
                "last_error": self._meta.get("last_error", ""),
                "counts": {c: len(self._charts.get(c, []))
                           for c, _, _ in CHART_CATEGORIES},
            }

    def stale(self, interval: float) -> bool:
        with self._lock:
            ts = float(self._meta.get("updated_at") or 0)
        return (not ts) or (time.time() - ts) >= interval

    def _persist(self, data: dict[str, Any]):
        tmp = STATE.with_suffix(".tmp")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(STATE)

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        if self._refreshing and not force and self._task and not self._task.done():
            return await self._task
        loop = asyncio.get_running_loop()
        self._refreshing = True
        self._task = loop.create_future()
        result: dict[str, Any] = {}
        try:
            data = await asyncio.wait_for(collect_charts(), timeout=REFRESH_TIMEOUT)
            charts = data.get("charts") or {}
            total = sum(len(v) for v in charts.values())
            if data.get("provider_ok", 0) == 0 or total == 0:
                raise RuntimeError("榜单全部获取失败/为空，保留旧缓存")
            data["last_error"] = ""
            with self._lock:
                self._persist(data)
                self._apply(data)
            result = data
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:120]}"
            with self._lock:
                self._meta["last_error"] = err
                result = {"charts": self._charts, **{k: self._meta.get(k)
                          for k in ("updated_at", "provider_ok", "provider_total")},
                          "last_error": err}
        finally:
            self._refreshing = False
            if self._task and not self._task.done():
                self._task.set_result(result)
        return result


chart_cache = ChartCache()
