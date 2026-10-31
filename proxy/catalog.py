#!/usr/bin/env python3
"""海量分类片库（方案 C）：直接按资源站苹果 CMS 分类深翻。

与豆瓣“热榜”（charts.py，精选 ~160 部、点击才搜源）不同，这里面向“无限浏览”：
- 启动/定时拉取各资源站的分类表 ac=list，把所有子分类按名称归并为
  movie / tv / anime / variety 四个标准大类；
- 每个大类选一个“规范源”（可深翻、页数多、稳定），浏览时按页直连
  ac=detail&t=<子类id>&pg=N，每页 20 条且自带播放地址，点开即可播，无需再搜索；
- 页级短 TTL 内存缓存 + 单 flight；规范源某页失败自动回退到备选源；
- 条目 guid 绑定 (源, vod_id)，brief 入库后复用既有详情/选集/播放/续播链。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx

import providers
from store import make_guid

PAGE_SIZE = 20
# 每个大类最多可浏览的页数（页数×20=条数）。250 页 ≈ 5000 部。
MAX_PAGES = 250
CLASS_REFRESH = 6 * 3600
PAGE_TTL = 10 * 60
# 空页（多为慢源抖动超时）只短缓存，避免该页被长时间冻结
EMPTY_TTL = 15
HTTP_TIMEOUT = 12.0

# 大类 -> 展示名 / MediaDB category
CATALOG_CATEGORIES = [
    ("movie", "🎞海量电影", "Movie"),
    ("tv", "📺海量剧集", "TV"),
    ("anime", "🌸海量动漫", "Mix"),
    ("variety", "🎤海量综艺", "Mix"),
]

# 跨源聚合的“海量四库”使用的保留作用域 id；单源独立库作用域为其 source id。
AGG_SCOPE = "__agg__"
CATS = [c for c, _, _ in CATALOG_CATEGORIES]

# 名称关键字（匹配 type_name 子串）。顺序：动漫/综艺优先于“电影/剧”判定。
_ANIME_KW = ("动漫", "动画", "番剧", "卡通")
_VARIETY_KW = ("综艺", "真人秀", "脱口秀", "娱乐")
# 电影：电影/影片/各类型“XX片”且非剧非动漫；剧：电视/连续剧/美剧/国产剧…
_MOVIE_KW = ("电影", "影片", "纪录片", "纪录")
_TV_KW = ("电视", "连续剧", "剧集", "美剧", "英剧", "韩剧", "日剧", "泰剧",
          "国产剧", "港台剧", "海外剧", "番剧")
# 明确排除的脏类（伦理/福利/资讯/解说/预告/体育/短剧等）
_EXCLUDE_KW = ("理论", "福利", "写真", "午夜", "解说", "预告",
               "花絮", "资讯", "新闻", "体育", "赛事", "短剧", "漫剧", "AI",
               "演员", "音乐", "MV")

# 硬红线：任何开关都不能放开。涉未成年人性化、非自愿（换脸/迷奸/强奸/偷拍等）。
# 同时作用于“分类名”和条目标题/备注，命中即丢弃，聚合库、独立库、搜索一律不收录。
_REDLINE_KW = (
    # 未成年人
    "萝莉", "童贞",
    # 非自愿 / 性暴力
    "换脸"
)
# 即便开启扩展分类，也仍按“非影视/中性噪音”排除的分类
_NEUTRAL_KW = ("解说", "预告", "花絮", "资讯", "新闻", "体育", "赛事",
               "演员", "音乐", "MV", "直播", "电台")

# 扩展分类使用的内部 cat id（不进入 CATS，故永不进入跨源聚合的四个海量库）
ADULT_CAT = "adult"

try:
    import features
except Exception:  # 兜底：独立运行/剥离异常时按公开（关闭）处理
    features = None


def _adult_on() -> bool:
    return bool(getattr(features, "ADULT", False))


def is_redline(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        for k in _REDLINE_KW:
            if k and k in t:
                return True
    return False


def catalog_title(cat: str) -> str:
    for c, title, _ in CATALOG_CATEGORIES:
        if c == cat:
            return title
    return "🌐海量片库"


def catalog_mdb(cat: str) -> str:
    for c, _, mdb in CATALOG_CATEGORIES:
        if c == cat:
            return mdb
    return "Mix"


def classify(name: str) -> str | None:
    n = (name or "").strip()
    if not n or any(k in n for k in _EXCLUDE_KW):
        return None
    if any(k in n for k in _ANIME_KW):
        return "anime"
    if any(k in n for k in _VARIETY_KW):
        return "variety"
    if any(k in n for k in _TV_KW):
        return "tv"
    if any(k in n for k in _MOVIE_KW):
        return "movie"
    # “动作片/喜剧片/剧情片/科幻片…”这类以“片”结尾的归入电影
    if n.endswith("片") and len(n) <= 4:
        return "movie"
    return None


def classify_source_class(name: str, adult_allowed: bool) -> str | None:
    """按源的扩展授权决定分类归属。

    - 命中硬红线（涉未成年/非自愿）一律返回 None，任何开关都不放行；
    - 常规四类走 classify（其内部脏类/中性噪音仍排除）；
    - 仅当该源显式开启扩展分类(adult_allowed)、且为完整构建时，未归入常规
      四类、又不属于中性噪音/红线的分类，纳入内部扩展分类（只进该源独立库）。
    """
    n = (name or "").strip()
    if not n or is_redline(n):
        return None
    cat = classify(n)
    if cat:
        return cat
    if not adult_allowed or not _adult_on():
        return None
    if any(k in n for k in _NEUTRAL_KW):
        return None
    # 其余扩展分类（公开构建下此分支不可达）
    return ADULT_CAT


async def _fetch_class_list(client: httpx.AsyncClient, source: dict) -> list[dict]:
    base = source["api"].split("/at/json")[0].split("?")[0]
    r = await client.get(base, params={"ac": "list"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    cls = j.get("class") if isinstance(j, dict) else None
    return cls if isinstance(cls, list) else []


async def _fetch_category_page(client: httpx.AsyncClient, source: dict,
                               type_id, page: int,
                               timeout: float = HTTP_TIMEOUT) -> tuple[list, int]:
    """返回 (本页 vod 列表, 该分类总页数)。"""
    base = source["api"].split("/at/json")[0].split("?")[0]
    # 注意：分类深翻不能带 h 参数（部分源加 h 后返回空/pagecount=0）。
    r = await client.get(base, params={"ac": "detail", "t": type_id, "pg": page},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return [], 0
    return (j.get("list") or []), int(j.get("pagecount") or 0)


class Catalog:
    def __init__(self):
        self._lock = threading.RLock()
        # source_id -> [ {type_id, type_name, type_pid, cat} ]
        self._classes: dict[str, list[dict]] = {}
        # "scope:cat" -> [ {source_id, type_id, type_name, pagecount?} ]（首选在最前）
        # scope 为 AGG_SCOPE（跨源聚合）或某个 source_id（单源独立媒体库）。
        self._routes: dict[str, list[dict]] = {}
        self._updated = 0.0
        self._refreshing = False
        # key = (scope, cat, page)
        self._page_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        # "scope:cat" -> 去重缓冲（资源站分页会重复个别 vod）
        self._seen_guids: dict[str, set[str]] = {}
        self._dense: dict[str, list[dict]] = {}
        self._scanned_page: dict[str, int] = {}
        self._dense_count: dict[str, int] = {}
        # (sid, cat) -> [备用叶子 route...]（首选分类探测失败时 rescue 得到）
        self._fallbacks: dict[tuple[str, str], list[dict]] = {}

    @staticmethod
    def _scope_key(scope: str, cat: str) -> str:
        return f"{scope}:{cat}"

    def standalone_scopes(self) -> list[str]:
        """当前需要独立成库的源 id（来源配置中的 standalone 标记）。"""
        try:
            from config import config
            return [s["id"] for s in config.all_sources() if s.get("standalone")]
        except Exception:
            return []

    # -------------------------------------------------- 分类表 / 路由
    async def refresh_routes(self, sources: list[dict]) -> dict[str, Any]:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                await asyncio.gather(
                    *[self._load_source_classes(client, s) for s in sources],
                    return_exceptions=True)

            # 每个源每类先探测一个代表性叶子；首选超时/空时，回退探测该类下
            # 其它叶子，选能连通且页数最多的（部分私有源个别分类响应很慢）。
            # all_by_cat[(sid,cat)] = [cl,...]，首个为首选。
            all_by_cat: dict[tuple[str, str], list[dict]] = {}
            order: list[tuple[str, str]] = []
            for sid, classes in self._classes.items():
                per_cat: dict[str, list[dict]] = {}
                for cl in classes:
                    cat = cl.get("cat")
                    if cat:
                        per_cat.setdefault(cat, []).append(cl)
                for cat, cls in per_cat.items():
                    key = (sid, cat)
                    all_by_cat[key] = cls
                    order.append(key)
            probed: dict[tuple[str, str], dict] = {}

            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                async def probe_one(sid, cat, cl):
                    source = self._source_by_id(sid)
                    if not source:
                        return None
                    try:
                        _lst, pc = await _fetch_category_page(
                            client, source, cl["type_id"], 1)
                    except Exception:
                        return None
                    if not _lst:
                        return None
                    return {"source_id": sid, "type_id": cl["type_id"],
                            "type_name": cl["type_name"],
                            "pagecount": int(pc or 0)}

                # 第一轮：每个 (源,类) 只探首选
                first = await asyncio.gather(
                    *[probe_one(sid, cat, all_by_cat[(sid, cat)][0])
                      for sid, cat in order],
                    return_exceptions=True)
                missing: list[tuple[str, str]] = []
                for (sid, cat), res in zip(order, first):
                    if isinstance(res, dict):
                        probed[(cat, sid)] = res
                    else:
                        missing.append((sid, cat))

                # 第二轮：对首轮失败/空的(源,类)，并发尝试其余叶子，取页数最多者
                async def rescue(sid, cat):
                    cls = all_by_cat[(sid, cat)][1:9]  # 最多再试 8 个叶子
                    if not cls:
                        return
                    res = await asyncio.gather(
                        *[probe_one(sid, cat, cl) for cl in cls],
                        return_exceptions=True)
                    ok = [r for r in res if isinstance(r, dict)]
                    if ok:
                        ok.sort(key=lambda r: -(r.get("pagecount") or 0))
                        probed[(cat, sid)] = ok[0]
                        # 记录备用叶子，供翻页超时/空页时同类并发竞速回退
                        self._fallbacks[(sid, cat)] = ok[1:7]
                await asyncio.gather(*[rescue(s, c) for s, c in missing],
                                     return_exceptions=True)

            def routes_for_scope(scope: str) -> dict[str, list[dict]]:
                out: dict[str, list[dict]] = {}
                # 扩展分类只可能出现在单源作用域（该源显式授权），永不进聚合
                cats = CATS if (scope == AGG_SCOPE or not _adult_on()) else CATS + [ADULT_CAT]
                for cat in cats:
                    if scope == AGG_SCOPE:
                        rs = [probed[(cat, sid)] for (c2, sid) in probed if c2 == cat]
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        # 聚合：首选最深的源，并附带其余可深翻源做回退
                        out[cat] = rs
                    else:
                        # 单源独立库：不依赖建库探测是否成功——直接纳入该类下
                        # 全部叶子（探测到页数的排前面，未探测到的用 MAX_PAGES
                        # 兜底），取页时并发抓取合并、天然容错慢/超时叶子。
                        by_id = {}
                        seed = []
                        hit = probed.get((cat, scope))
                        if hit:
                            seed.append(hit)
                        seed.extend(self._fallbacks.get((scope, cat), []))
                        for r in seed:
                            by_id[r["type_id"]] = r
                        for cl in self._classes.get(scope, []):
                            if cl.get("cat") == cat and cl["type_id"] not in by_id:
                                by_id[cl["type_id"]] = {
                                    "source_id": scope,
                                    "type_id": cl["type_id"],
                                    "type_name": cl["type_name"],
                                    "pagecount": MAX_PAGES}
                        rs = list(by_id.values())
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        out[cat] = rs
                return out

            new_routes: dict[str, list[dict]] = {}
            agg = routes_for_scope(AGG_SCOPE)
            for cat, rs in agg.items():
                new_routes[self._scope_key(AGG_SCOPE, cat)] = rs
            for sid in self.standalone_scopes():
                for cat, rs in routes_for_scope(sid).items():
                    if rs:
                        new_routes[self._scope_key(sid, cat)] = rs
            with self._lock:
                # 清理已不再需要的作用域缓冲
                valid_keys = set(new_routes.keys())
                self._routes = new_routes
                for d in (self._seen_guids, self._dense, self._scanned_page):
                    for k in list(d.keys()):
                        if k not in valid_keys:
                            d.pop(k, None)
                self._updated = time.time()
            warm_scopes = [sid for sid in self.standalone_scopes()
                           if any(self.available(c, sid) for c in
                                  (CATS + ([ADULT_CAT] if _adult_on() else [])))]
            for sid in warm_scopes:
                asyncio.create_task(self.warm_standalone(sid))
        finally:
            self._refreshing = False
        return self.status()

    async def _load_source_classes(self, client: httpx.AsyncClient, source: dict):
        try:
            cls = await _fetch_class_list(client, source)
        except Exception:
            cls = []
        # 只取叶子子类：苹果CMS 的父类（电影片/连续剧…）不聚合子类、只有零星条目，
        # 真正可深翻的是动作片/国产剧这类子分类。
        parent_ids = {str(x.get("type_id")) for x in cls}
        pids_with_child = {str(x.get("type_pid") or "0") for x in cls
                           if str(x.get("type_pid") or "0") not in ("", "0")}
        norm = []
        adult_allowed = bool(source.get("adult"))
        for x in cls:
            try:
                tid = x.get("type_id")
                pid = str(x.get("type_pid") or "0")
                name = str(x.get("type_name") or "")
                cat = classify_source_class(name, adult_allowed)
                if tid is None or cat is None:
                    continue
                is_leaf = pid != "0" or str(tid) not in pids_with_child
                if not is_leaf:
                    continue
                norm.append({"type_id": tid, "type_name": name,
                             "type_pid": pid, "cat": cat})
            except Exception:
                continue
        with self._lock:
            self._classes[source["id"]] = norm

    def _source_by_id(self, sid: str) -> dict | None:
        try:
            from config import config
            return next((s for s in config.all_sources() if s["id"] == sid), None)
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": int(self._updated),
                "categories": {
                    cat: {
                        "routes": len(self._routes.get(
                            self._scope_key(AGG_SCOPE, cat), [])),
                        "source": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("source_id", ""),
                        "type_name": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("type_name", ""),
                        "pagecount": min(int((self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0]
                            .get("pagecount") or 0), MAX_PAGES),
                    } for cat, _, _ in CATALOG_CATEGORIES},
            }

    def available(self, cat: str, scope: str = AGG_SCOPE) -> bool:
        with self._lock:
            return bool(self._routes.get(self._scope_key(scope, cat)))

    def pagecount(self, cat: str, scope: str = AGG_SCOPE) -> int:
        with self._lock:
            routes = self._routes.get(self._scope_key(scope, cat)) or []
            if not routes:
                return 0
            return min(int(routes[0].get("pagecount") or 0), MAX_PAGES)

    def total(self, cat: str, scope: str = AGG_SCOPE) -> int:
        # 用规范源页数估算 total；最后一页条数未知，前端以空页自然停止。
        return self.pagecount(cat, scope) * PAGE_SIZE

    def routes_stale(self) -> bool:
        return (not self._updated) or (time.time() - self._updated) >= CLASS_REFRESH

    # -------------------------------------------------- 页级抓取
    async def _extend_dense(self, scope: str, cat: str, need_count: int) -> int:
        """顺序翻页直到去重后的累计条目数达到 need_count（或翻到该源末页）。"""
        sk = self._scope_key(scope, cat)
        pc = self.pagecount(cat, scope)
        seen = self._seen_guids.setdefault(sk, set())
        dense = self._dense.setdefault(sk, [])
        pg = self._scanned_page.get(sk, 0) + 1
        empty_streak = 0
        while len(dense) < need_count and pg <= pc:
            rows = await self.get_page(cat, pg, scope)
            added = 0
            for b in rows:
                gid = b.get("guid")
                if gid in seen:
                    continue
                seen.add(gid); dense.append(b); added += 1
            if added:
                # 只有真正取到新增才把游标前移，慢源抖动的空页下次可重试
                self._scanned_page[sk] = pg
                pg += 1
                empty_streak = 0
            else:
                # 连续多页空（超时/末页）才停止，容忍慢源的偶发抖动
                empty_streak += 1
                if empty_streak >= 3:
                    self._scanned_page[sk] = pg
                    break
                pg += 1
        self._dense_count[sk] = len(dense)
        return len(dense)

    async def get_window(self, cat: str, offset: int, limit: int,
                         scope: str = AGG_SCOPE) -> list[dict]:
        """按“去重后的全局偏移”取一窗 brief，自动顺序跨 CMS 页，密集无重复。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        sk = self._scope_key(scope, cat)
        dense = self._dense.setdefault(sk, [])
        if len(dense) < offset + limit:
            await self._extend_dense(scope, cat, offset + limit)
        return dense[offset:offset + limit]

    async def get_page(self, cat: str, page: int, scope: str = AGG_SCOPE) -> list[dict]:
        """取某作用域某大类第 page 页（1 起）的可播放 brief 列表（已入库）。"""
        page = max(1, int(page))
        if page > MAX_PAGES:
            return []
        key = (scope, cat, page)
        with self._lock:
            hit = self._page_cache.get(key)
            if hit:
                ts, data, ttl = hit
                if time.time() - ts < ttl:
                    return data
                self._page_cache.pop(key, None)
            fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            with self._lock:
                self._inflight[key] = fut

            async def work():
                try:
                    briefs = await self._load_page(scope, cat, page)
                    with self._lock:
                        # 空结果只短缓存，避免慢源一次抖动把该页冻结 10 分钟
                        ttl = EMPTY_TTL if not briefs else PAGE_TTL
                        self._page_cache[key] = (time.time(), briefs, ttl)
                    if not fut.done():
                        fut.set_result(briefs)
                except Exception:
                    if not fut.done():
                        fut.set_result([])
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)

            asyncio.create_task(work())
        return await fut

    def cached_page_posters(self, cat: str, n: int = 8,
                            scope: str = AGG_SCOPE) -> list[str]:
        """纯本地、零网络：从已缓存的首页/密集列表取海报，供媒体库卡片秒回。"""
        out: list[str] = []
        with self._lock:
            # 优先已翻好的去重密集列表（最新），其次首页缓存
            dense = self._dense.get(self._scope_key(scope, cat)) or []
            pool = list(dense)
            hit = self._page_cache.get((scope, cat, 1))
            if hit and time.time() - hit[0] < hit[2]:
                for b in hit[1]:
                    if b not in pool:
                        pool.append(b)
            seen = set()
            for b in pool:
                p = b.get("poster") or ""
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
                    if len(out) >= n:
                        break
        return out

    def _briefs_from_vods(self, vods: list, source: dict, route: dict,
                          scope: str, cat: str) -> list[dict]:
        from config import config as _cfg
        briefs = []
        for v in vods:
            try:
                t = providers._map_vod(v, source)
            except Exception:
                continue
            if not t or not t.title:
                continue
            playable = [e for line in t.sources.values() for e in line
                        if e.direct or e.resolve]
            if not playable:
                continue
            # 硬红线（任何模式都不放行）：涉未成年/非自愿，标题或备注命中即丢。
            if is_redline(t.title, t.remarks):
                continue
            # 私有源放宽：单源作用域且该源开启 loose_filter 时，
            # 跳过标题/备注噪音过滤；硬红线与常规分类红线仍在。
            loose = (scope != AGG_SCOPE and bool(source.get("loose_filter")))
            if not loose and _cfg.is_junk(t.title, t.remarks):
                continue
            briefs.append({
                "guid": make_guid(t),
                "online_id": t.online_id, "title": t.title,
                "type_name": t.type_name, "year": t.year,
                "poster": t.poster, "overview": t.overview,
                "remarks": t.remarks, "score": t.score,
                "actor": t.actor, "director": t.director, "area": t.area,
                "lines": {name: [e.__dict__ for e in eps]
                          for name, eps in t.sources.items()},
                "catalog_cat": cat, "catalog_scope": scope,
                "catalog_source": route["source_id"],
            })
        return briefs

    async def _load_page(self, scope: str, cat: str, page: int) -> list[dict]:
        from store import store
        with self._lock:
            routes = list(self._routes.get(self._scope_key(scope, cat)) or [])
        if scope == AGG_SCOPE:
            routes = routes[:4]
        if not routes:
            return []

        async def fetch_one(client, route, timeout):
            source = self._source_by_id(route["source_id"])
            if not source:
                return None
            try:
                vods, pc = await _fetch_category_page(
                    client, source, route["type_id"], page, timeout=timeout)
            except Exception:
                return None
            if not vods:
                return None
            return source, route, vods

        # 单源独立库：把该类下全部叶子的同一页并发抓取后“合并去重”，
        # 而不是竞速只取一个——慢/不稳的私有源单页常超时，合并所有叶子
        # 才能保证条目完整。首轮给较长超时，全空再重试两轮。
        if scope != AGG_SCOPE and len(routes) > 1:
            sem = asyncio.Semaphore(10)

            async def once(timeout):
                async with httpx.AsyncClient(follow_redirects=True,
                                             trust_env=False,
                                             verify=False) as client:
                    async def bound(r):
                        async with sem:
                            return await fetch_one(client, r, timeout)
                    results = await asyncio.gather(
                        *[bound(r) for r in routes],
                        return_exceptions=True)
                merged: list[dict] = []
                seen_g: set[str] = set()
                for res in results:
                    if not isinstance(res, tuple):
                        continue
                    source, route, vods = res
                    for b in self._briefs_from_vods(vods, source, route, scope, cat):
                        g = b.get("guid")
                        if g and g not in seen_g:
                            seen_g.add(g)
                            merged.append(b)
                return merged

            for attempt, tmo in enumerate((16.0, 10.0, 8.0)):
                briefs = await once(tmo)
                if briefs:
                    return store.remember_merged(briefs)
            return []

        # 跨源聚合：串行回退（不同源结果应一致，并发会无谓翻倍请求）
        async with httpx.AsyncClient(follow_redirects=True, trust_env=False,
                                     verify=False) as client:
            for route in routes:
                got = await fetch_one(client, route, HTTP_TIMEOUT)
                if not got:
                    continue
                source, _route, vods = got
                briefs = self._briefs_from_vods(vods, source, route, scope, cat)
                if briefs:
                    return store.remember_merged(briefs)
        return []

    async def warm_standalone(self, scope: str):
        """建库后后台预热独立库各分类首页，缓解慢源首屏空白。"""
        try:
            src = self._source_by_id(scope)
            pool = CATS + [ADULT_CAT] if (_adult_on() and src and src.get("adult")) else CATS
            await asyncio.gather(
                *[self.get_page(c, 1, scope) for c in pool
                  if self.available(c, scope)],
                return_exceptions=True)
        except Exception:
            pass

    async def _source_cat_window(self, scope: str, cat: str,
                                 offset: int, limit: int) -> list[dict]:
        """单源某类窗口：每页 = 该类下全部叶子同页合并去重（首页约数百条）。

        顺序取合并大页直到攒够 offset+limit；容忍慢源偶发的整页空（重试到
        连续 3 个空页才认为到底），避免一次抖动就把分页永久截断。
        """
        out: list[dict] = []
        seen: set[str] = set()
        pg = 1
        empty_streak = 0
        while len(out) < offset + limit and pg <= MAX_PAGES and empty_streak < 3:
            rows = await self.get_page(cat, pg, scope)
            added = 0
            for b in rows:
                g = b.get("guid")
                if g and g not in seen:
                    seen.add(g); out.append(b); added += 1
            if added:
                empty_streak = 0
            else:
                empty_streak += 1
            pg += 1
        return out[offset:offset + limit]

    # --------------------------------------- 单源独立库：跨四类交错窗口
    async def standalone_window(self, scope: str, offset: int,
                                limit: int) -> tuple[list[dict], int]:
        """单源独立库：把该源的电影/剧/动漫/综艺按轮次交错成一条“最近更新”流。

        返回 (brief 列表, 估算 total)。四类各取 offset..offset+limit 的窗，
        再按排名轮次合并去重，最后按全局 offset 切片。
        """
        src = self._source_by_id(scope)
        pool = CATS + [ADULT_CAT] if (_adult_on() and src and src.get("adult")) else CATS
        cats = [c for c in pool if self.available(c, scope)]
        if not cats:
            return [], 0
        end = offset + limit
        results = await asyncio.gather(
            *[self._source_cat_window(scope, c, 0, end) for c in cats],
            return_exceptions=True)
        per: list[list[dict]] = []
        total = 0
        for c, r in zip(cats, results):
            per.append(r if isinstance(r, list) else [])
            if isinstance(r, list):
                total += self.total(c, scope)
        merged: list[dict] = []
        seen: set[str] = set()
        maxlen = max((len(x) for x in per), default=0)
        # 轮次交错：第 i 轮依次取每类第 i 条，近似源站“最近更新”混合排序
        for i in range(maxlen):
            for arr in per:
                if i < len(arr):
                    b = arr[i]
                    g = b.get("guid")
                    if g and g not in seen:
                        seen.add(g)
                        merged.append(b)
        return merged[offset:offset + limit], total

    def standalone_available(self, scope: str) -> bool:
        src = self._source_by_id(scope)
        pool = CATS + [ADULT_CAT] if (_adult_on() and src and src.get("adult")) else CATS
        return any(self.available(c, scope) for c in pool)


catalog = Catalog()
