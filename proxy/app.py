#!/usr/bin/env python3
"""fnmedia-ext 主代理（FastAPI）。

链路：nginx(/v) -> 本代理(UDS) -> trim-media 官方后端(upstream UDS)
策略：
  - 搜索 GET /v/api/v1/search/list   透传本地结果 + 追加在线资源
  - 详情 GET /v/api/v1/item/<guid>   在线 guid 返回伪造详情
  - 播放 POST /play/info、GET /stream/list、POST /stream、POST /play/play
                                     在线 guid/media_guid 返回伪造链路
  - 流   GET /v/media/online/<mg>/*  HLS 代理（重写 m3u8 分片/密钥）
  - 海报 GET /v/online/poster        外站海报同源代理
  - 其余全部原样透传官方后端
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import zlib
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse, FileResponse)

import features
import netutil
import providers
import sourcehealth
import linepredict
import t4
from t4_service import T4Service
from t4_media import T4Media
import t4_metadata
from t4_browser import T4Browser, LIB_GUID as T4_LIB_GUID, LIB_TITLE as T4_LIB_TITLE
import client as clientmod
import live as livemod
import panqr
import inject as inj
import config as cfgmod
from config import config
import vlibs
import fnusers
import hot as hotmod
from hot import hot_cache
import charts as chartmod
from charts import chart_cache
import chartbind
import catalog as catalogmod
from catalog import catalog
import history as histmod
from history import history
import marks as marksmod
from marks import marks
import updater
from store import (store, make_guid, media_guid, video_guid, is_online_guid,
                   season_guid, episode_guid, ep_media_guid, ep_video_guid)

# T4 运行时【懒加载】：导入期保持 None，绝不构造重型对象（T4Browser.__init__ 会读
# 节点状态文件并创建 asyncio.Semaphore，导入期构造既做磁盘 I/O 又有事件循环风险）。
# 仅当确实存在已启用的 T4 源、相关路由被调用时才按需创建，未启用 T4 时零开销，也保证
# CMS 共存契约：普通 CMS 请求路径永远不会实例化 T4 运行时。
t4_service = None
t4_media = None
t4_browser = None


def _get_t4_service():
    global t4_service
    if t4_service is None:
        t4_service = T4Service(store, config)
    return t4_service


def _get_t4_media():
    global t4_media
    if t4_media is None:
        t4_media = T4Media()
    return t4_media


def _get_t4_browser():
    global t4_browser
    if t4_browser is None:
        t4_browser = T4Browser(store, config)
    return t4_browser


def _t4_sources():
    if not config.get().get("t4_library_enabled", True):
        return []
    return [s for s in config.enabled_sources() if s.get("kind") == "t4"]


def _t4_node(guid):
    if not guid:
        return None
    if t4_browser is not None:
        return t4_browser.node(guid)
    # 尚未构造浏览器：仅当确实启用了 T4 源时才创建，避免普通 CMS 路径误实例化
    if not any(s.get("kind") == "t4" for s in config.enabled_sources()):
        return None
    return _get_t4_browser().node(guid)


UPSTREAM_SOCK = os.environ.get("FNMEDIA_UPSTREAM_SOCK",
                               "/var/run/trim_media_upstream.sock")
# 本服务回环基址：网盘原画/转码播放地址是根相对路径，HLS 探测/改写需经本机
# HTTP 自调用（nginx 5666 → 本应用），不能直接喂给外站 client。
SELF_BASE = os.environ.get("FNM_SELF_BASE", "http://127.0.0.1:5666")
ONLINE_PREFIX = "/v/media/online"


def _self_url(url: str) -> str:
    """把本服务根相对地址绝对化为回环地址；外站绝对地址原样返回。"""
    if url.startswith("/"):
        return SELF_BASE.rstrip("/") + url
    return url
_search_cache: dict[str, tuple[float, list]] = {}
_search_inflight: dict[str, asyncio.Future] = {}
CACHE_TTL = 120
# 绑定聚合缓存池：同一(片名·是否快源·是否含T4)在 TTL 内复用聚合后的 briefs，
# 榜单绑定 / 播放回源 / 搜索等多次调用同一片名时不重复扇出所有源。
# 注意：缓存的是 remember_merged 之后的 brief（含线路），命中即等价一次完整聚合。
_AGG_CACHE: dict[str, tuple[float, list]] = {}
_AGG_INFLIGHT: dict[str, asyncio.Future] = {}
AGG_CACHE_TTL = 180
_AGG_CACHE_MAX = 800
T4_STREAM_READY_BUDGET = 8.0
# 在线热门按内容拆成 4 个虚拟媒体库。guid 由固定串 md5 派生，保持稳定。
HOT_CATEGORIES = [
    ("movie",   "🌐在线电影", "Movie"),
    ("tv",      "🌐在线剧集", "TV"),
    ("anime",   "🌐在线动漫", "Mix"),
    ("variety", "🌐在线综艺", "Mix"),
]
HOT_LIB_GUIDS: dict[str, str] = {
    "movie":   "f4f27c311388d90f658670995a7fddb8",
    "tv":      "2a6937a71b652ea061a68456a72e69a8",
    "anime":   "5036a366ecfc047b4732b6bc2a39c3d8",
    "variety": "61d5d026ae7390a9f333b5d3a43ef5ff",
}
HOT_GUID_CAT = {g: c for c, g in HOT_LIB_GUIDS.items()}
# 兼容历史单库 guid：旧链接/旧缓存仍可打开，映射到剧集库。
HOT_LEGACY_GUID = "a110c1a0b1a04000900010000000f001"
HOT_DEFAULT_TITLE = "🌐在线热门"

# 真实榜单虚拟媒体库（方案 B：豆瓣周榜/推荐做展示，点击时按需搜源解析播放）。
CHART_LIB_GUIDS: dict[str, str] = {
    "movie":   "0b10d767319eb8cab9d99373ede48d0a",
    "tv":      "9e75c8f2616bd01851911eea649db367",
    "anime":   "18372cada44b72c305343c2bfcc026f0",
    "variety": "cb606d5f699560c630c97f7acc84f75b",
}
CHART_GUID_CAT = {g: c for c, g in CHART_LIB_GUIDS.items()}
# chart guid -> 已解析（挂载到 store）的可播放 brief
_chart_resolved: dict[str, dict] = {}
_chart_resolving: dict[str, asyncio.Future] = {}
# 秒进播放页时，榜单片可能尚未绑定；暂记 media_guid -> chart guid，
# 让播放页内的 stream/play/HLS 请求能找到同一个后台绑定任务，而不是漏给官方后端。
_PENDING_MEDIA_CHART: dict[str, str] = {}
# 已确认暂无可播源的榜单片：短时间内不要反复全网搜索（TTL 10 分钟）。
_CHART_FAILED_TS: dict[str, float] = {}
_CHART_FAILED_TTL = 10 * 60
# 快源未命中、慢源兜底仍在进行时的短冷却（秒）：避免反复压源站，但不把播放页锁死。
_CHART_FAST_MISS_TTL = 45.0
# 榜单绑定的聚合候选窗口：比全局搜索的 12 宽，避免兜底热榜长剧挤掉同名真片。
_CHART_BIND_MAXN = 60
# 失败等级：""=快源未命中(短冷却,慢源兜底中) / "dead"=快源+全源均确认无源(长冷却)
_CHART_FAIL_LEVEL: dict[str, str] = {}
# 正在跑“慢源兜底”的榜单 guid，避免对同一卡片重复起全源搜索
_CHART_SLOW_RUNNING: set[str] = set()
# 浏览热门库时的后台预解析并发上限（仅限流预取，播放触发的解析不限流）
_CHART_PREFETCH_SEM: "asyncio.Semaphore | None" = None


def _chart_cooldown_left(guid: str) -> float:
    """剩余冷却秒数：dead（快源+全源均无）按长 TTL，快源未命中按短 TTL。<=0 可重试。"""
    ts = _CHART_FAILED_TS.get(guid, 0)
    if not ts:
        return 0.0
    ttl = _CHART_FAILED_TTL if _CHART_FAIL_LEVEL.get(guid) == "dead" \
        else _CHART_FAST_MISS_TTL
    return ttl - (time.time() - ts)


def _chart_prefetch_sem() -> asyncio.Semaphore:
    global _CHART_PREFETCH_SEM
    if _CHART_PREFETCH_SEM is None:
        _CHART_PREFETCH_SEM = asyncio.Semaphore(6)
    return _CHART_PREFETCH_SEM

# 海量分类片库（方案 C：按资源站分类深翻，每类数千部，无限滚动）。
CATALOG_LIB_GUIDS: dict[str, str] = {
    "movie":   "e91dd7347a49dbd874fb8a0e78ad7ad7",
    "tv":      "196381f891d66a4de32f9032e8f244f3",
    "anime":   "97fc49015edcb20cc423658401711b6c",
    "variety": "cfff30730cffca2d1357e0c2b52fc2f5",
}
CATALOG_GUID_CAT = {g: c for c, g in CATALOG_LIB_GUIDS.items()}

# —— 单源独立媒体库：每个启用 standalone 的源生成一张库卡片 ——
_STANDALONE_PREFIX = "fnmedia-standalone:"


def standalone_lib_guid(source_id: str) -> str:
    return hashlib.md5((_STANDALONE_PREFIX + source_id).encode()).hexdigest()


def standalone_source_of(lib_guid: str) -> str | None:
    """库 guid 反查 source_id；非独立库返回 None。"""
    if not lib_guid:
        return None
    for s in config.all_sources():
        if standalone_lib_guid(s["id"]) == lib_guid:
            return s["id"]
    return None


def standalone_sources_active() -> list[dict]:
    return [s for s in config.enabled_sources() if s.get("standalone")]


def _vkey_for_guid(guid: str) -> str | None:
    """虚拟库 guid → 稳定库键（含动态库现场反查）；非虚拟库返回 None。"""
    if not guid:
        return None
    if _t4_node(guid):
        return vlibs.K_T4
    k = vlibs.key_of_guid(guid)
    if k:
        return k
    sid = standalone_source_of(guid)
    if sid:
        return vlibs.make_key(vlibs.K_STANDALONE, sid)
    return None


def _viewer_can_open_lib(request: Request, guid: str) -> bool:
    """当前账号是否允许打开该虚拟库（隐藏/无权限返回 False）。非虚拟库恒 True。"""
    key = _vkey_for_guid(guid)
    if not key:
        return True
    return vlibs.account_can_see(fnusers.resolve(request), key)



def standalone_lib_name(src: dict) -> str:
    base = "📡" + (src.get("name") or src.get("id") or "")
    return base






def charts_enabled() -> bool:
    return bool(config.get().get("hot_library_enabled", True))


def catalog_enabled() -> bool:
    """海量四库独立开关（与热门榜单互不影响）。"""
    return bool(config.get().get("catalog_library_enabled", True))


def any_virtual_lib_enabled() -> bool:
    if charts_enabled() or catalog_enabled() or _t4_sources():
        return True
    return False


def _tv_compat_date(v) -> str:
    """TV/盒子端用严格日期解析：'2026' 年份会抛异常导致整页空白，补成完整日期。"""
    s = str(v or "").strip()
    if len(s) == 4 and s.isdigit():
        return f"{s}-01-01"
    return s


def _client_of(request: Request) -> clientmod.Client:
    """从请求 UA 解析客户端画像（TV/iOS/Android/Web）。"""
    return clientmod.parse_user_agent(request.headers.get("user-agent", ""))


def _tv_compat_card(row: dict) -> dict:
    """已废弃的单点规整，保留为兼容包装；新代码请用 client.normalize_card。"""
    return clientmod.normalize_card(
        row, clientmod.Client(kind=clientmod.TV, is_tv=True, strict_card=True))


def chart_card(item: dict) -> dict:
    """榜单条目 → 媒体库卡片（不解析播放；点击详情时才搜源）。"""
    def _chart_poster():
        # 已搜源绑定：海报烧网盘名/本片预测速度角标；未绑定用纯海报，避免列表期探测
        try:
            bound = store.get_title_by_guid(item["guid"])
            if bound:
                return _brief_poster_badged(bound)
        except Exception:
            pass
        return inj._poster(item.get("cover"))
    cat = item["cat"]
    typ = "Movie" if cat == "movie" else "TV"
    year = item.get("year") or ""
    rating = item.get("rating") or 0
    tags = item.get("tags") or []
    overview = item.get("desc") or ("、".join(tags) if tags else "")
    title = item["title"]
    guid = item["guid"]
    row = {
        "guid": guid,
        # TV/盒子端(Flutter)列表模型要求 video_guid/path/runtime，缺失会整库“加载失败”；
        # 手机端较宽松。video_guid 与播放/详情接口用的 video_guid(guid) 保持一致。
        "video_guid": video_guid(guid),
        "path": "",
        "runtime": 0,
        "lan": "zh-CN",
        "imdb_id": "",
        "trim_id": "",
        "tv_title": "",
        "parent_guid": "",
        "parent_title": "",
        "title": title,
        "type": typ,
        "genres": tags[:3],
        "poster": _chart_poster(),
        "poster_width": 0,
        "poster_height": 0,
        "is_favorite": 0,
        "watched": 0,
        "watched_ts": 0,
        "vote_average": str(rating) if rating else "0",
        "media_stream": {"resolutions": None, "audio_type": None, "color_range_type": None},
        "release_date": year,
        "season_number": 0,
        "episode_number": 0,
        "air_date": year,
        "number_of_seasons": 1 if typ == "TV" else 0,
        "number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "local_number_of_episodes": 0,
        # 未解析前乐观标记可播，保证卡片有播放入口；解析失败时详情页会提示暂无片源
        "can_play": 1,
        "status": "豆瓣热榜",
        "overview": overview[:500],
        "ancestor_guid": CHART_LIB_GUIDS[cat],
        "ancestor_name": chartmod.chart_cat_title(cat),
        "ancestor_category": chartmod.chart_cat_mdb(cat),
        "ts": 0,
        "duration": 0,
        "single_child_guid": "",
        "file_name": "",
    }
    return _vod_directory_card(row)


def prewarm_persist_mode() -> bool:
    """预热方式开关：
    True  = 方案B 持久化（绑定结果落盘，重启直接恢复，少重新搜源）；
    False = 方案A 实时（重启后启动任务现场 resolve，默认）。"""
    return str(config.get().get("prewarm_mode", "realtime")) == "persist"


def _persist_chart_binding(guid: str, bound: dict) -> None:
    """方案B：把榜单绑定结果落盘（仅持久化模式）。失败静默，不影响播放。"""
    try:
        if prewarm_persist_mode() and guid and bound:
            chartbind.bind_store.remember(guid, bound)
    except Exception:
        pass


def _restore_chart_bindings() -> int:
    """方案B：启动时从磁盘恢复榜单绑定到 store（返回恢复条数）。

    过期绑定也恢复进内存（避免冷点），但播放链若发现死链仍会重新 resolve；
    同时后台预热会对失效片刷新。"""
    try:
        if not prewarm_persist_mode():
            return 0
        restored = 0
        for cat, _t, _g in chartmod.CHART_CATEGORIES:
            for it in chart_cache.items(cat):
                guid = it.get("guid")
                if not guid or store.get_title_by_guid(guid):
                    continue
                brief = chartbind.bind_store.get(guid)
                if not brief:
                    continue
                if not _chart_binding_matches(guid, brief):
                    chartbind.bind_store.forget(guid)
                    continue
                b = dict(brief)
                b["guid"] = guid  # 确保挂到榜单卡片 guid
                try:
                    store.remember_merged([b])
                    restored += 1
                except Exception:
                    pass
        return restored
    except Exception:
        return 0


_CHART_VARIANT_SUFFIX_RE = re.compile(
    r"^(?:(?:普通话|国语|粤语|英语|日语|韩语|中字|中英双字|中文字幕|"
    r"hd|高清|超清|蓝光|完整版|未删减|正片)+)$", re.I)
# 资源站常见的“片名+年份/语言版本”后缀，如《美人余2026》《璀璨之城粤语》。
# 仅允许短的年份或语言/清晰度词，避免《突如其来》误匹配《突如其来的假期》。
_FOLDER_VARIANT_SUFFIX_RE = re.compile(
    r"^(?:(?:19|20)\d{2}|普通话|国语|粤语|英语|日语|韩语|中字|中英双字|"
    r"中文字幕|hd|高清|超清|蓝光|完整版|未删减|正片|版)+$", re.I)


def _folder_title_rank(title: str, want_year: str, brief: dict) -> int:
    """二级目录绑定的片名相关度：完全同名优先，其次语言/年份后缀，拒绝包含误绑。"""
    want = _norm_title(title)
    name = _norm_title(brief.get("title", ""))
    if not want or not name:
        return 99
    brief_year = str(brief.get("year") or "")
    if want_year and brief_year and want_year != brief_year:
        return 99
    if name == want:
        return 0
    if name.startswith(want):
        suffix = name[len(want):]
        if suffix and _FOLDER_VARIANT_SUFFIX_RE.fullmatch(suffix):
            return 1
    return 99


def _pick_folder_brief(title: str, want_year: str, briefs: list[dict]) -> dict | None:
    ranked = sorted(((_folder_title_rank(title, want_year, b), i, b)
                     for i, b in enumerate(briefs)), key=lambda x: (x[0], x[1]))
    return ranked[0][2] if ranked and ranked[0][0] < 99 else None


def _chart_title_rank(item: dict, brief: dict) -> int:
    """榜单→资源站片名相关度：完整同名最优先，其次仅允许语言/清晰度后缀。
    不能沿用 242 的任意包含匹配，否则《突如其来》会错绑《突如其来的假期》。"""
    want = _norm_title(item.get("title", ""))
    name = _norm_title(brief.get("title", ""))
    if not want or not name:
        return 99
    want_year = str(item.get("year") or "")
    brief_year = str(brief.get("year") or "")
    if want_year and brief_year and want_year != brief_year:
        return 99
    if name == want:
        return 0
    if name.startswith(want):
        suffix = name[len(want):]
        if suffix and _CHART_VARIANT_SUFFIX_RE.fullmatch(suffix):
            return 1
    tokens = [_norm_title(t) for t in re.split(r"[\s]+", item.get("title", ""))
              if _norm_title(t)]
    if len(tokens) > 1 and all(tok in name for tok in tokens):
        return 2
    return 99


def _chart_binding_matches(guid: str, brief: dict | None) -> bool:
    item = chart_cache.get_guid(guid)
    return bool(item and brief and _chart_title_rank(item, brief) < 99)


def _drop_chart_binding(guid: str) -> None:
    """废弃已落盘的错误榜单绑定，并同步清理内存播放索引。"""
    try:
        with store._lock:
            store._titles.pop(guid, None)
            store._orig.pop(guid, None)
            store._dirty = True
            store._rebuild_index()
        chartbind.bind_store.forget(guid)
    except Exception:
        pass


def _best_chart_brief(item: dict, briefs: list[dict]) -> dict | None:
    ranked = sorted(((_chart_title_rank(item, b), i, b)
                     for i, b in enumerate(briefs)), key=lambda x: (x[0], x[1]))
    return ranked[0][2] if ranked and ranked[0][0] < 99 else None


# 快速模式未命中后、后台跑全源兜底的 chart guid（去重，避免重复全源搜索压源站）
_CHART_FULL_FALLBACK: set[str] = set()


def _schedule_full_fallback(guid: str) -> None:
    """快速绑定（快源）未命中时，后台静默跑一次完整绑定（含全源兜底）。

    不阻塞当前请求；命中后写入 store，用户刷新/重进二级目录即可看到线路。"""
    if guid in _CHART_FULL_FALLBACK or store.get_title_by_guid(guid):
        return
    _CHART_FULL_FALLBACK.add(guid)

    async def _job():
        try:
            # 常规参数：6s 快源软超时 + 25s 全源兜底；force 绕过短冷却
            await resolve_chart(guid, force=True)
        except Exception:
            pass
        finally:
            _CHART_FULL_FALLBACK.discard(guid)

    asyncio.create_task(_job())


def start_resolve_chart(guid: str, *, prefetch: bool = False,
                        force: bool = False,
                        fast_soft_timeout: float = 6.0,
                        fast_skip_fallback: bool = False) -> asyncio.Future | None:
    """启动榜单 guid 的后台解析（单飞），立即返回在途 Future；不阻塞当前请求。

    prefetch=True 表示“浏览热门库时提前解析”，受并发信号量限流（避免一次翻页
    对十几个片名并发聚合搜索压垮源站）；播放链路上的即时解析(prefetch=False)
    不限流，保证优先级。force=True 忽略近期失败冷却（播放页 phwait 重试时用，
    避免一次瞬时搜不到就被锁 10 分钟、热门片一直占位不恢复）。
    fast_soft_timeout：快源搜索软超时；二级目录等交互入口可传小值（~2s）让快速
    CMS 源尽快命中，避免被个别 7s+ 慢源拖到 App 网关超时。
    fast_skip_fallback：快速模式下快源未中即结束、不做 25s 全源兜底（浏览预取/
    二级目录用），完整兜底留给播放链路的常规解析，避免单飞任务被长兜底占住。"""
    existing = store.get_title_by_guid(guid)
    if existing and not _chart_binding_matches(guid, existing):
        _drop_chart_binding(guid)
        existing = None
    if existing:
        return None
    if not force and _chart_cooldown_left(guid) > 0:
        return None
    fut = _chart_resolving.get(guid)
    if fut is not None:
        return fut
    fut = asyncio.get_event_loop().create_future()
    _chart_resolving[guid] = fut

    async def work():
        _rel = None
        if prefetch:
            try:
                await asyncio.wait_for(_chart_prefetch_sem().acquire(), timeout=30)
                _rel = True
            except Exception:
                _rel = False
            if not _rel:
                # 限流排队超时：放弃这次预取（不置失败，播放时仍会正常解析）
                _chart_resolving.pop(guid, None)
                if not fut.done():
                    fut.set_result(None)
                return
        try:
            item = chart_cache.get_guid(guid)
            if not item:
                fut.set_result(None)
                return
            # 快源优先绑定，避免全源慢搜索超过首次播放等待窗口；只有无可靠匹配时
            # 才全源兜底。两阶段共用同名/变体/年份校验，绝不放宽错片绑定。
            briefs = await asyncio.wait_for(
                _aggregate_briefs(item["title"], _CHART_BIND_MAXN,
                                  fast_only=True, soft_timeout=fast_soft_timeout,
                                  include_t4=False),
                timeout=max(8.0, fast_soft_timeout + 2.0))
            best = _best_chart_brief(item, briefs)
            if best is None and not fast_skip_fallback:
                briefs = await asyncio.wait_for(
                    _aggregate_briefs(item["title"], _CHART_BIND_MAXN), timeout=25)
                best = _best_chart_brief(item, briefs)
            if best is not None:
                # 关键：把可播放 brief 的身份换成 chart guid，使播放链挂到榜单卡片
                bound = dict(best)
                bound["guid"] = guid
                bound["chart"] = {"title": item["title"], "cat": item["cat"],
                                  "cover": item.get("cover", ""),
                                  "rating": item.get("rating", 0)}
                _chart_resolved[guid] = bound
                store.remember_merged([bound])
                _persist_chart_binding(guid, bound)
                schedule_prewarm_title(guid, 0)
                _PENDING_MEDIA_CHART.pop(media_guid(guid), None)
                _CHART_FAILED_TS.pop(guid, None)
                _CHART_FAIL_LEVEL.pop(guid, None)
            else:
                # 快源未命中（慢源兜底后台进行中）：短冷却 + 非 dead 等级，播放页约
                # 45s 后可再触发；慢源兜底确认全无源才升级为 dead 长冷却。
                _CHART_FAIL_LEVEL[guid] = ""
                _CHART_FAILED_TS[guid] = time.time()
                # 快速模式（浏览预取/二级目录）跳过了同步全源兜底：这里另起后台
                # 完整绑定，不阻塞当前请求；用户稍后重进/刷新即可命中全源结果。
                if fast_skip_fallback:
                    _schedule_full_fallback(guid)
            if not fut.done():
                fut.set_result(best)
        except Exception:
            if not fut.done():
                fut.set_result(None)
        finally:
            _chart_resolving.pop(guid, None)
            if _rel:
                _chart_prefetch_sem().release()

    asyncio.create_task(work())
    return fut


async def resolve_chart(guid: str, *, force: bool = False) -> dict | None:
    """用榜单片名去 CMS 聚合搜索，把最匹配的可播放 brief 挂到该 chart guid。

    单 flight；命中后在内存和 store 都登记，之后整条 season/episode/play 链复用。
    """
    existing = store.get_title_by_guid(guid)
    if existing and not _chart_binding_matches(guid, existing):
        _drop_chart_binding(guid)
        existing = None
    if existing:
        return existing
    fut = start_resolve_chart(guid, force=force)
    if fut is None:
        return store.get_title_by_guid(guid)
    return await fut


# 二级目录按需搜索绑定的在途去重（guid -> Future），避免同片并发重复全网搜索
_FOLDER_BIND_FUT: dict[str, asyncio.Future] = {}


def _folder_title_hint(guid: str) -> str:
    """零网络获取某 guid 的片名：chart 缓存 → catalog 内存反查 → store。"""
    item = chart_cache.get_guid(guid)
    if item and item.get("title"):
        return item["title"]
    cb = catalog.find_brief_by_guid(guid)
    if cb and cb.get("title"):
        return cb["title"]
    b = store.get_title_by_guid(guid)
    return (b or {}).get("title", "")


# 二级目录快源未命中后的全源兜底在途去重（guid）
_FOLDER_FULL_FB: set[str] = set()


def _schedule_folder_full_fallback(guid: str, title: str, want_year: str) -> None:
    """快源未命中时后台跑一次全源搜索并挂到该 guid，不阻塞当前请求。"""
    if guid in _FOLDER_FULL_FB or store.get_title_by_guid(guid):
        return
    _FOLDER_FULL_FB.add(guid)

    async def _job():
        try:
            briefs = await asyncio.wait_for(
                _aggregate_briefs(title, _CHART_BIND_MAXN), timeout=25)
            pick = _pick_folder_brief(title, want_year, briefs)
            if pick is not None:
                bound = dict(pick)
                bound["guid"] = guid
                store.remember_merged([bound])
        except Exception:
            pass
        finally:
            _FOLDER_FULL_FB.discard(guid)

    asyncio.create_task(_job())


def _start_folder_bind(guid: str) -> asyncio.Future:
    """启动（或复用）某 guid 的二级目录绑定单飞任务，返回其 Future。"""
    existing = store.get_title_by_guid(guid)
    if existing:
        f = asyncio.get_event_loop().create_future()
        f.set_result(existing)
        return f
    fut = _FOLDER_BIND_FUT.get(guid)
    if fut is not None:
        return fut
    fut = asyncio.get_event_loop().create_future()
    _FOLDER_BIND_FUT[guid] = fut

    async def _work():
        _deadline = time.monotonic() + _FOLDER_BIND_WAIT
        try:
            title = _folder_title_hint(guid)
            if not title:
                return
            briefs = await asyncio.wait_for(
                _aggregate_briefs(title, _CHART_BIND_MAXN, fast_only=True,
                                  soft_timeout=_FOLDER_FAST_SOFT, include_t4=False,
                                  # 二级目录交互入口：最快源(lzi ~0.46s)给够首屏量即提前
                                  # 返回；同时给足硬超时（覆盖并发争用下的慢返回），
                                  # 避免本来能命中的快源搜索被短超时截断而返回空。
                                  early_min_wait=_FOLDER_EARLY_WAIT,
                                  early_min_results=_FOLDER_EARLY_RESULTS),
                timeout=_FOLDER_FAST_HARD)
            want_year = str((chart_cache.get_guid(guid) or {}).get("year") or
                            (catalog.find_brief_by_guid(guid) or {}).get("year") or "")
            # 复用榜单匹配规则：允许国语/粤语等语言后缀，拒绝任意包含误绑。
            pick = _pick_folder_brief(title, want_year, briefs)
            # 快源未命中：在总预算的剩余窗口内立即补一次全源（含慢源），充分等待其返回，
            # 避免短窗口提前放弃导致客户端先渲染“无数据”再自动重拉。超时才交给后台兜底。
            if pick is None:
                _left = _deadline - time.monotonic()
                if _left > 0.3:
                    try:
                        full = await asyncio.wait_for(
                            _aggregate_briefs(title, _CHART_BIND_MAXN,
                                              soft_timeout=_left),
                            timeout=_left)
                        pick = _pick_folder_brief(title, want_year, full)
                    except Exception:
                        pick = None
            if pick is not None:
                bound = dict(pick)
                bound["guid"] = guid
                store.remember_merged([bound])
                schedule_prewarm_title(guid, 0)
                if not fut.done():
                    fut.set_result(bound)
            else:
                # 实时预算耗尽仍无：后台静默跑全源（含慢源），命中后落库，
                # 用户重进/客户端自动重拉即有，去重避免压源站。
                _schedule_folder_full_fallback(guid, title, want_year)
        except Exception:
            pass
        finally:
            _FOLDER_BIND_FUT.pop(guid, None)
            if not fut.done():
                fut.set_result(None)

    asyncio.create_task(_work())
    return fut


def prefetch_folder_binding(guid: str) -> None:
    """非阻塞预绑定：folder 详情请求时提前触发，利用详情→列表的时间窗完成搜索。

    已绑定/已在途则直接复用，不重复扇出；调用方无需 await。"""
    try:
        if store.get_title_by_guid(guid):
            return
        _start_folder_bind(guid)
    except Exception:
        pass


async def ensure_folder_bound(guid: str) -> dict | None:
    """二级线路墙按需绑定：保证该 guid 在 store 有可播放线路。

    已绑定直接返回；否则按片名快速全网搜索（快源软超时，与二级目录等待窗口匹配），
    命中后把结果身份换成该 guid 落库。单飞去重，第二次进入复用在途/已落库结果。
    仅用同名同年匹配，绝不放宽到错片。"""
    existing = store.get_title_by_guid(guid)
    if existing:
        return existing
    fut = _start_folder_bind(guid)
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout=_FOLDER_BIND_WAIT)
    except Exception:
        pass
    return store.get_title_by_guid(guid)


def chart_guid_by_media(mg: str) -> str:
    """识别请求路径里的 mg 是否属于榜单/热门片。mg 可能【本身就是 chart guid】
    （媒体库直进），也可能是其确定性 media_guid（详情页进入/已绑定），二者都要认。"""
    if mg and chart_cache.is_chart_guid(mg):
        return mg
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    if og and chart_cache.is_chart_guid(og):
        return og
    # 重启后 _PENDING_MEDIA_CHART 已清空、记录也未绑定：mg 是 media_guid(chart_guid)
    # 的确定性哈希，直接在榜单缓存里反查，保证占位自修复通道仍能找到并触发绑定。
    return _chart_guid_scan(mg)


def _chart_guid_scan(mg: str) -> str:
    try:
        for cat, _t, _c in chartmod.CHART_CATEGORIES:
            for it in chart_cache.items(cat):
                g = it.get("guid")
                if g and (media_guid(g) == mg or video_guid(g) == mg):
                    return g
    except Exception:
        return ""
    return ""


def _title_for_media(mg: str) -> str:
    """media/video guid → 片名。优先用已绑定记录；未绑定时在榜单缓存里反查，
    供手切网盘线（不依赖 CMS 绑定）按片名搜索转存。"""
    try:
        b = store.resolve_media_guid(mg)
        t = (b or {}).get("title") or (b or {}).get("ep_title") or ""
        if t:
            return t
        og = chart_guid_by_media(mg)
        if og:
            for cat, _t, _c in chartmod.CHART_CATEGORIES:
                for it in chart_cache.items(cat):
                    if it.get("guid") == og:
                        return str(it.get("title") or "")
    except Exception:
        return ""
    return ""


# 普通库片“补齐全网同名线路”的后台状态（与榜单绑定分开，避免误判失败）：
# og -> 上次尝试时间。成功合并更多线路后线路已在记录内，可短周期复查新源。
_LINE_FILL_TS: dict[str, float] = {}
_LINE_FILL_TTL = 10 * 60
_LINE_FILL_MIN = 6   # 记录内线路数达到该值即视为已较全，不再补
# 补线并发闸门：高负载/批量浏览时避免几十个聚合任务同时抢占事件循环，
# 把海报/播放请求拖到十几秒。限制同时在途的补线任务数。
_LINE_FILL_SEM = asyncio.Semaphore(2)
_LINE_FILL_INFLIGHT = 0


def start_fill_lines(og: str) -> None:
    """仅做同名同年线路数据聚合，不改变242的竞速/选线/清单/分片策略。
    最多2个后台任务；首次播放仍先用现有线路，聚合完成后重开线路菜单显示全集。"""
    if not og or chart_cache.is_chart_guid(og):
        return
    now = time.time()
    if now - _LINE_FILL_TS.get(og, 0) < _LINE_FILL_TTL:
        return
    cur = store.get_title_by_guid(og) or {}
    if len(cur.get("lines") or {}) >= _LINE_FILL_MIN:
        _LINE_FILL_TS[og] = now
        return
    _LINE_FILL_TS[og] = now

    async def work():
        global _LINE_FILL_INFLIGHT
        async with _LINE_FILL_SEM:
            _LINE_FILL_INFLIGHT += 1
            try:
                await _fill_lines_work(og, cur)
            finally:
                _LINE_FILL_INFLIGHT -= 1

    asyncio.create_task(work())


async def _fill_lines_work(og: str, cur: dict) -> None:
    """单个补线任务的实际工作（从 start_fill_lines 拆出，便于并发闸门控制）。"""
    try:
        title = cur.get("title") or ""
        want_year = str(cur.get("year") or "")
        want = _norm_title(title)
        if not want:
            return
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=20, fast_only=True,
                              soft_timeout=6.0), timeout=12)
        # 选同名同部：规范名【完全相等】；年份强一致——当前记录有年份时只接受
        # 同年候选（候选缺年份也不并入），双方都缺年份才放宽，防止同名不同片串源。
        pick = None
        for b in briefs:
            nb = _norm_title(b.get("title", ""))
            by = str(b.get("year") or "")
            if nb != want:
                continue
            if (want_year or by) and by != want_year:
                continue
            if pick is None or len(b.get("lines") or {}) > \
                    len(pick.get("lines") or {}):
                pick = b
        if not pick:
            return
        merged = dict(cur)
        lines = dict(cur.get("lines") or {})
        added = 0
        for lk, eps in (pick.get("lines") or {}).items():
            if not eps or lk in lines:
                continue
            lines[lk] = eps
            added += 1
        if not added:
            return
        merged["lines"] = lines
        # 保持当前记录身份（og 不变，播放链/历史不断），只扩充线路
        merged["guid"] = og
        store.remember_merged([merged])
        _log.info("补线: 《%s》 +%d 条 (共 %d 条线路)",
                  title, added, len(lines))
        # 补进了更多源后重选本片最快线：
        #  - 此前【没有任何预测】（首探时目录片只有单一源/尚无廉价 m3u8，未形成冠军）
        #  - 或只预测到慢档（≥SLOW_SEC）
        # 都要清掉并在更全的候选里重新预热，否则这类片补线后永远不会再被预热，
        # 首次点击只能现场冷竞速 ~2.4s。
        try:
            ekey = f"{og}|0"
            lat = linepredict.latency(ekey)
            if lat is None or lat >= linepredict.SLOW_SEC:
                if lat is not None:
                    linepredict.invalidate(ekey)
                linepredict.clear_inflight(ekey)
                schedule_prewarm_title(og, 0)
        except Exception:
            pass
    except asyncio.TimeoutError:
        # 高负载下补线超时是常态，降级为 debug，不打 traceback 刷屏/占用 IO
        _log.debug("补线超时 og=%s", og)
    except Exception:
        _log.debug("补线失败 og=%s", og, exc_info=True)


async def resolve_chart_by_media(mg: str) -> dict | None:
    """播放页内用 media_guid 找回榜单片并等待其后台绑定完成。"""
    target = store.resolve_media_guid(mg)
    if target:
        return target
    og = chart_guid_by_media(mg)
    if og:
        await resolve_chart(og)
        _PENDING_MEDIA_CHART.pop(mg, None)
        return store.resolve_media_guid(mg)
    return None


def _instant_play_response(mg: str, body_video_guid: str = ""):
    """榜单源尚未绑定时的瞬时播放返回：先把播放器带进加载页，真正匹配延后到 preset。"""
    play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index=0"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": play_link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": body_video_guid or vg_for(mg),
        "video_index": 0, "wrapper": "HLS",
        "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


def _instant_stream_payload(mg: str):
    """与 _instant_play_response 配套：只返回最小可播元数据，绝不等待搜源。"""
    f = {
        "guid": mg, "path": "在线/加载中.m3u8", "file_name": "加载中.m3u8",
        "size": 0, "timestamp": 0, "type": 1, "sort_num": 0, "can_play": 1,
        "play_error": "", "create_time": 0, "update_time": 0,
        "parent_fv_guid": "", "file_birth_time": 0, "progress_thumb_hash_dir": "",
    }
    v = {
        "media_guid": mg, "title": "", "guid": vg_for(mg),
        "resolution_type": "1080p", "color_range_type": "SDR",
        "codec_name": "h264", "codec_type": "video", "color_range": "tv",
        "profile": "High", "index": 0, "width": 1920, "height": 1080,
        "coded_width": 0, "coded_height": 0, "display_aspect_ratio": "16:9",
        "pix_fmt": "yuv420p", "level": "40", "color_space": "",
        "color_transfer": "", "color_primaries": "", "duration": 0,
        "dv_profile": 0, "refs": 1, "r_frame_rate": "25 fps",
        "avg_frame_rate": "25 fps", "bits_per_raw_sample": "8", "bps": 0,
        "progressive": 1, "bit_depth": 8, "wrapper": "HLS",
        "create_time": 0, "update_time": 0, "rotation": 0, "ext1": 0,
        "is_bluray": False,
    }
    return {
        "files": [f], "video_streams": [v], "audio_streams": [],
        "subtitle_streams": [], "qualities": [], "cloud_storage_info": None,
    }


def _instant_stream_response(mg: str):
    return JSONResponse({"code": 0, "msg": "",
                         "data": _instant_stream_payload(mg)})


def _no_source_response():
    # 明确属于我们的在线榜单片、但源解析失败/暂无线路：返回本地可识别错误，
    # 不能转发官方（它不认识虚拟 media guid，会报“权限不足/Auth Failed”）。
    return JSONResponse({"code": 4001, "msg": "暂无可用在线片源，请稍后重试或切换其他影片",
                         "data": None}, status_code=200)


def hot_cat_title(cat: str) -> str:
    for c, title, _ in HOT_CATEGORIES:
        if c == cat:
            return title
    return HOT_DEFAULT_TITLE


def hot_cat_category(cat: str) -> str:
    for c, _, mdbcat in HOT_CATEGORIES:
        if c == cat:
            return mdbcat
    return "Mix"


def is_hot_guid(guid: str) -> bool:
    return guid in HOT_GUID_CAT or guid == HOT_LEGACY_GUID


def b64e(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


async def _pan_cache_cleanup_once() -> dict:
    """对已登录的夸克/UC 各执行一次过期转存清理（lite 无 pan 模块则跳过）。"""
    out = {}
    try:
        ttl = int(float(config.get().get("pan_cache_ttl_hours", 6) or 6)) * 3600
    except (TypeError, ValueError):
        ttl = 6 * 3600
    for name, modname in (("quark", "pan"), ("uc", "panuc")):
        try:
            mod = __import__(modname)
        except Exception:
            continue
        try:
            if not getattr(mod, "logged_in", lambda: False)():
                continue
            res = await asyncio.to_thread(mod.cleanup_expired, ttl)
            if res and res.get("deleted"):
                _log.info("网盘缓存清理 %s 删除=%d 释放=%.1fMB",
                          name, res.get("deleted", 0),
                          res.get("freed", 0) / 1048576)
            out[name] = res
        except Exception:
            _log.info("网盘缓存清理失败 %s", name, exc_info=True)
    return out


async def lifespan(app: FastAPI):
    # 先从磁盘恢复榜单/热门缓存：重启后内存为空，若不 load，热门媒体库的
    # media_guid 无法反查到 chart_guid（chart_guid_by_media/_chart_guid_scan 都依赖
    # 这份缓存），hls_main 拿不到任何线路、只能一直回“加载中”占位；海量媒体库不依赖
    # 榜单缓存故表现为秒播。磁盘恢复是同步本地 IO，先让热门库立即可用，后台再联网刷新。
    try:
        chart_cache.load()
    except Exception as exc:  # noqa: BLE001
        _log.warning("chart cache load on startup failed: %s", exc)
    # 方案B：从磁盘恢复榜单绑定（线路），重启后热门片无需重新全网搜源
    try:
        _n = _restore_chart_bindings()
        if _n:
            _log.info("chart bindings restored on startup: %d", _n)
    except Exception as exc:  # noqa: BLE001
        _log.warning("chart binding restore failed: %s", exc)
    # 方案B：同时恢复冠军线记忆（重启即秒播，不用现建播放清单）
    try:
        if prewarm_persist_mode():
            linepredict.set_persist(True)
    except Exception:  # noqa: BLE001
        pass
    # 网盘转存缓存定时清理（默认保留 6h，每小时检查；配置关闭则不跑）。
    async def _pan_cache_loop():
        try:
            await asyncio.sleep(90)  # 启动后等服务稳定再首轮
            while True:
                try:
                    if bool(config.get().get("pan_cache_cleanup_enabled", True)):
                        await _pan_cache_cleanup_once()
                except Exception:
                    pass
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
    app.state.pan_cache_task = asyncio.create_task(_pan_cache_loop())

    app.state.up = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=UPSTREAM_SOCK),
        timeout=httpx.Timeout(120), trust_env=False)
    app.state.ext = httpx.AsyncClient(
        timeout=httpx.Timeout(30), trust_env=False, follow_redirects=True)
    # 海报专用客户端：与预热/播放的 ext 连接池隔离，避免全量预热等后台任务耗尽连接
    # 或污染 keepalive 后海报请求被拖到几十秒。限制并发连接，读超时单独收窄。
    app.state.poster_http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8.0, read=15.0, write=10.0, pool=5.0),
        trust_env=False, follow_redirects=True,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
    app.state.stop_hot = asyncio.Event()
    app.state.hot_task = asyncio.create_task(_hot_worker(app))
    # 重启后后台预热热门四库首屏（纯 CMS 冠军线），消除冷启动首批片慢/无线路
    app.state.startup_warm = asyncio.create_task(startup_prewarm_hot())

    # 源健康画像：每 5 分钟把内存统计落盘一次（热路径零 IO）
    async def _health_flush_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(sourcehealth.flush)
            except Exception:
                pass
    app.state.health_task = asyncio.create_task(_health_flush_loop())


    # 后台只读检查 GitHub 更新（仅提示，绝不自动安装）
    app.state.stop_update = asyncio.Event()
    app.state.update_task = asyncio.create_task(
        updater.periodic_loop(app.state.stop_update))


    try:
        yield
    finally:
        app.state.stop_hot.set()
        app.state.hot_task.cancel()
        try:
            app.state.pan_cache_task.cancel()
        except Exception:
            pass
        try:
            await app.state.hot_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            app.state.health_task.cancel()
        except Exception:
            pass
        try:
            await asyncio.to_thread(sourcehealth.flush)
        except Exception:
            pass
        try:
            app.state.stop_update.set()
            app.state.update_task.cancel()
            await app.state.update_task
        except (asyncio.CancelledError, Exception):
            pass
        if t4_media is not None:
            await t4_media.metadata.close()
        await app.state.up.aclose()
        await app.state.ext.aclose()
        # 关闭前把防抖未落盘的在线资源缓存同步写一次（issue #42 异步落盘配套）
        try:
            await asyncio.to_thread(store.close)
        except Exception:
            pass


async def _hot_worker(app: FastAPI):
    """榜单 + 海量库路由后台刷新。启动先读旧缓存，稍等再刷新；之后按配置轮询。"""
    await asyncio.sleep(4)
    while not app.state.stop_hot.is_set():
        cfg = config.get()
        charts_on = cfg.get("hot_library_enabled", True)
        catalog_on = cfg.get("catalog_library_enabled", True)
        interval = max(1800, int(cfg.get("hot_refresh_interval", 6 * 3600)))
        if charts_on and chart_cache.stale(interval):
            try:
                await asyncio.wait_for(chart_cache.refresh(force=True),
                                       timeout=chartmod.REFRESH_TIMEOUT)
            except asyncio.TimeoutError:
                _log.warning("chart refresh timed out; keeping previous cache")
            except Exception as exc:
                _log.warning("chart refresh failed, keeping previous cache: %s", exc)
        if catalog_on and catalog.routes_stale():
            try:
                await asyncio.wait_for(
                    catalog.refresh_routes(config.enabled_sources()),
                    timeout=90)
            except Exception as exc:
                _log.warning("catalog routes failed: %s", exc)
        try:
            await asyncio.wait_for(app.state.stop_hot.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


app = FastAPI(lifespan=lifespan)

import logging
_log = logging.getLogger("fnmedia")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
# 关闭 httpx/httpcore 每条请求一行的 INFO 噪音（建库/资源站轮询会刷爆日志）；
# 保留我们自己在 uvicorn/fnmedia logger 中打印的访问日志。
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection",
               "httpcore._sync.connection", "httpcore._async.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _diagnostic_query(request: Request) -> str:
    return urllib.parse.urlencode([
        (k, "<redacted>" if k.lower() in (
            "playlink", "play-link", "token", "authorization") else v)
        for k, v in request.query_params.multi_items()])


@app.middleware("http")
async def _access(request: Request, call_next):
    started = time.monotonic()
    # 在线播放历史按飞牛账号分区（issue #5）：为本次请求设置当前用户上下文。
    # fnusers 带 30s 缓存，识别失败/无 token 落到全局分区，行为同旧版。
    _hist_tokens = None
    try:
        if request.url.path.startswith(("/v/api/v1/play/", "/v/api/v1/item/",
                                        "/v/api/v1/play", "/v/media/online")):
            _u = fnusers.resolve(request)
            if _u.get("known"):
                _hist_tokens = histmod.set_current_user(
                    _u.get("guid"), bool(_u.get("is_admin")))
    except Exception:
        _hist_tokens = None
    try:
        resp = await call_next(request)
    finally:
        if _hist_tokens is not None:
            histmod.reset_current_user(_hist_tokens)
    p = request.url.path
    if "/api/" in p or p.startswith(ONLINE_PREFIX) or p.startswith("/v/media"):
        query = _diagnostic_query(request)
        _log.info("%s %s -> %s client=%s elapsed_ms=%d", request.method,
                  (p + ("?" + query if query else ""))[:120],
                  resp.status_code, _client_of(request).label,
                  int((time.monotonic() - started) * 1000))
    _live_record(request, resp.status_code, p)
    return resp


# 实时诊断：需要展示的播放链路路径 -> 阶段标签
_LIVE_STAGES = [
    ("/v/api/v1/mediadb/list", "library", "local"),
    ("/v/api/v1/mdb/", "library", "local"),
    ("/v/api/v1/mediadb/sum", "library", "mixed"),
    ("/v/api/v1/item/list", "browse", "local"),
    ("/v/api/v1/search/list", "search", "mixed"),
    ("/v/api/v1/item/", "detail", "mixed"),
    ("/v/api/v1/season/list", "season", "local"),
    ("/v/api/v1/episode/list", "episode", "local"),
    ("/v/api/v1/stream/list", "streams", "local"),
    ("/v/api/v1/play/info", "play", "local"),
    ("/v/api/v1/stream", "play", "local"),
    ("/v/api/v1/play/play", "play", "local"),
    ("/v/api/v1/media/range/", "media", "local"),
    ("/v/api/v1/favorite/list", "favorite", "mixed"),
    ("/v/api/v1/play/list", "history", "mixed"),
]
# 即使走了官方透传，也属于“本地与官方合并”的路径（不能简单判为纯透传）
_LIVE_MERGE_PATHS = {"/v/api/v1/mediadb/list", "/v/api/v1/mediadb/sum",
                     "/v/api/v1/search/list",
                     "/v/api/v1/favorite/list", "/v/api/v1/play/list"}


def _live_stage(p: str):
    for pref, stage, origin in _LIVE_STAGES:
        if p.startswith(pref):
            return stage, origin
    if p.startswith(ONLINE_PREFIX):
        if p.endswith(".m3u8"):
            return "hls", "local"
        if p.endswith("/seg"):
            return "seg", "local"
        if p.endswith("/direct"):
            return "direct", "local"
        return "media", "local"
    return None, None


def _live_title_for(path: str, query_params) -> str:
    """尽力从播放 URL 的 mg 反查片名，仅用于实时展示。"""
    try:
        if path.startswith(ONLINE_PREFIX):
            mg = path[len(ONLINE_PREFIX) + 1:].split("/", 1)[0]
            tgt = store.resolve_media_guid(mg)
            if tgt:
                return (tgt.get("ep_title") or tgt.get("title") or "")[:40]
        if path.startswith("/v/api/v1/media/range/"):
            mg = path.rsplit("/", 1)[-1]
            tgt = store.resolve_media_guid(mg)
            if tgt:
                return (tgt.get("ep_title") or tgt.get("title") or "")[:40]
    except Exception:
        pass
    return ""


def _live_record(request: Request, status: int, path: str) -> None:
    try:
        stage, default_origin = _live_stage(path)
        if not stage:
            return
        touched_up = bool(getattr(request.state, "live_upstream", False))
        origin = getattr(request.state, "live_origin", "") or (
            "mixed" if touched_up and path in _LIVE_MERGE_PATHS
            else ("upstream" if touched_up else default_origin))
        title = _live_title_for(path, request.query_params)
        ip = request.headers.get("x-real-ip") or \
            request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
            (request.client.host if request.client else "")
        note = ""
        if status >= 400:
            note = {403: "源站鉴权拒绝", 404: "无此片源/线路",
                    500: "服务端错误", 502: "上游抓取失败",
                    503: "暂无可播片源", 504: "上游超时"}.get(status, "请求失败")
        livemod.record(request.headers.get("user-agent", ""), ip,
                       request.method, path, _diagnostic_query(request), status,
                       origin, title=title, stage=stage, note=note)
    except Exception:
        pass  # 诊断本身绝不影响正常请求


def up_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.up


def ext_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.ext


def _poster_http(req: Request) -> httpx.AsyncClient:
    """海报专用客户端（连接池与预热/播放隔离）；未就绪时回退 ext。"""
    try:
        c = req.app.state.poster_http
        if c is not None and not c.is_closed:
            return c
    except Exception:
        pass
    return req.app.state.ext


def src_client(req: Request) -> httpx.AsyncClient:
    """访问资源站取源（搜索/分类/探测/解析播放地址）的客户端。

    遵循「源代理」开关 source_proxy_mode：off 直连 / on 全走代理 /
    auto 直连失败按主机自动切代理。代理 URL 或模式变化时惰性重建。
    媒体分片/海报等流量仍走直连 ext_client，不增加代理带宽。"""
    px = netutil.source_proxy()
    mode = netutil.proxy_mode()
    st = req.app.state
    cur = getattr(st, "src_proxy_key", None)
    key = (mode, px)
    if not hasattr(st, "src") or cur != key:
        old = getattr(st, "src", None)
        # px=None：交给 netutil 按模式构造（auto 时为自适应客户端）
        st.src = netutil.async_client(timeout=30.0)
        st.src_proxy_key = key
        st.src_proxy_url = px
        if old is not None:
            try:
                asyncio.create_task(old.aclose())
            except Exception:
                pass
    return st.src


# 源站/CDN 偶发抖动（keep-alive 失效、瞬时 5xx/429、读超时）在长视频播放中必然出现。
# 一次失败就给播放器 502 会表现为“播到一半弹出播放出错”。所有外站 GET
# （m3u8、分片、密钥、直链）统一走带退避的重试。
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRIABLE_EXC = (httpx.TimeoutException, httpx.TransportError)


async def ext_get_retry(req: Request, url: str, headers: dict | None = None,
                        *, tries: int = 3, timeout: float | None = 20.0):
    """带重试的外站 GET。成功（2xx/3xx 或不可重试状态码）即返回；网络异常或
    可重试状态码指数退避后重试，最后一次把原始响应/异常交给调用方。"""
    client = ext_client(req)
    last_exc: Exception | None = None
    r = None
    for attempt in range(tries):
        try:
            r = await client.get(url, headers=headers or {}, timeout=timeout)
            if r.status_code not in _RETRY_STATUS or attempt == tries - 1:
                return r
            await r.aclose()
        except _RETRIABLE_EXC as exc:
            last_exc = exc
        if attempt < tries - 1:
            await asyncio.sleep(0.25 * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    return r  # type: ignore[return-value]


# 透传上游响应头时必须丢弃的 hop-by-hop / 会与本代理冲突的头。
_HOP_HEADERS = {"connection", "content-length", "content-encoding",
                "transfer-encoding", "keep-alive", "upgrade", "proxy-authenticate",
                "proxy-authorization", "te", "trailers", "host",
                # server/date 由本服务 uvicorn 自行生成；透传上游会出现重复的
                # server/date 同名头，移动/TV 严格播放器可能判为非法响应（http error）。
                "server", "date"}
# 上游资源站常自带 CORS 头；若再叠加我们的 ACAO，会产生重复 Access-Control-* 头，
# 属于非法响应（同名头出现多次且可能取值冲突）。Web 端通常容忍，但移动/TV 的严格
# 播放器会直接判 “http error”。统一丢弃上游 CORS 头，由本服务单点签发。
_UPSTREAM_CORS_HEADERS = {
    "access-control-allow-origin", "access-control-allow-methods",
    "access-control-allow-headers", "access-control-allow-credentials",
    "access-control-max-age", "access-control-expose-headers",
}


def _safe_response_headers(src, drop: "set[str] | None" = None) -> "dict[str, str]":
    """把上游响应头过滤成可安全回传的 latin-1 头字典。

    关键修复：不少资源站会在 Content-Disposition / Set-Cookie / 自定义头里塞中文
    （如 filename="第一集.ts"）。HTTP 头只允许 latin-1，原值直接交给 Starlette 会抛
    UnicodeEncodeError 导致整个响应 500，播放器拉到该分片/清单即“播放出错”。
    这里：①丢 hop-by-hop 与调用方指定头；②值无法 latin-1 编码的头一律丢弃
    （Content-Disposition 对解码无意义），绝不因一个头毁掉整个播放响应。
    """
    blocked = _HOP_HEADERS | _UPSTREAM_CORS_HEADERS | (drop or set())
    out: dict[str, str] = {}
    for k, v in src.items():
        if k.lower() in blocked:
            continue
        try:
            v.encode("latin-1")
        except (UnicodeEncodeError, AttributeError):
            continue  # 非 latin-1（含中文）头：对播放无价值，丢弃
        out[k] = v
    return out


async def forward_upstream(request: Request) -> Response:
    """原样透传官方后端（走 unix socket）。"""
    # 实时诊断：标记本请求接触过官方后端
    try:
        request.state.live_upstream = True
    except Exception:
        pass
    client = up_client(request)
    url = "http://localhost" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("connection", "content-length", "host")}
    headers["Host"] = "localhost"
    try:
        r = await client.request(request.method, url, headers=headers, content=body)
    except Exception as exc:
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}", "data": None},
                            status_code=502)
    resp_headers = _safe_response_headers(r.headers)
    return Response(content=r.content, status_code=r.status_code,
                    headers=resp_headers,
                    media_type=r.headers.get("content-type"))


# 需要逐块流式转发的响应类型（视频/文件/媒体流）。这些绝不能整包缓冲，
# 否则本地播放几 GB 的影片会被一次性读进内存，打满 RAM+swap 拖死整机。
_STREAM_CONTENT_TYPE = (
    "video/", "audio/", "application/octet-stream", "application/x-mpegurl",
    "application/vnd.apple.mpegurl", "image/", "multipart/", "binary",
)


def _is_stream_response(headers) -> bool:
    ct = (headers.get("content-type") or "").lower()
    return any(t in ct for t in _STREAM_CONTENT_TYPE)


async def forward_upstream_streaming(request: Request) -> Response:
    """流式原样透传官方后端：逐块转发、不缓冲整文件，支持 Range 拖动。

    本地库视频/下载等大响应必须走这里。仅在判定为大文件/媒体流时流式，
    普通 JSON 仍走缓冲版 forward_upstream（具名 JSON 路由直接调它，不受影响）。
    """
    try:
        request.state.live_upstream = True
    except Exception:
        pass
    client = up_client(request)
    url = "http://localhost" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("connection", "content-length", "host")}
    headers["Host"] = "localhost"
    # 大文件读取不能受普通 120s 总超时限制：用极宽 read 超时，连接阶段保持短超时。
    stream_timeout = httpx.Timeout(connect=10.0, read=None, write=120.0, pool=10.0)
    req = client.build_request(request.method, url, headers=headers, content=body,
                               timeout=stream_timeout)
    try:
        up = await client.send(req, stream=True)
    except Exception as exc:
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}",
                             "data": None}, status_code=502)

    # 仅当能确定是“小响应”时才整包缓冲（普通 JSON API）；媒体/文件、超大响应、
    # 或大小未知（分块）一律流式，绝不把大文件读进内存。
    _SMALL_MAX = 4 * 1024 * 1024
    ct = (up.headers.get("content-type") or "").lower()
    looks_json = ("json" in ct) or ct.startswith("text/") or ct == ""
    try:
        clen = int(up.headers.get("content-length", "-1"))
    except (TypeError, ValueError):
        clen = -1
    buffer_small = (request.method != "HEAD" and looks_json
                    and 0 <= clen <= _SMALL_MAX)
    if buffer_small:
        try:
            data = await up.aread()
            resp_headers = _safe_response_headers(up.headers)
            return Response(content=data, status_code=up.status_code,
                            headers=resp_headers,
                            media_type=up.headers.get("content-type"))
        finally:
            await up.aclose()

    resp_headers = _safe_response_headers(up.headers)
    # 流式透传需保留 length/range/encoding（aiter_raw 是原始字节，与 encoding 一致），
    # 这几个被默认安全过滤丢弃，这里显式补回；播放器拖动(Range)依赖它们。
    for _k in ("content-length", "content-range", "content-encoding",
               "accept-ranges"):
        _v = up.headers.get(_k)
        if _v is not None:
            try:
                _v.encode("latin-1")
                resp_headers[_k] = _v
            except Exception:
                pass

    async def gen():
        try:
            async for chunk in up.aiter_raw(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        except Exception:
            # 客户端拖动/中断会触发取消：静默结束，连接由 finally 归还
            pass
        finally:
            await up.aclose()

    return StreamingResponse(gen(), status_code=up.status_code,
                             headers=resp_headers,
                             media_type=up.headers.get("content-type"))


# ------------------------------------------------------------- 在线搜索

async def _aggregate_briefs(keyword: str, maxn: int | None = None,
                            fast_only: bool = False,
                            soft_timeout: float | None = None,
                            *, include_t4: bool = True,
                            refresh: bool = False,
                            early_min_wait: float | None = None,
                            early_min_results: int | None = None) -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。
    fast_only=True 时只查秒回的标准 CMS 直链源（跳过加固/drpy 等慢源），供播放
    失败时快速回源，避免被 redroid 慢源拖到网关超时。

    结果进绑定聚合缓存池（按 片名·fast·t4 区分），TTL 内同片名的多次绑定/回源/
    搜索复用同一份聚合结果，并以单飞去重并发扇出，不重复拉取所有源。"""
    cache_key = f"{keyword}\x00{int(bool(fast_only))}\x00{int(bool(include_t4))}"
    loop = asyncio.get_event_loop()
    _hit = None if refresh else _AGG_CACHE.get(cache_key)
    if _hit and (time.time() - _hit[0]) < AGG_CACHE_TTL:
        return [dict(b) for b in _hit[1]]
    # refresh=True 用于绑定搜索的网络重试：必须绕过负缓存与旧在途任务，
    # 否则一次偶发空结果会被当成“无源”持续复用。
    _fut = None if refresh else _AGG_INFLIGHT.get(cache_key)
    if _fut is not None and not _fut.done():
        try:
            return [dict(b) for b in await _fut]
        except Exception:
            pass  # 前次扇出失败则自己重试
    fut = loop.create_future()
    _AGG_INFLIGHT[cache_key] = fut
    try:
        result = await __aggregate_briefs_inner(
            keyword, maxn, fast_only, soft_timeout, include_t4,
            early_min_wait=early_min_wait,
            early_min_results=early_min_results)
        # remember_merged 幂等：命中缓存路径也已入库，这里再确保 store 有这些 brief
        try:
            store.remember_merged(result)
        except Exception:
            pass
        _AGG_CACHE[cache_key] = (time.time(), result)
        if len(_AGG_CACHE) > _AGG_CACHE_MAX:
            for _old in list(_AGG_CACHE)[:_AGG_CACHE_MAX // 2]:
                _AGG_CACHE.pop(_old, None)
        if not fut.done():
            fut.set_result(result)
        return [dict(b) for b in result]
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _AGG_INFLIGHT.pop(cache_key, None)


async def __aggregate_briefs_inner(keyword, maxn, fast_only, soft_timeout,
                                   include_t4, *, early_min_wait=None,
                                   early_min_results=None) -> list[dict]:
    # 可选特性源不进入全局搜索（公开构建下该开关恒关，等价于不过滤）。
    sources = [s for s in config.enabled_sources()
               if not (features.ADULT and s.get("adult"))
               and (include_t4 or s.get("kind") != "t4")]
    if fast_only:
        sources = [s for s in sources
                   if not s.get("guard") and not s.get("spider_src")
                   and not str(s.get("id", "")).startswith(("spider-", "drpy-"))
                   and s.get("api")
                   and s.get("id") not in ("dduo", "xiaoya")]
    titles = await providers.search_all(keyword, sources,
                                        soft_timeout=soft_timeout,
                                        budget=5.0 if any(
                                            s.get("kind") == "t4" for s in sources) else None,
                                        early_min_wait=early_min_wait,
                                        early_min_results=early_min_results)
    if maxn is None:
        maxn = int(config.get().get("max_per_keyword", 12))
    # 跨源同名合并：同 规范化片名+年份 聚成一个条目，每个来源各成一条
    # 播放线路（供播放器「切换线路」）；过滤解说/预告/无直链。
    loose_ids = {s["id"] for s in sources if s.get("loose_filter")}
    # T4 search cards intentionally have no episode list. Never merge them by name.
    remote = [inj.brief_of(t) for t in titles if t.locator.get("kind") == "t4"
              and not catalogmod.is_redline(t.title, t.remarks)
              and (t.provider in loose_ids or not config.is_junk(t.title, t.remarks))]
    titles = [t for t in titles if t.locator.get("kind") != "t4"]
    groups: dict[tuple, dict] = {}

    # 预筛可分组标题，并统计每个规范片名的年份（加固/爬虫源常缺年份）。
    # 归并规则：年份缺失的结果并入同规范片名“有年份且出现最多”的组，
    # 避免加固源因缺年份单独成组、导致无法作为该影片的可切换线路。
    cand: list = []
    year_votes: dict[str, dict[str, int]] = {}
    for t in titles:
        try:
            if catalogmod.is_redline(t.title, t.remarks):
                continue
        except Exception:
            pass
        if t.provider not in loose_ids and config.is_junk(t.title, t.remarks):
            continue
        playable = [e for line in t.sources.values() for e in line
                    if e.direct or e.resolve]
        if not playable:
            continue
        nt = _norm_title(t.title)
        cand.append((t, nt, playable))
        if t.year:
            year_votes.setdefault(nt, {})
            year_votes[nt][t.year] = year_votes[nt].get(t.year, 0) + 1

    def _group_key_for(ntitle: str, year: str) -> tuple:
        # 年份是不同版本/同名片的硬边界。缺年份的源只与其他【同样缺年份】的同名
        # 结果合并（独立成组），绝不再乐观并入“有年份且票数最多”的组——否则同名的
        # 老片/短片/不同版本会被并进同一菜单，切线路就播成与海报不符的另一部片。
        return (ntitle, year or "")

    for t, nt, playable in cand:
        key = _group_key_for(nt, t.year)
        g = groups.get(key)
        if g is None:
            g = {"rep": t, "lines": {}}
            groups[key] = g
        for lname, eps in t.sources.items():
            play = [e for e in eps if e.direct or e.resolve]
            if not play:
                continue
            lkey = f"{t.provider}::{lname}"
            # 同 key 保留直链更多（更优）者；否则集数更多者
            cur = g["lines"].get(lkey)
            nd_new = sum(1 for e in play if e.direct)
            nd_cur = sum(1 for e in (cur or []) if e.get("direct"))
            if cur is None or nd_new > nd_cur or \
                    (nd_new == nd_cur and len(play) > len(cur)):
                g["lines"][lkey] = [e.__dict__ for e in play]
    # 排序：热门资源优先高质量源（源质量权重），同权重再按可播直链总数（跨线路）降序；
    # 开启「自动优选」时叠加实测健康度（死亡/高失败率/慢源排后，issue #4）。
    prefer_q = bool(config.get().get("prefer_quality_source", True))
    auto_best = bool(config.get().get("auto_best_source", True))
    overrides = config.get().get("source_priority") or {}
    _now_h = time.time()

    def _group_key(g) -> tuple:
        lines_n = sum(len(v) for v in g["lines"].values())
        if not prefer_q:
            base = (0, lines_n)
        else:
            pr = providers.source_priority({"id": g["rep"].provider}, overrides)
            base = (pr, lines_n)
        if auto_best:
            # 用该标题首个线路对应的源 key 取健康权重；不健康源的组整体下沉
            any_line = next(iter(g["lines"].keys()), "")
            sk = sourcehealth.source_key_of(any_line)
            grade, avg_lat, fail = sourcehealth.weight(sk, _now_h) if sk else (1, 5.0, 1.0)
            # 把健康档放在质量权重之后、直链数之前：高质量死源也不会排到健康源前太多
            return (base[0], -grade, lines_n, -fail, -avg_lat)
        return base

    ordered = sorted(groups.values(), key=_group_key, reverse=True)[:maxn]
    briefs = []
    for g in ordered:
        brief = inj.brief_of(g["rep"])
        brief["lines"] = g["lines"]
        # 预算海报角标：网盘资源名 + 最优线路健康等级（搜索/媒体库海报通用）
        try:
            _pl, _hg, _ = _brief_line_flags(brief)
            brief["_pan"] = _pl
            brief["_health"] = _hg
        except Exception:  # noqa: BLE001
            pass
        briefs.append(brief)
    return store.remember_merged((remote + briefs)[:maxn])


async def online_search(keyword: str, client: httpx.AsyncClient,
                        fast_only: bool = True) -> list:
    now = time.time()
    cache_key = keyword if not fast_only else keyword + "\x00fast"
    if cache_key in _search_cache and now - _search_cache[cache_key][0] < CACHE_TTL:
        return _search_cache[cache_key][1]
    fut = _search_inflight.get(cache_key)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _search_inflight[cache_key] = fut

        async def work():
            try:
                # 全局搜索框走 fast_only：只查秒回的标准 CMS 直链源（实测 ~2.4s
                # 数十条）。若带上几十个 redroid 真机源（加固/spider/drpy），它们
                # 争抢仅有的 3 台设备、单源可能长时间不返回，而搜索 HTTP 预算很短，
                # 会导致整个在线搜索结果为空。真机源在各自媒体库/详情与播放回源里
                # 仍照常使用，不影响其可见性。
                briefs = await _aggregate_briefs(
                    keyword, fast_only=fast_only,
                    # 软超时略小于搜索路由 6s 硬窗口：到时返回已完成源的部分
                    # 结果，避免 gather 全有或全无导致个别慢源拖空整个在线结果。
                    soft_timeout=5.2 if fast_only else None)
                items = [_get_t4_browser().media_card(b) if
                         b.get("locator", {}).get("kind") == "t4" else inj.search_item(b)
                         for b in briefs]
                # 搜索结果首屏：后台轻量预测每条片的最快线，点击即秒播（去重/限并发在内部）
                for b in briefs[:10]:
                    if b.get("locator", {}).get("kind") != "t4":
                        schedule_prewarm_title(b.get("guid", ""), 0)
                _search_cache[cache_key] = (time.time(), items)
                if not fut.done():
                    fut.set_result(items)
            except Exception:
                # 客户端断连会让 fut 被取消，此时再 set_result 会抛 InvalidStateError
                if not fut.done():
                    fut.set_result([])
            finally:
                _search_inflight.pop(cache_key, None)

        asyncio.create_task(work())
    return await fut


def _norm_title(t: str) -> str:
    """跨源同名归并的片名规范化：去空白/标点/常见后缀，便于把不同站的同一部片聚到一起。"""
    t = (t or "").strip().lower()
    # 去掉空格与常见分隔标点
    t = re.sub(r"[\s·・:：,，.。!！?？'’\"\-—_()（）\[\]【】]+", "", t)
    # 去掉年份尾缀（年份单独作为合并键）
    t = re.sub(r"(19|20)\d{2}$", "", t)
    return t


_CJK_RE = re.compile(r"[一-鿿]")
# 拼音输入碎片：含音节分隔符 ' 或 ’（gan、gang't、gang'tie'xia）
_PINYIN_SEP_RE = re.compile(r"['’]")


def _should_online(q: str) -> bool:
    """只对"整词"触发在线搜索：
       - 含中文（输入法上屏后）→ 触发
       - 拼音碎片（含 ' 分隔符，或纯小写短音节 ≤6 字符）→ 不触发
       - 英文整词：含空格的多词，或长度 ≥7 的单词（Inception）→ 触发
    """
    if _CJK_RE.search(q):
        return True
    if _PINYIN_SEP_RE.search(q):
        return False
    if " " in q.strip() and len(q.strip()) >= 4:
        return True
    # 纯单词：拼音音节最长 6（zhuang），用 7 作为英文词门槛
    return len(q) >= 7


# ------------------------------------------------------------- 在线热门媒体库

def _brief_line_flags(brief: dict) -> tuple[str, int | None, set]:
    """扫描 brief 的所有线路，返回 (网盘名标注, 最优健康等级, 网盘源key集合)。

    网盘名：任一线路播放地址命中网盘特征即给出（如"夸克"/"UC"/"阿里"），多盘取首个；
    健康等级：在非死线路里取最优（grade 数值越小越优）；全死则返回 3。"""
    pan_names: list[str] = []
    pan_keys: set = set()
    grades: list[int] = []
    for lname, eps in (brief.get("lines") or {}).items():
        src_key = sourcehealth.source_key_of(lname)
        if src_key:
            g = sourcehealth.display_grade(src_key)
            if g is not None:
                grades.append(g)
        for e in eps:
            url = str(e.get("url") or "")
            pv = _pan_match(url) if url else None
            if pv is not None:
                nm = pv.get("name", "")
                tag = nm.replace("网盘", "")
                if tag and tag not in pan_names:
                    pan_names.append(tag)
                if src_key:
                    pan_keys.add(src_key)
    pan_label = "/".join(pan_names[:3])
    # 只在有真实探测样本时显示健康点；全死(3)且无更优时显示红点
    live = [g for g in grades if g != 3] or grades
    grade = min(live) if live else None
    return pan_label, grade, pan_keys


# 向注入层提供海报角标（网盘名 + 健康等级），使所有 search_item 路径统一生效
try:
    def _badge_flags(b):
        pan, health, keys = _brief_line_flags(b)
        try:
            speed = linepredict.grade(f"{b.get('guid', '')}|0")
        except Exception:
            speed = None
        return pan, health, speed
    inj.set_badge_provider(_badge_flags)
except Exception:  # noqa: BLE001
    pass


def _brief_poster_badged(brief: dict) -> str:
    """按 brief 线路生成带角标的海报路径。

    只保留网盘资源 ☁ 文字角标；不再烧录测速/健康圆点——后者需要后端逐张下载
    原图并用 Pillow 合成重编码（to_thread），海报墙几十张图会显著拖慢加载，
    且速度信息已在播放时由自动竞速/线路预测体现，无需画在封面上。"""
    cover = brief.get("poster") or brief.get("cover") or ""
    if not cover:
        return ""
    pan_label, _health_grade, _ = _brief_line_flags(brief)
    try:
        # health_grade / speed_grade 一律不传（None）→ 纯 CMS 片直接透传原图，
        # 零 Pillow 合成开销；仅网盘资源走一次 ☁ 角标合成。
        return inj._poster_badged(cover, pan_label, None, None)
    except Exception:  # noqa: BLE001
        return inj._poster(cover)


def _hot_card(brief: dict, cat: str) -> dict:
    row = inj.search_item(brief)
    # 海报墙角标：网盘资源 + 源健康点（烧进海报图，原生库无需改 UI）
    try:
        bp = _brief_poster_badged(brief)
        if bp:
            row["poster"] = bp
    except Exception:  # noqa: BLE001
        pass
    # 媒体库内不再重复显示【在线】前缀，让海报墙更接近原生媒体库体验
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    row["title"] = title + (f" ·{remarks}" if remarks else "")
    row["ancestor_guid"] = HOT_LIB_GUIDS[cat]
    row["ancestor_name"] = hot_cat_title(cat)
    row["ancestor_category"] = hot_cat_category(cat)
    row["video_guid"] = video_guid(brief["guid"])
    return _vod_directory_card(row)


def _hot_cat_items(cat: str) -> list[dict]:
    return [b for b in hot_cache.items() if inj.hot_category(b) == cat]


def _hot_rows_all(body: dict, cat: str) -> list[dict]:
    items = _hot_cat_items(cat)
    # parent_guid 可能用于目录/分组；热门库只有一层，非根 parent 直接为空。
    parent = (body.get("parent_guid") or "").strip()
    if parent:
        return []
    rows = [_hot_card(b, cat) for b in items]
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        media_wanted = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in media_wanted:
            media_wanted.discard("Movie,TV")
            media_wanted.update({"Movie", "TV"})
        if media_wanted:
            rows = [r for r in rows
                    if r.get("type") in media_wanted or r.get("type") == "Directory"]
        watched = str(tags.get("watched") or "")
        if watched in {"0", "1"}:
            want_watched = watched == "1"
            filtered = []
            for r in rows:
                rec = history.get(r["guid"])
                is_watched = bool(rec and rec.get("duration") and
                                  int(rec.get("ts") or 0) >= int(rec.get("duration") or 0) * 0.95)
                if is_watched == want_watched:
                    filtered.append(r)
            rows = filtered
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows
                    if r.get("type") in wanted or r.get("type") == "Directory"]

    rows = [_apply_marks(r) for r in rows]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]

    col = body.get("sort_column", "create_time")
    order = str(body.get("sort_type", "DESC")).upper()
    reverse = order.lower() != "asc"

    def rank(r: dict):
        g = r["guid"]
        b = hot_cache.get_guid(g) or {}
        h = b.get("hot") or {}
        return (h.get("rank") if reverse else -int(h.get("rank") or 0))

    if col == "sort_title":
        rows.sort(key=lambda r: (r.get("title") or ""), reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        rows.sort(key=lambda r: (r.get("release_date") or ""), reverse=reverse)
    elif col in ("vote_average", "score"):
        def score(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        rows.sort(key=lambda r: (score(r), rank(r)), reverse=True)
    # create_time / 默认：保持 collect_hot 已计算好的热门顺序，不在这里二次排序。
    return rows


def _chart_rows_all(body: dict, cat: str) -> list[dict]:
    items = chart_cache.items(cat)
    parent = (body.get("parent_guid") or "").strip()
    if parent:
        return []
    rows = [chart_card(it) for it in items]
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        media_wanted = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in media_wanted:
            media_wanted.discard("Movie,TV")
            media_wanted.update({"Movie", "TV"})
        if media_wanted:
            rows = [r for r in rows
                    if r.get("type") in media_wanted or r.get("type") == "Directory"]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows
                    if r.get("type") in wanted or r.get("type") == "Directory"]
    rows = [_apply_marks(r) for r in rows]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]

    col = body.get("sort_column", "create_time")
    order = str(body.get("sort_type", "DESC")).upper()
    reverse = order.lower() != "asc"
    # 榜单原始顺序即热度顺序（周榜 rank 在前）。评分/年份/片名排序时保留榜单次序作次级键。
    indexed = list(enumerate(rows))
    if col == "sort_title":
        indexed.sort(key=lambda kv: (kv[1].get("title") or ""), reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        indexed.sort(key=lambda kv: (kv[1].get("release_date") or ""), reverse=reverse)
    elif col in ("vote_average", "score"):
        def sc(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        indexed.sort(key=lambda kv: sc(kv[1]), reverse=True)
    return [r for _, r in indexed]


def _catalog_card(brief: dict, cat: str) -> dict:
    row = inj.search_item(brief)
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    row["title"] = title + (f" ·{remarks}" if remarks else "")
    row["ancestor_guid"] = CATALOG_LIB_GUIDS[cat]
    row["ancestor_name"] = catalogmod.catalog_title(cat)
    row["ancestor_category"] = catalogmod.catalog_mdb(cat)
    row["video_guid"] = video_guid(brief["guid"])
    return _vod_directory_card(row)


def _catalog_filter(rows: list[dict], body: dict) -> list[dict]:
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        mw = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in mw:
            mw.discard("Movie,TV"); mw.update({"Movie", "TV"})
        if mw:
            rows = [r for r in rows
                    if r.get("type") in mw or r.get("type") == "Directory"]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows
                    if r.get("type") in wanted or r.get("type") == "Directory"]
    rows = [_apply_marks(r) for r in rows]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]
    # 海量库由资源站按“最近更新”排序，前端默认排序即保持此顺序；
    # 用户显式按评分/年份/片名排序时在本页内排序（单页内有效）。
    col = body.get("sort_column", "create_time")
    reverse = str(body.get("sort_type", "DESC")).upper() != "ASC"
    if col == "sort_title":
        rows = sorted(rows, key=lambda r: r.get("title") or "", reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        rows = sorted(rows, key=lambda r: r.get("release_date") or "", reverse=reverse)
    elif col in ("vote_average", "score"):
        def sc(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        rows = sorted(rows, key=sc, reverse=True)
    return rows


def _page_args(body: dict, total: int) -> tuple[int, int, int]:
    page_size = max(1, min(int(body.get("page_size") or 22), 100))
    page = int(body.get("page") or body.get("p") or body.get("page_index")
               or body.get("pn") or 1)
    page = max(1, page)
    offset = int(body.get("offset") if body.get("offset") is not None else
                 (page - 1) * page_size)
    offset = max(0, min(offset, total))
    return page_size, offset, min(offset + page_size, total)


async def _json_from_upstream(upstream: Response) -> dict | None:
    try:
        data = json.loads(upstream.body)
        return data if isinstance(data, dict) else None
    except Exception:
        return None




@app.get("/v/api/v1/mediadb/list")
async def mediadb_list(request: Request):
    upstream = await forward_upstream(request)
    # 当前账号（用于按账号显隐；识别失败/管理员安全放行）
    viewer = fnusers.resolve(request)
    # 热门四库与海量四库各有独立开关；两类都关时才完全不注入（单源独立库/加固专区
    # 仍应展示，故这里只判断“聚合四库”开关）。
    if not any_virtual_lib_enabled():
        return upstream
    payload = await _json_from_upstream(upstream)
    dbs = payload.get("data") if payload else None
    vlib_guids = (set(CHART_LIB_GUIDS.values()) | set(CATALOG_LIB_GUIDS.values())
                  | {standalone_lib_guid(s["id"]) for s in config.all_sources()}
                  | {T4_LIB_GUID})
    # 始终由插件重建虚拟库列表：需要按账号授权 / 手动隐藏 / 自定义顺序过滤，不能直接
    # 透传可能已含“旧虚拟库”的上游响应。先剥掉上游里残留的我们自己的虚拟库，再统一注入。
    if isinstance(dbs, list):
        dbs = [d for d in dbs if d.get("guid") not in vlib_guids]
    if not isinstance(dbs, list):
        if not (payload and payload.get("code") == 0):
            dbs = []  # 官方失败：以我们的虚拟库兜底
        else:
            return upstream
    try:
        inserts = []
        if _t4_sources():
            inserts.append(_get_t4_browser().library())
        # 1) 豆瓣真实榜单四库（独立开关 hot_library_enabled）
        if charts_enabled():
            for cat, _t, _c in chartmod.CHART_CATEGORIES:
                ps = []
                for it in chart_cache.items(cat):
                    p = inj._poster(it.get("cover") or "")
                    if p:
                        ps.append(p)
                    if len(ps) >= 8:
                        break
                inserts.append({
                    "guid": CHART_LIB_GUIDS[cat],
                    "title": chartmod.chart_cat_title(cat),
                    "poster": ps[0] if ps else "",
                    "posters": ps,
                    "category": chartmod.chart_cat_mdb(cat),
                    "view_type": 0, "poster_type": 0, "refresh_disabled": True,
                })
        # 2) 海量分类片库（资源站深翻）；仅在已建立分类路由时展示。
        # 海报只读本页/密集缓存，绝不在此联网抓取——TV/安卓 App 对该接口超时很敏感，
        # 一次资源站首页请求可能耗时十余秒，导致客户端提示“加载失败/媒体库不存在”。
        # 海量四库独立开关 catalog_library_enabled
        if catalog_enabled() and any(catalog.available(c) for c, _, _ in catalogmod.CATALOG_CATEGORIES):
            for cat, _t, _c in catalogmod.CATALOG_CATEGORIES:
                if not catalog.available(cat):
                    continue
                ps = [inj._poster(p) for p in catalog.cached_page_posters(cat, 8)]
                inserts.append({
                    "guid": CATALOG_LIB_GUIDS[cat],
                    "title": catalogmod.catalog_title(cat),
                    "poster": ps[0] if ps else "",
                    "posters": ps,
                    "category": catalogmod.catalog_mdb(cat),
                    "view_type": 0, "poster_type": 0, "refresh_disabled": True,
                })
        # 3) 单源独立媒体库（用户在管理页为某个源勾选“作为独立媒体库”）
        for s in standalone_sources_active():
            scope = s["id"]
            if not catalog.standalone_available(scope):
                continue
            ps: list[str] = []
            # 同样只用缓存海报，不联网；后台预热会逐步填充海报
            try:
                for cat in list(catalogmod.CATALOG_CATEGORIES):
                    if catalog.available(cat, scope):
                        for p in catalog.cached_page_posters(cat, 8, scope):
                            if p not in ps:
                                ps.append(p)
                            if len(ps) >= 8:
                                break
                    if len(ps) >= 8:
                        break
            except Exception:
                pass
            inserts.append({
                "guid": standalone_lib_guid(scope),
                "title": standalone_lib_name(s),
                "poster": ps[0] if ps else "",
                "posters": [inj._poster(p) for p in ps],
                "category": "Mix", "view_type": 0, "poster_type": 0,
                "refresh_disabled": True,
            })
        # 4) TVBox 加固源专区（每个启用且运行时在线的加固源一张库卡片）。
        # 海报只读缓存（后台预热），绝不在此联网——该接口对超时极敏感。

        # 5) 小雅分类媒体库（电影/剧集/综艺/动漫/纪录片/短剧 各一张卡片）
        # —— 统一：为每张虚拟库建立 guid→稳定库键 映射（供显隐/授权/排序）——
        guid2key: dict[str, str] = {T4_LIB_GUID: vlibs.K_T4}
        for cat, g in CHART_LIB_GUIDS.items():
            guid2key[g] = vlibs.make_key(vlibs.K_CHART, cat)
        for cat, g in CATALOG_LIB_GUIDS.items():
            guid2key[g] = vlibs.make_key(vlibs.K_CATALOG, cat)
        for s in standalone_sources_active():
            guid2key[standalone_lib_guid(s["id"])] = vlibs.make_key(vlibs.K_STANDALONE, s["id"])

        # 1) 标注库键；2) 过滤“管理员隐藏”与“当前账号无权限”的库
        visible = []
        for d in inserts:
            key = guid2key.get(d["guid"], "")
            d["_vkey"] = key
            if key and not vlibs.account_can_see(viewer, key):
                continue
            visible.append(d)

        # 3) 虚拟库之间按自定义顺序排列（未列出的保持原有次序追加在后）
        ordered_keys = vlibs.lib_order()

        def _ko(d):
            k = d.get("_vkey", "")
            try:
                return ordered_keys.index(k)
            except ValueError:
                return 10_000_000

        indexed = [d for d in visible if _ko(d) < 10_000_000]
        tail = [d for d in visible if _ko(d) >= 10_000_000]
        indexed.sort(key=_ko)
        visible = indexed + tail

        # 4) 相对本地库的位置：front=之前；back=之后；after=插到指定本地库之后。
        pinned_set = config.pinned_lib_guids()
        pinned = [d for d in visible if d["guid"] in pinned_set]
        rest = [d for d in visible if d["guid"] not in pinned_set]
        placement = vlibs.lib_placement_default()
        for i, d in enumerate(pinned):
            d["sort_num"] = 20_000_000 - i
        for d in visible:
            d.pop("_vkey", None)
        if placement == "back":
            for i, d in enumerate(rest):
                d["sort_num"] = 0
            dbs.extend(pinned + rest)
        elif placement == "after":
            anchor = vlibs.lib_anchor()
            pos = next((i for i, d in enumerate(dbs) if d.get("guid") == anchor), -1)
            for i, d in enumerate(rest):
                d["sort_num"] = 10_000_000 - i
            block = pinned + rest
            if pos >= 0:
                for d in reversed(block):
                    dbs.insert(pos + 1, d)
            else:
                # 锚点库不存在（被删/改名）→ 兜底放到本地库之后
                dbs.extend(block)
        else:  # front
            for i, d in enumerate(rest):
                d["sort_num"] = 10_000_000 - i
            dbs[0:0] = rest
            dbs[0:0] = pinned
        if not isinstance(payload, dict):
            payload = {"code": 0, "msg": "", "data": []}
        # 官方该接口失败时我们用虚拟库兜底，必须把整体码改为成功，否则 App 直接报错
        if payload.get("code") != 0:
            payload["code"] = 0
            payload["msg"] = ""
        payload["data"] = dbs
        return JSONResponse(payload)
    except Exception as exc:
        _log.warning("inject virtual mediadb failed: %s", exc)
        return upstream


@app.get("/v/api/v1/mdb/{guid}")
async def mdb_detail(guid: str, request: Request):
    """App（Flutter）打开媒体库前会用 /mdb/<guid> 校验库是否存在。"""
    # 子账号无权限 / 被隐藏的虚拟库：伪装成“库不存在”，防止直接用 guid 进入
    if not _viewer_can_open_lib(request, guid):
        return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})

    if guid == T4_LIB_GUID:
        # 无启用的 T4 源：库视为不存在（-6），且绝不实例化 T4 浏览器（共存契约）。
        if not _t4_sources():
            return JSONResponse({"code": -6, "msg": "T4 library unavailable",
                                 "data": None})
        return JSONResponse({"code": 0, "msg": "", "data": _get_t4_browser().library(detail=True)})
    async def posters_for(items: list[str], n: int = 8) -> list[str]:
        ps = []
        for cover in items:
            p = inj._poster(cover)
            if p:
                ps.append(p)
            if len(ps) >= n:
                break
        return ps

    vguid = None
    if guid in CHART_GUID_CAT:
        cat = CHART_GUID_CAT[guid]
        try:
            posters = await posters_for([it.get("cover") or ""
                                         for it in chart_cache.items(cat)])
        except Exception:
            posters = []
        payload = {
            "guid": guid,
            "name": chartmod.chart_cat_title(cat),
            "category": chartmod.chart_cat_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})

    if guid in CATALOG_GUID_CAT:
        cat = CATALOG_GUID_CAT[guid]
        posters = []
        try:
            if catalog.available(cat):
                posters = [inj._poster(p)
                           for p in catalog.cached_page_posters(cat, 8)]
        except Exception:
            posters = []
        payload = {
            "guid": guid,
            "name": catalogmod.catalog_title(cat),
            "category": catalogmod.catalog_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})

    # 单源独立媒体库
    sscope = standalone_source_of(guid)
    if sscope and any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        posters = []
        try:
            ps: list[str] = []
            for cat in list(catalogmod.CATALOG_CATEGORIES):
                if catalog.available(cat, sscope):
                    for p in catalog.cached_page_posters(cat, 8, sscope):
                        if p not in ps:
                            ps.append(p)
                    if len(ps) >= 8:
                        break
            posters = [inj._poster(p) for p in ps[:8]]
        except Exception:
            posters = []
        payload = {
            "guid": guid, "name": standalone_lib_name(src),
            "category": "Mix",
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})

    # TVBox 加固源专区库

    # 小雅分类媒体库

    # 兼容旧热门库 guid
    if is_hot_guid(guid):
        cat = HOT_GUID_CAT.get(guid) or ("tv" if guid == HOT_LEGACY_GUID else "movie")
        return JSONResponse({"code": 0, "msg": "", "data": {
            "guid": guid, "name": chartmod.chart_cat_title(cat),
            "category": chartmod.chart_cat_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": "", "poster_type": 0, "posters": [],
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True}})

    return await forward_upstream(request)


@app.get("/v/api/v1/mediadb/sum")
async def mediadb_sum(request: Request):
    upstream = await forward_upstream(request)
    if not any_virtual_lib_enabled():
        return upstream
    payload = await _json_from_upstream(upstream)
    if not payload or not isinstance(payload.get("data"), dict):
        return upstream
    total_added = 0
    if _t4_sources() and _viewer_can_open_lib(request, T4_LIB_GUID):
        count = len(_t4_sources())
        payload["data"][T4_LIB_GUID] = count
        total_added += count
    if charts_enabled():
        for cat, _t, _c in chartmod.CHART_CATEGORIES:
            n = len(chart_cache.items(cat))
            if n:
                payload["data"][CHART_LIB_GUIDS[cat]] = n
                total_added += n
    if catalog_enabled():
        for cat, _t, _c in catalogmod.CATALOG_CATEGORIES:
            if catalog.available(cat):
                n = catalog.total(cat)
                if n:
                    payload["data"][CATALOG_LIB_GUIDS[cat]] = n
                    total_added += n
    for s in standalone_sources_active():
        scope = s["id"]
        if catalog.standalone_available(scope):
            pool = list(catalogmod.CATALOG_CATEGORIES)
            if features.ADULT and s.get("adult"):
                pool = pool + [(catalogmod.ADULT_CAT, "", "")]
            n = sum(catalog.total(c, scope) for c, _, _ in pool)
            if n:
                payload["data"][standalone_lib_guid(scope)] = n
                total_added += n
    if total_added:
        payload["data"]["total"] = int(payload["data"].get("total", 0)) + total_added
    return JSONResponse(payload)






_LINE_FOLDER_PREFIX = "fnmedia-line-folder:"
_LINE_ITEM_PREFIX = "fnmedia-line-item:"
_LINE_SEASON_PREFIX = "fnmedia-line-season:"


def _line_season_guid(og: str, line: str, season: int = 1) -> str:
    raw = f"{og}|{line}|{int(season or 1)}".encode("utf-8")
    return _LINE_SEASON_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_line_season_guid(guid: str) -> tuple[str, str, int] | None:
    if not isinstance(guid, str) or not guid.startswith(_LINE_SEASON_PREFIX):
        return None
    try:
        token = guid[len(_LINE_SEASON_PREFIX):]
        token += "=" * (-len(token) % 4)
        og, line, season = base64.urlsafe_b64decode(token.encode()).decode().split("|", 2)
        return og, line, int(season or 1)
    except Exception:
        return None


def _line_folder_guid(og: str) -> str:
    if isinstance(og, str) and og.startswith(_LINE_FOLDER_PREFIX):
        return og  # 幂等，避免多次包装
    return _LINE_FOLDER_PREFIX + og


def _parse_line_folder_guid(guid: str) -> str | None:
    if isinstance(guid, str) and guid.startswith(_LINE_FOLDER_PREFIX):
        return guid[len(_LINE_FOLDER_PREFIX):]
    return None


def _line_item_guid(og: str, line: str, index: int = 0) -> str:
    raw = f"{og}|{line}|{int(index or 0)}".encode("utf-8")
    return _LINE_ITEM_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_line_item_guid(guid: str) -> tuple[str, str, int] | None:
    if not isinstance(guid, str) or not guid.startswith(_LINE_ITEM_PREFIX):
        return None
    try:
        token = guid[len(_LINE_ITEM_PREFIX):]
        token += "=" * (-len(token) % 4)
        og, line, index = base64.urlsafe_b64decode(token.encode()).decode().split("|", 2)
        return og, line, int(index or 0)
    except Exception:
        return None


def _vod_directory_card(row: dict) -> dict:
    """一级影片卡改为文件夹；进入后陈列该片线路海报，不展开季集。"""
    row["type"] = "Directory"
    row["guid"] = _line_folder_guid(row["guid"])
    row["can_play"] = 0
    row["play_item_guid"] = ""
    row["play_error"] = ""
    row["ancestor_category"] = "Mix"
    row["number_of_episodes"] = 0
    row["number_of_seasons"] = 0
    row["local_number_of_episodes"] = 0
    row["local_number_of_seasons"] = 0
    row["season_number"] = 0
    row["episode_number"] = 0
    return row


def _vod_resume_index(brief: dict) -> int:
    if not inj.is_tv(brief):
        return 0
    try:
        ep_guids = store.tv_episode_guids(brief.get("guid", ""), regular_only=True)
        target_eg, _ = history.resume_episode(ep_guids)
        ep = store.get_episode(target_eg) if target_eg else None
        return int((ep or {}).get("index", 0) or 0)
    except Exception:
        return 0


def _line_episode_count(og: str, line: str) -> int:
    """固定线路的可播集数：优先按该线路直放集统计（保持各线路独立集数），
    解析线（direct=False，list_episodes 为 0）回退到全局集索引，避免显示 0。"""
    try:
        per_line = len(store.list_episodes(og, line))
        if per_line > 0:
            return per_line
    except Exception:
        pass
    try:
        return len(store.tv_episode_guids(og, regular_only=True))
    except Exception:
        return 0


def _vod_line_card(brief: dict, line: str, index: int, parent: str) -> dict:
    og = brief["guid"]
    provider = line.split("::", 1)[0]
    source = _provider_display(provider)
    title = brief.get("title", "")
    row = inj.detail_item(brief)
    item_guid = _line_item_guid(og, line, index)
    folder_guid = _line_folder_guid(og)
    is_tv = inj.is_tv(brief)
    if is_tv:
        ep_count = _line_episode_count(og, line)
        row.update({
            "type": "TV",
            "number_of_seasons": 1,
            "number_of_episodes": ep_count,
        })
    else:
        row.update({
            "type": "Movie",
            "number_of_seasons": 0,
            "number_of_episodes": 0,
            "local_number_of_episodes": 0,
            "local_number_of_seasons": 0,
        })
    row.update({
        "guid": item_guid,
        "title": f"{source}｜{title}",
        "can_play": 1,
        "play_item_guid": item_guid,
        "parent_guid": folder_guid,
        "parent_title": title,
        "ancestor_guid": folder_guid,
        "ancestor_name": title,
        "ancestor_category": row.get("type"),
        "local_number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "overview": f"来源：{source}\n点击按该线路直接播放，不自动竞速。\n\n"
                    + (brief.get("overview") or ""),
        # TV/盒子/iOS 列表模型必填（见 inject.search_item 注释）：缺 video_guid/
        # poster/path/runtime 等会整库“加载失败”或卡片不渲染。detail_item 不含这些
        # 列表字段，这里按 search_item 的合规形状补齐，保证二级线路墙在各端可渲染。
        "video_guid": ep_video_guid(episode_guid(og, "", index), line),
        "poster": row.get("posters") or inj._poster(inj._f(brief, "poster")),
        "path": "",
        "lan": "zh-CN",
        "runtime": 0,
        "single_child_guid": "",
        "file_name": "",
        "ts": 0,
        "watched": 0,
        "episode_number": row.get("episode_number") or 0,
    })
    return _apply_marks(row)


def _vod_pansou_card(brief: dict, index: int, parent: str) -> dict | None:
    """二级线路墙网盘高清卡的 lite 安全兜底：公开构建恒无网盘线，返回 None。

    完整版的真实实现（构造 PanSou 虚拟线路卡）在下方网盘私有特性段内覆盖。"""
    return None


def _fixed_media_target(mg: str, request: Request | None = None) -> dict | None:
    """线路卡/季卡 guid -> 固定线路播放目标；不进入任何自动选优。

    line-season guid 解析为该线路该季的第 0 集，使播放页对季节点请求
    stream/list 时也能拿到流容器（否则返回空、右下角集数与选集入口消失）。"""
    parsed = _parse_line_item_guid(mg)
    if not parsed:
        season_parsed = _parse_line_season_guid(mg)
        if season_parsed:
            og, line, _season_no = season_parsed
            parsed = (og, line, 0)
    if not parsed:
        return None
    og, line, index = parsed
    if request is not None:
        try:
            index = int(request.query_params.get("index", index) or index)
        except (TypeError, ValueError):
            pass
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    # PanSou 网盘高清是虚拟手动线（不在 store），构造其专用 target，不解析、不测速。
    if _is_pansou_line_name(line):
        target = _pansou_manual_target(index)
    else:
        target = store.resolve_episode(og, line, index)
        if not target:
            return None
    is_tv = inj.is_tv(brief)
    return dict(target, online_guid=og, title=brief.get("title", ""),
                ep_guid=episode_guid(og, "", index) if is_tv else "",
                kind="episode" if is_tv else "title", _fixed_line=True)


def _vod_folder_detail(guid: str, client) -> JSONResponse | None:
    """仅专用文件夹 guid 返回目录；原影片详情与播放身份保持不变。"""
    og = _parse_line_folder_guid(guid)
    if not og:
        return None
    brief = store.get_title_by_guid(og)
    if brief:
        row = inj.detail_item(brief)
    else:
        item = chart_cache.get_guid(og)
        if not item:
            return None
        row = chart_card(item)
    row["guid"] = _line_folder_guid(og)
    row = _vod_directory_card(row)
    # Directory 卡同样走列表模型，缺 video_guid/poster/path 等会导致 TV/盒子整库
    # “加载失败”、iOS 不渲染（见 inject.search_item 注释）。detail_item/chart_card
    # 不含这些列表字段，这里按 search_item 合规形状补齐。
    _poster_fallback = row.get("posters") or (
        inj._poster(inj._f(brief, "poster")) if brief else "")
    row.setdefault("video_guid", video_guid(og))
    row.setdefault("poster", _poster_fallback)
    row.setdefault("path", "")
    row.setdefault("lan", "zh-CN")
    row.setdefault("runtime", 0)
    row.setdefault("single_child_guid", "")
    row.setdefault("file_name", "")
    row.setdefault("ts", 0)
    row.setdefault("watched", 0)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(row, client)})


def _vod_line_detail(guid: str, client) -> JSONResponse | None:
    parsed = _parse_line_item_guid(guid)
    if not parsed:
        return None
    og, line, index = parsed
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    if _is_pansou_line_name(line):
        row = _vod_pansou_card(brief, index, og)
        if row is None:
            return None
    else:
        source = _provider_display(line.split("::", 1)[0])
        row = _vod_line_card(brief, line, index, og)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(row, client)})


def _vod_line_season_detail(guid: str, client) -> JSONResponse | None:
    """固定线路季卡详情：返回带真实集数的 Season 节点（点击后由 episode/list 出集）。"""
    parsed = _parse_line_season_guid(guid)
    if not parsed:
        return None
    og, line, season_no = parsed
    brief = store.get_title_by_guid(og)
    if not brief or not inj.is_tv(brief):
        return None
    rows = inj.season_list(brief)
    row = next((r for r in rows
                if int(r.get("season_number") or 1) == int(season_no or 1)), None)
    if row is None and rows:
        row = rows[0]
    if row is None:
        return None
    # 固定线路身份，保证后续 episode/list 沿同一线路取集
    row["guid"] = _line_season_guid(og, line, int(season_no or 1))
    row["parent_guid"] = _line_item_guid(og, line, 0)
    # Web 端季卡必须可播放且带真实集数，否则不渲染选集入口（见 season_list_route）。
    if client.kind == clientmod.WEB:
        row["can_play"] = 1
        if not row.get("number_of_episodes"):
            row["number_of_episodes"] = _line_episode_count(og, line)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(row, client)})


# 二级目录绑定等待策略（按用户要求：给足加载时间，宁可在加载画面内等待也不提前返回空）。
# 实测飞牛客户端可容忍 6s+ 的 item/list（安卓成功请求 P99≈6.3s）；全源搜索中位 ~2s、
# 最慢 ~3.7s。总等待窗口 8s：快源未命中即在同一请求内继续全源搜索直到命中或耗尽预算。
_FOLDER_BIND_WAIT = 8.0
_FOLDER_FAST_SOFT = 2.0
# 快源搜索的硬超时（wait_for 上限）：并发争用时快源可能 1.5s+ 才返回，给 4s 防止
# 本来能命中的搜索被短超时截断。正常情况下提前返回参数会让它在 ~0.5s 就收手。
_FOLDER_FAST_HARD = 4.0
# 详情请求内等待绑定的上限：与列表一致，二者经 _FOLDER_BIND_FUT 单飞共享同一任务。
_FOLDER_DETAIL_BIND_WAIT = 8.0
# 二级目录快源搜索的提前返回参数：最快 CMS 源 ~0.46s 即返回 20 条，达到首屏量
# （15 条）且过 0.4s 最小等待就提前收手；不满足时继续等满软超时，不会因早退而漏片。
_FOLDER_EARLY_WAIT = 0.4
_FOLDER_EARLY_RESULTS = 15


async def _vod_line_folder(parent: str, body: dict, client) -> JSONResponse:
    """二级线路海报墙：整片/整剧按线路直接播放，不展示每集。

    文件夹打开必须快速返回：已有绑定立即出线路；未绑定只给极短等待，后台单飞继续
    绑定，下一次轮询/重进即可显示完整线路，避免二级菜单卡 8 秒。
    """
    brief = store.get_title_by_guid(parent)
    if not brief:
        # 冷片（榜单 chart 或海量分类 catalog，均可能尚未落库）：进入二级目录即
        # 主动按片名快速搜索并在本次请求内短等待绑定，命中后直接列出可播线路，
        # 无需用户手动下拉刷新。单飞去重，第二次进入复用在途/已落库结果。
        brief = await ensure_folder_bound(parent)
        if not brief:
            _CHART_FAILED_TS.pop(parent, None)
            _CHART_FAIL_LEVEL.pop(parent, None)
    title = (brief or chart_cache.get_guid(parent) or
             catalog.find_brief_by_guid(parent) or {}).get("title", "")
    if not brief:
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": title or "线路", "mdb_category": "Movie", "top_dir": "",
            "dir": parent, "total": 0, "list": [], "jump_list": None}})
    index = _vod_resume_index(brief)
    lines = store.target_lines({"online_guid": parent, "index": index})
    # 同一资源站常有“解析线 + m3u8直放线”两种技术线路。二级海报墙按来源聚合，
    # 每个来源只展示一张卡，优先 m3u8/直放线；解析线仍可进入播放后的线路菜单。
    source_rows: dict[str, dict] = {}
    for t in lines:
        line = t.get("line")
        if not line:
            continue
        provider = line.split("::", 1)[0]
        old = source_rows.get(provider)
        if old is None:
            source_rows[provider] = t
            continue
        old_direct = bool(old.get("m3u8") or not old.get("resolve"))
        new_direct = bool(t.get("m3u8") or not t.get("resolve"))
        if new_direct and not old_direct:
            source_rows[provider] = t
    rows = [_vod_line_card(brief, t["line"], index, parent)
            for t in source_rows.values() if t.get("line")]
    # 网盘高清手动线作为独立来源卡追加在末尾（仅完整版且 PanSou 可用）。
    pansou_card = _vod_pansou_card(brief, index, parent)
    if pansou_card is not None:
        rows.append(pansou_card)
    rows = clientmod.normalize_cards(rows, client)
    # 电视剧线路墙仍归入 TV，保证客户端加载剧集模型与选集入口。
    mdb_category = "TV" if inj.is_tv(brief) else "Movie"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "mdb_name": f"{brief.get('title', '')} · {len(rows)}条线路",
        "mdb_category": mdb_category, "top_dir": "", "dir": parent,
        "total": len(rows), "list": rows, "jump_list": None}})


@app.post("/v/api/v1/item/list")
async def item_list(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        body = {}
    aguid = body.get("ancestor_guid")
    client = _client_of(request)

    # 子账号无权限 / 被隐藏的虚拟库：直接透传官方（官方不认识该 guid → 库不存在）
    parent = body.get("parent_guid") or aguid
    return await _item_list_impl(request, body, aguid, client, parent)


async def _item_list_impl(request: Request, body: dict, aguid, client, parent):
    # 子账号无权限 / 被隐藏的虚拟库：直接透传官方（官方不认识该 guid → 库不存在）
    if any(g and not _viewer_can_open_lib(request, g) for g in (aguid, parent)):
        return await forward_upstream(request)
    if aguid == T4_LIB_GUID or parent == T4_LIB_GUID or _t4_node(parent):
        node = _t4_node(parent)
        # 库根直进但没有任何启用的 T4 源（如唯一源被删除）：返回空列表 200，不实例化
        # 任何 T4 运行时；子节点若存在仍按其源处理。
        if node is None and not _t4_sources():
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": T4_LIB_TITLE, "mdb_category": "Mix", "top_dir": "",
                "dir": T4_LIB_TITLE, "jump_list": [], "total": 0, "list": []}})
        started = time.monotonic()
        try:
            if node and node["kind"] == "title":
                await asyncio.wait_for(_get_t4_service().ensure_detail(
                    ext_client(request), node["target_guid"]), timeout=35)
            data = await asyncio.wait_for(
                _get_t4_browser().listing(ext_client(request), parent or T4_LIB_GUID, body),
                timeout=35)
        except asyncio.TimeoutError:
            _log.warning(
                "T4 browse timeout source=%s node=%s kind=%s page=%s elapsed_ms=%d",
                (node or {}).get("source_id", ""), parent,
                (node or {}).get("kind", "root"), body.get("page", 1),
                int((time.monotonic() - started) * 1000))
            return JSONResponse({"code": -1, "msg": "T4 浏览超时，请稍后重新进入",
                                 "data": None}, status_code=504)
        except t4.T4Error as exc:
            _log.warning("T4 browse failed: %s", exc)
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=502)
        data["list"] = clientmod.normalize_cards(
            [_apply_marks(row) for row in data["list"]], client)
        return JSONResponse({"code": 0, "msg": "", "data": data})

    # —— TVBox 加固源专区库 ——

    # —— 🛡加固影视：OK影视 式目录树（源→分类→无限海报墙），只读后台缓存，即时返回 ——

    # —— 小雅分类媒体库 ——

    # —— TVBox 式二级线路海报墙：一级影片文件夹，二级固定线路 VOD 卡 ——
    folder_og = _parse_line_folder_guid(parent)
    if folder_og:
        return await _vod_line_folder(folder_og, body, client)

    # —— 单源独立媒体库：该源四类交错成一条最近更新流 ——
    sscope = standalone_source_of(aguid) if aguid else None
    if sscope is not None and \
            any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        lib_name = standalone_lib_name(src)
        if not catalog.standalone_available(sscope):
            if catalog.routes_stale() and not catalog._refreshing:
                asyncio.create_task(
                    catalog.refresh_routes(config.enabled_sources()))
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
                "dir": "", "total": 0, "list": [], "jump_list": None}})
        page = max(1, int(body.get("page") or body.get("page_index") or 1))
        page_size = max(1, min(int(body.get("page_size") or 22), 100))
        offset = int(body["offset"]) if body.get("offset") is not None \
            else (page - 1) * page_size
        briefs, total = await catalog.standalone_window(sscope, offset, page_size)
        rows = []
        for b in briefs:
            row = inj.search_item(b)
            row["title"] = (b.get("title") or "") + (
                f" ·{b['remarks']}" if b.get("remarks") else "")
            row["ancestor_guid"] = aguid
            row["ancestor_name"] = lib_name
            row["ancestor_category"] = "Mix"
            row["video_guid"] = video_guid(b["guid"])
            _apply_marks(row)
            rows.append(_vod_directory_card(row))
        rows = clientmod.normalize_cards(rows, client)
        if offset == 0:
            warm_catalog_briefs(briefs[:18])
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
            "dir": "", "total": total, "list": rows, "jump_list": None}})

    # —— 海量分类片库：按资源站分类深翻，前端无限滚动逐页请求 ——
    ccat = CATALOG_GUID_CAT.get(aguid)
    if ccat is not None:
        mdb_name = catalogmod.catalog_title(ccat)
        mdb_cat = catalogmod.catalog_mdb(ccat)
        if not catalog_enabled() or not catalog.available(ccat):
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
                "dir": "", "total": 0, "list": [], "jump_list": None}})
        # 路由表过期则后台重建（不阻塞）
        if catalog.routes_stale() and not catalog._refreshing:
            asyncio.create_task(catalog.refresh_routes(config.enabled_sources()))
        page = int(body.get("page") or body.get("p") or body.get("page_index")
                   or body.get("pn") or 1)
        page = max(1, page)
        page_size = int(body.get("page_size") or 22)
        page_size = max(1, min(page_size, 100))
        offset = int(body["offset"]) if body.get("offset") is not None \
            else (page - 1) * page_size
        total = catalog.total(ccat)
        offset = max(0, min(offset, total))
        briefs = await catalog.get_window(ccat, offset, page_size)
        rows = _catalog_filter([_catalog_card(b, ccat) for b in briefs], body)
        rows = clientmod.normalize_cards(rows, client)
        if offset == 0:
            warm_catalog_briefs(briefs[:18])
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
            "dir": "", "total": total, "list": rows,
            "jump_list": None}})

    cat = CHART_GUID_CAT.get(aguid)
    if cat is None:
        # 兼容旧的“最近更新”热门库 guid（单库/剧集库）→ 榜单剧集库
        cat = "tv" if aguid in (HOT_LEGACY_GUID, HOT_LIB_GUIDS.get("tv")) else \
            HOT_GUID_CAT.get(aguid)
    if cat is None:
        return await forward_upstream(request)
    mdb_name = chartmod.chart_cat_title(cat)
    mdb_cat = chartmod.chart_cat_mdb(cat)
    if not charts_enabled():
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
            "dir": "", "total": 0, "list": [], "jump_list": None,
        }})
    # 榜单只读、秒开；缓存过期时后台补刷（stale-while-revalidate），不阻塞本次响应。
    interval = max(1800, int(config.get().get("hot_refresh_interval", 6 * 3600)))
    try:
        if chart_cache.stale(interval) and not chart_cache._refreshing:
            asyncio.create_task(chart_cache.refresh(force=True))
    except Exception:
        pass
    all_rows = _chart_rows_all(body, cat)
    page_size, start, end = _page_args(body, len(all_rows))
    out_rows = all_rows[start:end]
    out_rows = clientmod.normalize_cards(out_rows, client)
    # 浏览热门库本页卡片时，后台提前搜源绑定（不阻塞列表）。这样用户点进某部片时
    # 多数已绑定，preset/main 不必现场等 9~25s 的榜单匹配，实现热门库也“秒播”。
    # 仅预解析首页前若干张（通常就是屏幕可见区），避免一次性压垮聚合源。
    # 浏览热门库【每一页】都后台搜源绑定（不阻塞列表），按展示顺序处理、显示一页
    # 匹配一页：用户翻到第 2/3 页点进去时也大多已绑定，preset/main 不必现场等。
    # 仅本页前若干张（≈屏幕可见区）、去重 + 冷却限流，避免翻页压垮聚合源。
    try:
        # 只后台绑定当前屏幕可见的前6张，避免一页18张并发聚合拖慢二次进入；
        # 绑定是单飞且不阻塞海报墙，二级文件夹可直接复用结果。
        _pre = [r.get("guid") for r in out_rows[:6] if r.get("guid")]
        for g in _pre:
            if chart_cache.is_chart_guid(g) and not store.get_title_by_guid(g) \
                    and g not in _chart_resolving \
                    and g not in _CHART_SLOW_RUNNING \
                    and _chart_cooldown_left(g) <= 0:
                # 浏览期预绑定走快速通道：2s 软超时、跳过全源兜底，使二级文件夹
                # 单飞复用的是“快任务”，不会被 25s 兜底占住而拖到 App 网关超时。
                start_resolve_chart(g, prefetch=True,
                                    fast_soft_timeout=_FOLDER_FAST_SOFT,
                                    fast_skip_fallback=True)
    except Exception:
        pass
    _payload = {"code": 0, "msg": "", "data": {
        "mdb_name": mdb_name,
        "mdb_category": mdb_cat,
        "top_dir": "",
        "dir": "",
        "total": len(all_rows),
        "list": out_rows,
        "jump_list": None,
    }}
    return JSONResponse(_payload)


@app.get("/v/api/v1/search/list")
async def search_list(request: Request):
    local = await forward_upstream(request)
    q = request.query_params.get("q", "").strip()
    try:
        import json
        data = json.loads(local.body)
    except Exception:
        return local
    if not isinstance(data, dict):
        return local
    items = data.get("data")
    if not isinstance(items, list):
        items = []
    online = []
    # 子账号权限：未获“在线片源”授权的账号只搜本地，不注入在线结果
    _viewer = fnusers.resolve(request)
    if q and _should_online(q) and vlibs.account_online_allowed(_viewer):
        try:
            online = await asyncio.wait_for(online_search(q, src_client(request)),
                                            timeout=6.0)
        except asyncio.TimeoutError:
            # 偶发慢源/直连抖动会撞上 6s 外窗，旧实现直接当“零结果”并造成
            # 后续播放绑定也失败。只在超时时快速重试一次；正常空结果不重试。
            _log.info("online search timeout, retry once: %s", q)
            try:
                online = await asyncio.wait_for(
                    online_search(q, src_client(request)), timeout=4.0)
            except Exception:
                online = []
        except Exception:
            online = []
    # 在线片库更齐全，默认把在线结果排到前面；可在管理页关闭
    if online:
        online = clientmod.normalize_cards(online, _client_of(request))
    if config.get().get("online_first", True):
        data["data"] = online + items
    else:
        data["data"] = items + online
    return JSONResponse(data, status_code=200,
                        headers={"Content-Type": "application/json; charset=utf-8"})


# ------------------------------------------------------------- 详情 / 播放

async def _load_t4_detail(guid: str, request: Request):
    ep = store.get_episode(guid)
    if ep:
        guid = ep["online_guid"]
    if not store.get_title_by_guid(guid):
        meta = store.get_season(guid)
        if meta:
            guid = meta["online_guid"]
    # 无启用的 T4 源时：绝不实例化 T4 服务（CMS 共存契约），直接返回 None 走常规路径
    if not _t4_sources():
        return None
    try:
        await _get_t4_service().ensure_detail(ext_client(request), guid)
    except t4.T4Error as exc:
        _log.warning("T4 detail %s: %s", guid, exc)
        return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                            status_code=502)
    return None


def _t4_media_url(request: Request, mg: str, **params) -> str:
    # Native STRM players consume this URL without the browser's origin resolution.
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if scheme not in ("http", "https"):
        scheme = request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = host or request.url.netloc
    return urllib.parse.urlunsplit((
        scheme, host, f"{ONLINE_PREFIX}/{mg}/t4",
        urllib.parse.urlencode(params), ""))


async def _t4_play_response(mg: str, target: dict, body: dict, request: Request):
    try:
        result = await _get_t4_service().resolve(ext_client(request), target)
        await _get_t4_media().identify(ext_client(request), result)
        if result.wrapper == "DASH":
            raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放，未改写为 HLS")
    except t4.T4Error as exc:
        _log.warning("T4 play %s: %s", mg, exc)
        return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                            status_code=502)
    # Headers stay server-side; unsigned public results need no extra media hop.
    if result.wrapper == "HLS":
        session = _get_t4_service().create_session(mg, target)
        link = _t4_media_url(request, mg, session=session)
    else:
        link = _t4_media_url(request, mg, line=target["line"]) \
            if result.needs_proxy else result.url
    _log.info("T4 handoff media=%s client=%s wrapper=%s delivery=%s absolute=%s",
              mg, _client_of(request).label, result.wrapper,
              "server" if result.needs_proxy else "direct",
              bool(urllib.parse.urlsplit(link).netloc))
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg, "play_link": link,
        "subtitle_guid": "", "subtitle_index": -1, "subtitle_link": "",
        "video_guid": body.get("video_guid", vg_for(mg)), "video_index": 0,
        "wrapper": result.wrapper, "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


@app.api_route(f"{ONLINE_PREFIX}/{{mg}}/t4", methods=["GET", "HEAD"])
async def t4_range(mg: str, request: Request):
    target = store.resolve_media_guid(mg)
    if not target or not target.get("t4"):
        return Response("not found", status_code=404)
    if request.query_params.get("session"):
        target = _get_t4_service().session_target(mg, request.query_params["session"])
        if target is None:
            return Response("播放会话已过期，请重新播放", status_code=410)
    try:
        target = _t4_selected_target(mg, target, request.query_params, request)
    except t4.T4Error as exc:
        return Response(str(exc), status_code=400)
    try:
        result = await _get_t4_service().resolve(ext_client(request), target)
        await _get_t4_media().identify(ext_client(request), result)
        if result.wrapper == "DASH":
            raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放")
    except t4.T4Error as exc:
        return Response(str(exc), status_code=502)
    response = await _get_t4_media().deliver(ext_client(request), result.url, result, request)
    if response.status_code >= 400:
        _get_t4_service().invalidate_playback(target)
    return response


@app.api_route("/v/media/t4-resource/{token}", methods=["GET", "HEAD"])
async def t4_resource(token: str, request: Request):
    # T4 媒体运行时尚未创建（未启用/未播放过）：任何资源 token 都视为已过期，不实例化
    if t4_media is None:
        return Response("T4 session expired", status_code=410)
    return await t4_media.resource_response(ext_client(request), token, request)


@app.get("/v/api/v1/item/{guid}")
async def item_detail(guid: str, request: Request):
    if _parse_line_item_guid(guid):
        fixed_detail = _vod_line_detail(guid, _client_of(request))
        if fixed_detail is not None:
            return fixed_detail
    if _parse_line_season_guid(guid):
        season_detail = _vod_line_season_detail(guid, _client_of(request))
        if season_detail is not None:
            return season_detail
    if _parse_line_folder_guid(guid):
        # 详情请求与安卓的 item/list 几乎并发（间隔仅 ~0.3s）。这里只【非阻塞触发】
        # 绑定任务（经 _FOLDER_BIND_FUT 单飞），绝不 await——否则详情响应被搜索拖慢
        # 0.9~5s，而安卓要等详情返回才渲染页面，用户会先看到整页“无数据”。
        # 详情秒回渲染页面骨架，真正的线路数据由并发的 item/list 在等待窗口内等出。
        _folder_og = _parse_line_folder_guid(guid)
        if _folder_og and not store.get_title_by_guid(_folder_og):
            prefetch_folder_binding(_folder_og)
        folder_detail = _vod_folder_detail(guid, _client_of(request))
        if folder_detail is not None:
            return folder_detail
    if guid == T4_LIB_GUID or _t4_node(guid):
        if not _viewer_can_open_lib(request, guid):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        # 直进 T4 库 guid 却无启用的 T4 源：库不存在，绝不实例化 T4 运行时（共存契约）
        if guid == T4_LIB_GUID and not _t4_sources():
            return JSONResponse({"code": -6, "msg": "T4 library unavailable",
                                 "data": None})
        if guid == T4_LIB_GUID:
            data = inj._base_item(guid=guid, type="MediaDB", title=T4_LIB_TITLE,
                                  ancestor_guid="", ancestor_name="", ancestor_category="",
                                  can_play=0, play_item_guid="", play_error="", logic_type=0)
        else:
            node = _t4_node(guid)
            try:
                _get_t4_browser().source(node["source_id"])
            except t4.T4Error as exc:
                return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                    status_code=502)
            data = _get_t4_browser().card(guid)
        return JSONResponse({"code": 0, "msg": "", "data":
                             clientmod.normalize_detail(data, _client_of(request))})
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    client = _client_of(request)
    # 单源独立媒体库根节点
    sscope = standalone_source_of(guid)
    if sscope is not None and \
            any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        _pool = list(catalogmod.CATALOG_CATEGORIES)
        if features.ADULT and src.get("adult"):
            _pool = _pool + [(catalogmod.ADULT_CAT, "", "")]
        n = sum(catalog.total(c, sscope) for c, _, _ in _pool)
        data = inj._base_item(
            guid=guid, type="MediaDB", title=standalone_lib_name(src),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # TVBox 加固源专区库根节点
    # 小雅分类库：库根节点 + 影片详情
    # 海量库根节点
    if guid in CATALOG_GUID_CAT:
        ccat = CATALOG_GUID_CAT[guid]
        n = catalog.total(ccat)
        data = inj._base_item(
            guid=guid, type="MediaDB", title=catalogmod.catalog_title(ccat),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # 榜单虚拟媒体库根节点（进入媒体库时前端会请求 item/<库guid>）。
    # 必须在透传前拦截，否则官方后端返回 -6，页面提示「此媒体库不存在」。
    if guid in CHART_GUID_CAT or is_hot_guid(guid):
        cat = CHART_GUID_CAT.get(guid) or \
            HOT_GUID_CAT.get(guid) or ("tv" if guid == HOT_LEGACY_GUID else "movie")
        n = len(chart_cache.items(cat))
        data = inj._base_item(
            guid=guid, type="MediaDB", title=chartmod.chart_cat_title(cat),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # 榜单条目：按需用片名搜源解析；成功后复用标准详情，失败返回“暂无片源”。
    if chart_cache.is_chart_guid(guid):
        meta = chart_cache.get_guid(guid) or {}
        bound = store.get_title_by_guid(guid)
        # 只有“快源+全源均无源(dead)”才在冷却内跳过；快源未命中/匹配中仍尝试，
        # 保证热门片进详情页时后台尽快完成绑定。
        if bound is None and _chart_cooldown_left(guid) <= 0:
            # 详情接口也不能把用户卡在海报墙：后台搜源 + 极短窗口，超时先返回
            # 可播骨架（play/info/stream/preset 会在播放页内继续等待同一任务）。
            fut = start_resolve_chart(guid)
            _PENDING_MEDIA_CHART[media_guid(guid)] = guid
            if fut is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=0.12)
                except asyncio.TimeoutError:
                    pass
            bound = store.get_title_by_guid(guid)
        if bound is not None:
            data = inj.detail_item(bound)
            _apply_marks(data)
            # 用榜单的优质海报/评分/简介覆盖资源站元数据
            if meta.get("cover"):
                for k in ("backdrops", "posters"):
                    if data.get(k):
                        data[k] = inj._poster(meta["cover"])
            if meta.get("rating"):
                data["vote_average"] = str(meta["rating"])
            if meta.get("desc") and not data.get("overview"):
                data["overview"] = meta["desc"]
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(data, client)})
        cat = meta.get("cat", "tv")
        # 只有“快源+全源均无源(dead)”才对详情标记失败；快源未命中仍可进播放页占位。
        failed = _CHART_FAIL_LEVEL.get(guid) == "dead" \
            and _chart_cooldown_left(guid) > 0
        data = inj._base_item(
            guid=guid,
            type="Movie" if cat == "movie" else "TV",
            title=meta.get("title", ""), can_play=0 if failed else 1,
            ancestor_guid=CHART_LIB_GUIDS.get(cat, ""),
            ancestor_name=chartmod.chart_cat_title(cat),
            ancestor_category=chartmod.chart_cat_mdb(cat),
            poster=meta.get("cover", ""), logic_type=0)
        # 匹配中：标记可播，让前端能立刻进入播放页；真正失败在播放页内给出提示。
        data["play_item_guid"] = guid if not failed else ""
        if failed:
            data["play_error"] = "暂无在线片源"
            data["overview"] = (meta.get("desc") or
                                "暂未在内置资源站找到可播片源，可过段时间再试，或用顶部搜索同名资源。")
        else:
            data["play_error"] = ""
            data["overview"] = meta.get("desc") or data.get("overview", "")
        data["vote_average"] = str(meta.get("rating") or "0")
        data["release_date"] = meta.get("year") or ""
        data["air_date"] = meta.get("year") or ""
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    # 伪造的 Season / Episode 节点
    if store.get_episode(guid):
        ep = store.get_episode(guid)
        data = inj.episode_item(ep)
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    season = store.get_season(guid)
    if season:
        tv = season["online_guid"]
        brief = store.get_title_by_guid(tv)
        data = inj.season_item(brief, dict(season, guid=guid))
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    brief = store.get_title_by_guid(guid)
    if not brief:
        return await forward_upstream(request)
    data = inj.detail_item(brief)
    _apply_marks(data)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_detail(data, client)})


@app.get("/v/api/v1/season/list/{guid}")
async def season_list_route(guid: str, request: Request):
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    parsed_line = _parse_line_item_guid(guid) or _parse_line_season_guid(guid)
    fixed_line = parsed_line[1] if parsed_line else None
    fixed_season = parsed_line[2] if parsed_line else 1
    if parsed_line:
        guid = parsed_line[0]
    brief = store.get_title_by_guid(guid)
    if not brief or not inj.is_tv(brief):
        return await forward_upstream(request)
    client = _client_of(request)
    rows = inj.season_list(brief)
    if fixed_line:
        for row in rows:
            try:
                sn = int(row.get("season_number") or fixed_season or 1)
            except (TypeError, ValueError):
                sn = 1
            row["guid"] = _line_season_guid(guid, fixed_line, sn)
            row["parent_guid"] = _line_item_guid(guid, fixed_line, 0)
    # 飞牛 Web 对 can_play=null / 集数 0 的“合成包装季”不渲染选集入口（拉完
    # season/list 后不再请求 episode/list），表现为剧集不显示、无法切集；而 App/TV
    # 依赖原生 CMS 包装形状（null/0，由其自行统计），给真实集数反而隐藏集列表。
    # 故仅对 Web 端把季卡补成可播放 + 真实集数，纯展示层差异，不改任何播放/选线策略。
    if client.kind == clientmod.WEB:
        for row in rows:
            row["can_play"] = 1
            if not row.get("number_of_episodes"):
                if fixed_line:
                    row["number_of_episodes"] = _line_episode_count(guid, fixed_line)
                else:
                    try:
                        row["number_of_episodes"] = len(
                            store.tv_episode_guids(guid, regular_only=True))
                    except Exception:
                        pass
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(rows, client)})


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_list_route(guid: str, request: Request):
    # guid 可能是伪造的 season guid、固定线路卡/线路季 guid，也可能是 tv guid
    parsed_line = _parse_line_item_guid(guid) or _parse_line_season_guid(guid)
    fixed_line = parsed_line[1] if parsed_line else None
    fixed_season = parsed_line[2] if parsed_line else None
    if parsed_line:
        guid = parsed_line[0]
    season = store.get_season(guid)
    tv = season["online_guid"] if season else None
    if not tv:
        brief = store.get_title_by_guid(guid)
        if brief and inj.is_tv(brief):
            tv = guid
    if not tv:
        return await forward_upstream(request)
    error = await _load_t4_detail(tv, request)
    if error is not None:
        return error
    client = _client_of(request)
    season_number = season["season"] if season else fixed_season
    rows = inj.episode_list(store.get_title_by_guid(tv), season_number)
    if fixed_line:
        line_season = _line_season_guid(tv, fixed_line, season_number or 1)
        for row in rows:
            ep_g = row.get("guid")
            if ep_g:
                try:
                    ep_index = int(row.get("episode_number") or 1) - 1
                except (TypeError, ValueError):
                    ep_index = 0
                row["guid"] = _line_item_guid(tv, fixed_line, ep_index)
                row["media_guid"] = ep_media_guid(ep_g, fixed_line)
                row["video_guid"] = ep_video_guid(ep_g, fixed_line)
                row["parent_guid"] = line_season
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(rows, client)})


@app.get("/v/api/v1/stream/list/{guid}")
async def stream_list(guid: str, request: Request):
    return await _stream_list_impl(guid, request)


@app.post("/v/api/v1/stream/list")
async def stream_list_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    guid = str(body.get("guid") or body.get("item_guid") or "")
    if not guid:
        return await forward_upstream(request)
    return await _stream_list_impl(guid, request)


async def _stream_list_impl(guid: str, request: Request):
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    # TVBox 固定线路卡：按 guid 中携带的影片/线路/集序号返回流容器。
    fixed = _fixed_media_target(guid, request)
    if fixed is not None:
        brief = store.get_title_by_guid(fixed["online_guid"]) or {}
        ep_guids = store.tv_episode_guids(fixed["online_guid"], regular_only=True) \
            if inj.is_tv(brief) else []
        ep = store.get_episode(ep_guids[fixed["index"]]) if ep_guids and fixed["index"] < len(ep_guids) else None
        if ep is not None:
            sl = inj.episode_stream_list(ep)
        else:
            sl = inj.stream_list(brief)
        target = fixed
    else:
        ep = store.get_episode(guid)
        target = None
    if fixed is not None:
        pass
    elif ep:
        sl = inj.episode_stream_list(ep)
        target = store.get_episode_target(guid)
    else:
        brief = store.get_title_by_guid(guid)
        if not brief and chart_cache.is_chart_guid(guid):
            # 仅“快源+全源均无源(dead)”冷却内才透传官方后端；匹配中/快源未命中
            # 一律返回占位骨架，保证能进播放页并随后台绑定自动恢复。
            if _CHART_FAIL_LEVEL.get(guid) == "dead" \
                    and _chart_cooldown_left(guid) > 0:
                return await forward_upstream(request)
            # 打开详情/线路菜单也不能同步等搜源：启动后台匹配并立即返回占位线路，
            # 真正播放时 play/info/preset 会等待同一单飞任务。
            start_resolve_chart(guid)
            _PENDING_MEDIA_CHART[media_guid(guid)] = guid
            return JSONResponse({"code": 0, "msg": "",
                                 "data": _instant_stream_payload(media_guid(guid))})
        if not brief:
            return await forward_upstream(request)
        sl = inj.stream_list(brief)
        if inj.is_tv(brief):
            # 标题播放键实际播放智能续播集，线路菜单也以该集为准
            egs = store.tv_episode_guids(guid, regular_only=True)
            if egs:
                resume_guid, _ = history.resume_episode(egs)
                target = store.get_episode_target(resume_guid)
        else:
            target = store.resolve_media_guid(media_guid(guid))
    quals = _qualities(target) if target else []
    if target and target.get("t4"):
        # Menus never start a media probe. Reuse an exact episode/line result.
        playback = _get_t4_service().cached_playback(target)
        metadata = _get_t4_media().metadata.cached(playback) if playback else None
        t4_metadata.apply(sl, playback.wrapper if playback else "", metadata)
    if quals:
        sl["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
        sl["video_streams"][0]["bps"] = quals[0]["bitrate"]
        sl["qualities"] = quals
        sl["cloud_storage_info"] = None
    # 打开详情/线路菜单即后台预热默认线路播放列表（单飞，真正取 main 时并入）。
    # PanSou 是虚拟手动线，只在用户真正播放时才搜索转存，菜单阶段绝不预热。
    if target and not target.get("_pansou_manual") \
            and not _is_pansou_line_name(target.get("line", "")):
        try:
            _mg = (sl.get("video_streams") or [{}])[0].get("media_guid")
            if _mg:
                _warm_playlist(_mg, target, request)
        except Exception:
            pass
    return JSONResponse({"code": 0, "msg": "", "data": sl})


# ------------------------------------------------------- 收藏 / 已看（本地接管）

def _online_card_meta(guid: str) -> dict | None:
    """把一个在线 guid 解析成可用于收藏夹展示的元信息（不做网络请求）。"""
    brief = store.get_title_by_guid(guid)
    if brief:
        return {
            "title": inj._f(brief, "title"),
            "type": inj.fnos_type(brief),
            "poster": inj._f(brief, "poster"),
            "year": inj._f(brief, "year"),
            "remarks": inj._f(brief, "remarks"),
        }
    ep = store.get_episode(guid)
    if ep:
        return {"title": ep.get("ep_title") or "episode", "type": "Episode",
                "poster": "", "year": ""}
    meta = chart_cache.get_guid(guid) or {}
    if meta:
        return {"title": meta.get("title", ""),
                "type": "Movie" if meta.get("cat") == "movie" else "TV",
                "poster": meta.get("cover", ""), "year": meta.get("year", ""),
                "remarks": ""}
    return None


def _normalize_history_guid(guid: str) -> str:
    """把带线路身份的复合 guid 归一化为基础影片/集 guid，保证历史、收藏、已看
    都按原片身份记录，不按资源站拆分；非复合 guid 原样返回。"""
    if not isinstance(guid, str):
        return guid or ""
    parsed = _parse_line_item_guid(guid)
    if parsed:
        og, _line, index = parsed
        brief = store.get_title_by_guid(og)
        if brief and inj.is_tv(brief):
            return episode_guid(og, "", index)
        return og
    season = _parse_line_season_guid(guid)
    if season:
        return season_guid(season[0], season[2])
    return guid


def _is_online_item_guid(guid: str) -> bool:
    if not guid:
        return False
    if _parse_line_item_guid(guid) or _parse_line_season_guid(guid):
        return True
    if (guid in CATALOG_GUID_CAT or guid in CHART_GUID_CAT or is_hot_guid(guid)
            or standalone_source_of(guid) is not None):
        return False  # 媒体库根节点不可收藏
    if store.get_title_by_guid(guid) or store.get_episode(guid):
        return True
    if chart_cache.is_chart_guid(guid):
        return True
    return False


def _apply_marks(row: dict) -> dict:
    g = row.get("guid", "")
    if marks.is_favorite(g):
        row["is_favorite"] = 1
    if marks.is_watched(g):
        row["watched"] = 1
        row["is_watched"] = 1
        ts = marks.watched_ts(g)
        if ts:
            row["watched_ts"] = int(ts)
    return row


def _mark_extra(guid: str) -> dict:
    meta = _online_card_meta(guid) or {}
    return {
        "title": meta.get("title", ""), "type": meta.get("type", ""),
        "poster": meta.get("poster", ""), "year": meta.get("year", ""),
        "remarks": meta.get("remarks", ""),
    }


@app.put("/v/api/v1/item/favorite")
async def item_favorite_add(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    base = _normalize_history_guid(guid)
    marks.add_favorite(base, _mark_extra(base))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": base}})


@app.delete("/v/api/v1/item/favorite")
async def item_favorite_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_favorite(_normalize_history_guid(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.post("/v/api/v1/item/watched")
async def item_watched_set(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    # 允许显式取消（watched=0/is_watched=0）
    want = body.get("watched", body.get("is_watched", 1))
    try:
        want = int(want)
    except (TypeError, ValueError):
        want = 1
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    base = _normalize_history_guid(guid)
    if want == 0:
        marks.remove_watched(base)
    else:
        marks.set_watched(base, _mark_extra(base))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": base}})


@app.delete("/v/api/v1/item/watched")
async def item_watched_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_watched(_normalize_history_guid(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


def _favorite_card(rec: dict) -> dict | None:
    guid = rec.get("guid", "")
    brief = store.get_title_by_guid(guid)
    if brief:
        row = inj.search_item(brief)
        row["title"] = inj._f(brief, "title") + (
            f" ·{inj._f(brief, 'remarks')}" if inj._f(brief, "remarks") else "")
    elif chart_cache.is_chart_guid(guid):
        meta = chart_cache.get_guid(guid) or {}
        if not meta:
            return None
        row = chart_card(meta)
    else:
        return None  # 剧集子节点 / 已失效条目暂不在收藏夹展示
    _apply_marks(row)
    return row


@app.post("/v/api/v1/favorite/list")
async def favorite_list(request: Request):
    """收藏夹：合并官方本地收藏与本插件在线收藏，统一分页。"""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    local = await forward_upstream(request)
    try:
        ldata = json.loads(local.body)
    except Exception:
        return local
    if not isinstance(ldata, dict):
        return local
    ldata.setdefault("data", {})
    if not isinstance(ldata["data"], dict):
        return local

    ftype = str(body.get("type") or "all")
    cards: list[dict] = []
    for rec in marks.favorite_records():
        row = _favorite_card(rec)
        if not row:
            continue
        if ftype in ("movie", "tv", "video", "other", "live"):
            t = (row.get("type") or "").lower()
            ok = (ftype == "movie" and t == "movie") or \
                 (ftype == "tv" and t in ("tv", "mix")) or \
                 (ftype in ("video", "other") and t not in ("movie", "tv", "mix"))
            if not ok:
                continue
        cards.append(row)

    official = (ldata["data"].get("list") or [])
    cards = clientmod.normalize_cards(cards, _client_of(request))
    merged = cards + list(official)
    page = max(1, int(body.get("page") or 1))
    page_size = max(1, int(body.get("page_size") or len(merged) or 50))
    start = (page - 1) * page_size
    ldata["data"]["total"] = len(merged)
    ldata["data"]["list"] = merged[start:start + page_size]
    ldata["code"] = 0
    ldata["msg"] = ""
    return JSONResponse(ldata)


def _online_play_info(guid: str, request: Request) -> dict | None:
    """对已绑定的在线标题（brief）构造原生播放 info。

    返回 None 表示该 guid 不属于在线标题（调用方应继续走榜单/上游分支）。
    与资源浏览页 open 深链共用，保证 /video 用的 guid 与播放器一致。
    """
    # 与 play/info 路由一致：先看该 guid 是否已是某个（动态解析出的）可播集
    ep = store.get_episode(guid)
    if ep:
        info = inj.episode_play_info(ep)
        rec0 = history.get(guid)
        if rec0:
            info["ts"] = rec0.get("ts", 0)
        return info
    brief = store.get_title_by_guid(guid)
    if not brief:
        return None
    rec = history.get(guid)
    if inj.is_tv(brief):
        ep_guids = store.tv_episode_guids(guid, regular_only=True)
        if ep_guids:
            # T4 远程剧集采用“回看最近交互”语义；普通 CMS 保持按集序续播
            t4_rewatch = brief.get("locator", {}).get("kind") == "t4"
            target_eg, resume_ts = history.resume_episode(
                ep_guids, latest_interaction=t4_rewatch)
            if target_eg:
                ep = store.get_episode(target_eg)
                if ep:
                    info = inj.episode_play_info(ep)
                    info["ts"] = resume_ts
                    return info
    info = inj.play_info(brief)
    if rec:
        info["ts"] = rec.get("ts", 0)
    return info


def _vod_play_info(guid: str, client) -> JSONResponse | None:
    parsed = _parse_line_item_guid(guid)
    if not parsed:
        return None
    og, line, index = parsed
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    is_pansou = _is_pansou_line_name(line)
    if is_pansou:
        # PanSou 虚拟手动线不在 store，用其专用 target，不做普通集解析。
        target = _pansou_manual_target(index)
    else:
        target = store.resolve_episode(og, line, index)
        if not target:
            return None
    is_tv = inj.is_tv(brief)
    eg = episode_guid(og, "", index)
    base_guid = eg if is_tv else og
    if is_tv:
        ep = store.get_episode(eg)
        if not ep:
            return None
        # 飞牛 Web 只在 play/info 为 Episode 形状（type=Episode、parent 指向季、
        # 带 season/episode 序号）时渲染「选集」入口；TV/detail 形状会被当成独立影片而
        # 不显示选集。故 Web 多集内容复用 episode_play_info 的原生形状（与播放历史入口
        # 完全一致，该入口实测有选集），仅把 media/video guid 换成带线路版本锁定本线路。
        # App/TV 仍走下方 TV 模型，保持其原生选集/右下角集数契约不变。
        if client.kind == clientmod.WEB and not is_pansou:
            info = inj.episode_play_info(ep)
            info["media_guid"] = ep_media_guid(eg, line)
            info["video_guid"] = ep_video_guid(eg, line)
            # Episode item 默认用基础 ep guid（不带线路），Web 点选集时会透传官方后端
            # 而失败；改成带固定线路身份的 line-item/line-season guid 与其带线路 media
            # guid，使选集链全程锁定本线路（复用已验证的 episode_list_route 固定线路链）。
            _witem = info.get("item") or {}
            _witem["guid"] = _line_item_guid(og, line, index)
            _witem["parent_guid"] = _line_season_guid(og, line, int(ep.get("season") or 1))
            _witem["media_guid"] = ep_media_guid(eg, line)
            _witem["video_guid"] = ep_video_guid(eg, line)
            _rec = history.get(eg) or history.get(og)
            if _rec:
                info["ts"] = _rec.get("ts", 0)
            return JSONResponse({"code": 0, "msg": "", "data":
                                 clientmod.normalize_detail(info, client)})
        info = inj.episode_play_info(ep)
    else:
        info = inj.play_info(brief)
    info["guid"] = base_guid
    # TVBox/VOD：点击即播当前续播集，但保留TV模型以显示选集入口。
    info["type"] = "TV" if is_tv else "Movie"
    info["media_guid"] = ep_media_guid(eg, line)
    info["video_guid"] = ep_video_guid(eg, line)
    item = inj.detail_item(brief)
    if is_pansou:
        ep_count = len(store.tv_episode_guids(og, regular_only=True)) if is_tv else 0
        source_label = PANSOU_LABEL
    else:
        # 按具体线路统计集数，解析线为 0 时回退全局集索引（见 _line_episode_count）
        ep_count = _line_episode_count(og, line) if is_tv else 0
        source_label = _provider_display(line.split('::', 1)[0])
    item.update({
        "guid": base_guid,
        "type": "TV" if is_tv else "Movie",
        "can_play": 1,
        "play_item_guid": base_guid,
        "parent_guid": og,
        "parent_title": brief.get("title", ""),
        "number_of_episodes": ep_count,
        "number_of_seasons": 1 if is_tv else 0,
        "local_number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "overview": f"来源：{source_label}\n"
                    f"指定线路直接播放，不自动竞速。\n\n{brief.get('overview') or ''}",
    })
    info["item"] = item
    rec = history.get(base_guid)
    if rec:
        info["ts"] = rec.get("ts", 0)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(info, client)})


@app.post("/v/api/v1/play/info")
async def play_info(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    guid = body.get("item_guid", "")
    client = _client_of(request)
    if _parse_line_item_guid(guid):
        fixed_info = _vod_play_info(guid, client)
        if fixed_info is not None:
            return fixed_info
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error


    def _prewarm_from_info(info: dict):
        """v0.2.42 按需模式：详情页不触发后台预热。"""
        return None

    ep = store.get_episode(guid)
    rec = history.get(guid)
    if ep:
        info = inj.episode_play_info(ep)
        if rec:
            info["ts"] = rec.get("ts", 0)
        _prewarm_from_info(info)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(info, client)})
    brief = store.get_title_by_guid(guid)
    if brief and chart_cache.is_chart_guid(guid) \
            and not _chart_binding_matches(guid, brief):
        _drop_chart_binding(guid)
        brief = None
    if not brief and chart_cache.is_chart_guid(guid):
        # 网页端必须先拿到 play/info 才会跳转播放页。这里不能长时间等待全网搜源，
        # 否则用户会停在海报墙；先触发解析并给极短窗口，超时立即返回可播骨架，
        # 后续进入播放页后的 stream/play 接口再等待同一个单飞任务完成。
        fut = start_resolve_chart(guid)
        if fut is not None:
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=0.12)
            except asyncio.TimeoutError:
                pass
        brief = store.get_title_by_guid(guid)
        if brief is None:
            meta = chart_cache.get_guid(guid) or {}
            cat = meta.get("cat", "tv")
            typ = "Movie" if cat == "movie" else "TV"
            _PENDING_MEDIA_CHART[media_guid(guid)] = guid
            if len(_PENDING_MEDIA_CHART) > 1000:
                for _k in list(_PENDING_MEDIA_CHART)[:500]:
                    _PENDING_MEDIA_CHART.pop(_k, None)
            info = {
                "grand_guid": "", "guid": guid, "parent_guid": "",
                "play_config": None, "ts": 0, "type": typ,
                "video_guid": video_guid(guid),
                "audio_guid": "", "subtitle_guid": "",
                "media_guid": media_guid(guid),
                "item": {
                    "guid": guid, "type": typ, "title": meta.get("title", ""),
                    "can_play": 1, "play_error": "",
                    "season_number": 0 if typ == "Movie" else 1,
                    "episode_number": 0, "parent_guid": "",
                    "posters": inj._poster(meta.get("cover", "")) if meta.get("cover") else "",
                    "backdrops": inj._poster(meta.get("cover", "")) if meta.get("cover") else "",
                    "number_of_episodes": 1, "number_of_seasons": 1 if typ == "TV" else 0,
                },
                "direct_link_audio_index": -1,
            }
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(info, client)})
    if not brief:
        return await forward_upstream(request)
    info = _online_play_info(guid, request)
    if info is None:
        return await forward_upstream(request)
    _prewarm_from_info(info)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_detail(info, client)})


def _line_choices(target: dict, *, include_pansou: bool = True) -> list[dict]:
    """该播放目标可切换的线路：按资源站去重（同一站直链优先于 share 页），
    当前播放线路排第一；T4 保持稳定菜单顺序，防止切线后序号漂移。

    include_pansou=True 时在末尾全局追加一条“网盘高清（PanSou）”手动线（完整版且
    可用时）。该线不在 store、不进搜索、不参与测速，仅供用户手动切到后触发转存。"""
    raw = store.target_lines(target)
    if target.get("t4"):
        # Stable menu indices even when a session has selected another line.
        return [{**choice, "label": choice["line"]} for choice in raw]
    cur = target.get("line")
    # 当前线路置顶，其余直链在前、可解析页在后
    raw.sort(key=lambda t: (t["line"] != cur, not t["m3u8"]
                            and not re.search(r"\.mp4(\?|$)", t["url"], re.I),
                            t.get("resolve", False)))
    out, seen_provider, seen_name = [], set(), set()
    for t in raw:
        if t.get("line") == PANSOU_LINE:
            continue
        provider = t["line"].split("::", 1)[0]
        name = _provider_display(provider)
        if provider in seen_provider:
            continue
        # 极端情况下不同 provider 映射到同名，加序号区分
        if name in seen_name:
            k = 2
            while f"{name}{k}" in seen_name:
                k += 1
            name = f"{name}{k}"
        seen_provider.add(provider)
        seen_name.add(name)
        out.append({**t, "label": name})
    # 全局追加网盘高清手动线（始终最后，且永不作为默认/竞速对象）
    if include_pansou and _pansou_available():
        try:
            _idx = int(target.get("index", 0) or 0)
        except Exception:
            _idx = 0
        out.append({**_pansou_manual_target(_idx), "label": PANSOU_LABEL})
    # 保持当前线路第一
    out.sort(key=lambda t: t["line"] != cur)
    return out


# 线路码率编码：用 9000 万段的稳定整数，避免与真实码率（通常<3000万）冲突。
# 旧方案按线路在菜单里的【位置序号】编码，每部片线路数不同→同一 bitrate 跨片映射
# 到不同线，客户端记住 bitrate 后在新片高亮/选错。改为按【线路名稳定哈希】：同一条
# 物理线路（如 baofeng::bfzym3u8）在任何片里 bitrate 恒定，跨片记忆不再错位。
_LINE_BR_BASE = 90_000_000
_LINE_BR_STEP = 10_001          # 旧位置编码步长（兼容解码用）
_LINE_BR_HASH_SPAN = 800_000    # 哈希段宽度，避开旧段且 < 9100 万


def _line_hash_code(line: str) -> int:
    """线路名 → 稳定哈希码（同线恒定，跨片一致）。"""
    return int(hashlib.md5(str(line).encode("utf-8")).hexdigest()[:8], 16) % _LINE_BR_HASH_SPAN


def _line_bitrate(line: str, lines: list[str] | None = None) -> int:
    # 按线路名稳定哈希编码：同一条物理线路在任何片里 bitrate 恒定，跨片记忆不错位。
    # lines 参数保留以兼容旧调用签名（新编码不再依赖位置）。
    try:
        return _LINE_BR_BASE + 100_020 + _line_hash_code(line)
    except Exception:
        return _LINE_BR_BASE


def _line_of_bitrate(br, lines: list[str]) -> str | None:
    try:
        b = int(br or 0)
    except (TypeError, ValueError):
        return None
    if not (_LINE_BR_BASE <= b < 91_000_000):
        return None
    # 1) 新哈希编码：按线路名匹配（跨片稳定）
    if b >= _LINE_BR_BASE + 100_020:
        h = b - (_LINE_BR_BASE + 100_020)
        for ln in lines:
            if _line_hash_code(ln) == h:
                return ln
        return None  # 该片无此线路，交由上层回落默认，绝不瞎选
    # 2) 旧位置编码兼容（老客户端/升级前记住的 bitrate）
    idx = (b - _LINE_BR_BASE) // _LINE_BR_STEP
    if 0 <= idx < len(lines):
        return lines[idx]
    return None


def _qualities(target: dict) -> list[dict]:
    """把每条播放线路表达成播放器「清晰度」菜单里的一个可选项。"""
    lines = _line_choices(target)
    # 全局追加网盘线后，只要总选项≥2 就给菜单（含“只有一条 CMS + 网盘”的情况）。
    if len(lines) < 2:
        return []
    out = []
    for t in lines:
        out.append({
            "resolution": t.get("label") or _friendly_line(t["line"]),
            "bitrate": _line_bitrate(t["line"]),
            "progressive": True,
            "is_m3u8": False,  # 置 False 才不会被菜单项置灰
        })
    return out


def _t4_selected_target(mg: str, target: dict, selection, request: Request) -> dict:
    """Use the same episode/line identity for stream, play and native Range."""
    choices = _line_choices(target)
    names = [choice["line"] for choice in choices]
    line = selection.get("line")
    index = selection.get("direct_link_quality_index")
    if not line and index is not None:
        try:
            number = int(index)
        except (TypeError, ValueError):
            raise t4.T4Error("T4 线路序号无效，请重新选择线路") from None
        if not 0 <= number < len(names):
            raise t4.T4Error("T4 线路序号已失效，请重新打开详情")
        line = names[number]
    if not line:
        line = _line_of_bitrate(selection.get("bitrate"), names)
    if not line:
        link = selection.get("playlink") or request.headers.get("Play-Link", "")
        try:
            parsed = urllib.parse.urlsplit(link)
        except ValueError:
            raise t4.T4Error("播放地址格式无效，请重新播放") from None
        # Read only our own media selector, never fetch a client-supplied URL.
        if parsed.path == f"{ONLINE_PREFIX}/{mg}/t4":
            params = urllib.parse.parse_qs(parsed.query)
            line = params.get("line", [None])[0]
            session = params.get("session", [""])[0]
            if not line and session:
                target = _get_t4_service().session_target(mg, session)
                if target is None:
                    raise t4.T4Error("播放会话已过期，请重新播放")
        elif parsed.path.startswith(f"{ONLINE_PREFIX}/"):
            raise t4.T4Error("播放地址与当前分集不一致，请重新选择分集")
        elif link:
            line = _get_t4_service().cached_line_for_url(target, link)
    if not line:
        return target
    if line not in names:
        raise t4.T4Error("T4 线路已失效或缺少该分集，请重新打开详情")
    return next(choice for choice in choices if choice["line"] == line)


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    target = _fixed_media_target(mg, request) or store.resolve_media_guid(mg)
    if not target and chart_guid_by_media(mg):
        # 网页播放器会在进入播放页前连续请求 stream；这里必须瞬时返回，不能等待搜源，
        # 否则三个请求串起来仍会把用户留在海报墙。真实匹配推迟到播放器请求 preset。
        start_resolve_chart(chart_guid_by_media(mg))
        return _instant_stream_response(mg)
    if not target:
        return await forward_upstream(request)
    if target.get("t4"):
        started = time.monotonic()
        _log.info("T4 stream start media=%s client=%s",
                  mg, _client_of(request).label)
        menu_target = target
        choices = _line_choices(target)
        try:
            target = _t4_selected_target(mg, target, body, request)
        except t4.T4Error as exc:
            _log.warning("T4 stream failed media=%s client=%s stage=selection",
                         mg, _client_of(request).label)
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=400)
        try:
            playback = await _get_t4_service().resolve(ext_client(request), target)
            await _get_t4_media().identify(ext_client(request), playback)
            if playback.wrapper == "DASH":
                raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放")
        except t4.T4Error as exc:
            _log.warning("T4 stream failed media=%s client=%s stage=resolve elapsed_ms=%d",
                         mg, _client_of(request).label,
                         int((time.monotonic() - started) * 1000))
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=502)
        if target.get("kind") == "episode":
            sl = inj.episode_stream_list(target)
        else:
            sl = inj.stream_list(store.get_title_by_guid(target["online_guid"]))
        metadata = await _get_t4_media().metadata.for_playback(
            ext_client(request), playback,
            wait=max(0.0, T4_STREAM_READY_BUDGET - (time.monotonic() - started)))
        t4_metadata.apply(sl, playback.wrapper, metadata)
        f, v = sl["files"][0], sl["video_streams"][0]
        if not f["size"]:
            f["size"] = playback.size
        qualities = _qualities(menu_target)
        names = [choice["line"] for choice in choices]
        if len(choices) > 1:
            # These codes select sources in the official quality menu, not encoders.
            v.update(resolution_type=target.get("label") or target["line"],
                     bps=_line_bitrate(target["line"], names))
        else:
            # Native players also require a ready original-quality entry for one source.
            qualities = [{
                "resolution": v["resolution_type"] or "原画",
                "bitrate": v["bps"],
                "progressive": True,
                "is_m3u8": False,
            }]
        data = {"file_stream": f, "video_stream": v, "audio_streams": sl["audio_streams"],
                "subtitle_streams": [], "qualities": qualities, "cloud_storage_info": None}
        if playback.wrapper != "HLS":
            # trim.media 0.9.8 uses 9001 for remote raw-file/STRM delivery.
            # This is a response contract only; no STRM file or DB row is created.
            raw = [{
                **quality,
                "url": _t4_media_url(request, mg, line=choice["line"]),
            } for choice, quality in zip(choices, qualities)]
            data.update(cloud_storage_info={"cloud_storage_type": 9001},
                        direct_link_qualities=raw)
        _log.info("T4 stream media=%s client=%s wrapper=%s delivery=%s lines=%d "
                  "qualities=%d audio=%d elapsed_ms=%d",
                  mg, _client_of(request).label, playback.wrapper,
                  "server" if playback.needs_proxy else "direct", len(choices),
                  len(qualities), len(sl["audio_streams"]),
                  int((time.monotonic() - started) * 1000))
        return JSONResponse({"code": 0, "msg": "", "data": data})
    if target.get("kind") == "episode":
        sl = inj.episode_stream_list(target)
    else:
        brief = store.get_title_by_guid(target["online_guid"])
        sl = inj.stream_list(brief)
    f = sl["files"][0]
    v = sl["video_streams"][0]
    quals = _qualities(target)
    if quals:
        # 高亮必须与【实际播放线路 target.line】一致，不能盲目标第一条，
        # 否则 auto 竞速/冠军线与菜单首项不同时客户端会高亮错误线路。
        cur = next((q for q in quals
                    if _line_of_bitrate(q["bitrate"],
                                        [c["line"] for c in _line_choices(target)])
                    == target.get("line")), None)
        if cur is None:
            cur = next((q for q in quals
                        if q["resolution"] == (target.get("label")
                                               or _friendly_line(target["line"]))),
                       quals[0])
        v["resolution_type"] = cur["resolution"]
        v["bps"] = cur["bitrate"]
    return JSONResponse({"code": 0, "msg": "", "data": {
        "file_stream": f, "video_stream": v,
        "audio_streams": [], "subtitle_streams": [],
        "qualities": quals, "cloud_storage_info": None}})


@app.api_route("/v/api/v1/media/range/{mg}", methods=["GET", "HEAD"])
async def media_range(mg: str, request: Request):
    """App（Flutter 原生播放器）不走 /play/play，而是直接 GET /media/range/<mg>。

    官方后端对我们的虚拟 mg 无本地文件记录，会返回 403（App 提示“文件权限不足”）。
    T4 直接流式交付，保留 Range 和线路选择；CMS 引导到既有代理（302）。
    非在线 mg 原样流式透传官方。
    """
    target = _fixed_media_target(mg, request) or store.resolve_media_guid(mg)
    chart_g = "" if target else chart_guid_by_media(mg)
    if not target and chart_g:
        # 无论匹配中、快源未命中还是已确认无源，都【绝不返回 503】（会弹“播放出错”）：
        # 一律 302 到占位 preset，由其后台绑定/轮询自愈；已过 dead 冷却则顺带重试。
        _dead = _CHART_FAIL_LEVEL.get(chart_g) == "dead"
        if not _dead or _chart_cooldown_left(chart_g) <= 0:
            start_resolve_chart(chart_g)
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index=0",
                                status_code=302)
    if not target:
        # 本地真实媒体字节（Flutter 原生播放器 Range 请求）：必须流式透传，
        # 不能整包缓冲几 GB 的影片，否则打满内存/swap 拖死整机。
        return await forward_upstream_streaming(request)
    if target.get("t4"):
        return await t4_range(mg, request)
    # 同样【不在此同步解析】（会卡 App 起播、像点了没反应）：先瞬时 302 到对应代理，
    # 由 preset/main、direct 内部在播放加载阶段解析，并有双向 302 兜底。
    _wrapper, is_hls = _guess_wrapper(target)
    # auto=1：这是“播放器自动起播”（非用户手动切线），main 见到它即按冠军线/竞速
    # 选本片实测最快的源秒播，四端（Web/iOS/Android/TV）行为统一；URL 仍带默认线
    # 名是为了在无更快预测时回落到它。用户从线路菜单手切的请求不带 auto=1，
    # 严格尊重所选线。
    params = {"line": target["line"], "index": target["index"]}
    if not target.get("_fixed_line"):
        params["auto"] = "1"
    q = urllib.parse.urlencode(params)
    if is_hls:
        # 普通库片：后台按片名补齐全网同名 CMS 线路（非阻塞），让线路菜单和热门库
        # 一样完整、名称规范；本次仍用记录内既有线路秒播。
        try:
            _og = target.get("online_guid")
            if _og and not chart_cache.is_chart_guid(_og):
                start_fill_lines(_og)
        except Exception:
            pass
        # 与 play/play 一致：后台立刻【并发预热全部普通 CMS 线路】。App 经 302 到
        # preset 后会串行预取每条变体的 main，这里从 stream（App 最早必经）就并发
        # 建热所有廉价 m3u8 清单，等它串行拉到时逐条命中，避免慢线(2.5~6s/条)串行
        # 累计成“加载半天”。网盘/加固等贵线不预热。
        try:
            _tgts = [t for t in store.target_lines(target)[:_PREWARM_MAX]
                     if not t.get("_pansou_manual") and not t.get("xsrc")
                     and not any(t.get(k) for k in ("catvod", "drpy", "guard"))
                     and (t.get("m3u8") or
                          re.search(r"\.m3u8(\?|$)", str(t.get("url") or ""), re.I))]
            if _tgts:
                _warm_preset_lines(mg, _tgts, request)
            else:
                _warm_playlist(mg, target, request)
        except Exception:
            pass
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}",
                                status_code=302)
    return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)


def _guess_wrapper(target: dict) -> tuple[str, bool]:
    """不发起任何网络请求，预判该线路该用 HLS 还是 MP4 包装。
    返回 (wrapper, is_hls)。预判可能失误：main/direct 入口在真正解析后会做
    双向 302 兜底，所以这里宁可默认 HLS（绝大多数资源是 m3u8）。"""
    url = (target.get("url") or "")
    path = url.split("?", 1)[0].lower()
    # 裸 mp4 直链（非跳转/解析页）才直接判 MP4
    if not target.get("redirect") and not target.get("resolve") \
            and re.search(r"\.mp4$", path):
        return "MP4", False
    # 跳转页 / 分享解析页 / 普通直链默认 HLS；解析后若实为 mp4，main 会 302 到 direct
    return "HLS", True


@app.post("/v/api/v1/play/play")
async def play_play(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    base_target = _fixed_media_target(mg, request) or store.resolve_media_guid(mg)
    if not base_target and chart_guid_by_media(mg):
        # 未绑定榜单片：play/play 也立即返回占位 HLS，播放器先进入加载页；
        # preset/main 请求时再等待后台匹配，避免 play/info+stream+play 串行等待。
        start_resolve_chart(chart_guid_by_media(mg))
        return _instant_play_response(mg, body.get("video_guid", ""))
    if not base_target:
        return await forward_upstream(request)
    if base_target.get("t4"):
        try:
            target = _t4_selected_target(mg, base_target, body, request)
        except t4.T4Error as exc:
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=400)
        return await _t4_play_response(mg, target, body, request)
    # 播放器「清晰度/线路」菜单切换时会带我们编码的 bitrate，据此选线路
    choices = _line_choices(base_target)
    chosen_line = None
    if base_target.get("_fixed_line"):
        chosen_line = base_target.get("line")
    elif len(choices) > 1:
        chosen_line = _line_of_bitrate(body.get("bitrate"), [t["line"] for t in choices])
    # 二级线路海报墙的固定网盘卡属于用户明确点选：直接登记手切意图并 arm，
    # 否则会被下方“网盘永不作跨片默认”的 user_pick 守卫拦截。
    if base_target.get("_fixed_line") and chosen_line == PANSOU_LINE:
        _pansou_mark_user_pick(mg)
        try:
            _pidx0 = int(base_target.get("index", 0) or 0)
            _t0 = base_target.get("title") or base_target.get("ep_title") or ""
            if not _t0:
                _og0 = base_target.get("online_guid") or store.online_guid_by_media(mg)
                _rec0 = store.get_title_by_guid(_og0) if _og0 else None
                _t0 = (_rec0 or {}).get("title") or ""
            if _t0:
                _pansou_manual_arm(mg, _t0, _pidx0)
        except Exception:
            pass
    # 网盘线永不作跨片默认：App 会记住上一部选的 bitrate 并带到新视频，而 PanSou
    # 在每部片菜单都存在，导致换新片仍默认网盘。真正手切网盘必先请求
    # preset?line=PanSou（已对该 mg 登记 user_pick）；无此意图的 bitrate 命中一律回落。
    if chosen_line == PANSOU_LINE and not _pansou_is_user_pick(mg):
        chosen_line = None
    # 用户在 App 清晰度/线路菜单手选“网盘高清（PanSou）”：播放地址直挂 PanSou 占位
    # preset（取 main 先待命，播放器拉占位分片时才搜索转存）；不预热、不参与测速。
    if chosen_line == PANSOU_LINE:
        _pansou_mark_user_pick(mg)
        _pidx = int(base_target.get("index", 0) or 0)
        # App 手切网盘：此刻能拿到片名，直接 arm（带标题）。否则后面 main 在影片未
        # 绑定/重启后反查不到片名，网盘搜索会因空关键词 0 结果而无法转存。
        try:
            _t0 = base_target.get("title") or base_target.get("ep_title") or ""
            if not _t0:
                _og0 = base_target.get("online_guid") or store.online_guid_by_media(mg)
                _rec0 = store.get_title_by_guid(_og0) if _og0 else None
                _t0 = (_rec0 or {}).get("title") or ""
            if _t0:
                _pansou_manual_arm(mg, _t0, _pidx)
        except Exception:
            pass
        _pq = urllib.parse.urlencode({"line": PANSOU_LINE, "index": _pidx})
        return JSONResponse({"code": 0, "msg": "", "data": {
            "audio_guid": "", "audio_index": -1, "hls_time": 6,
            "is_subtitle_external": 0, "media_guid": mg,
            "play_link": f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{_pq}",
            "subtitle_guid": "", "subtitle_index": -1,
            "subtitle_link": "", "video_guid": body.get("video_guid", vg_for(mg)),
            "video_index": 0, "wrapper": "HLS",
            "non_fatal_errno": None, "video_encoder": "",
            "not_supported_resolution": "", "supported_highest_resolution": ""}})
    if chosen_line and chosen_line != base_target.get("line"):
        if base_target.get("kind") == "episode" and base_target.get("ep_guid"):
            target = store.get_episode_target(base_target["ep_guid"], chosen_line) or base_target
        else:
            target = store.resolve_episode(
                base_target["online_guid"], chosen_line,
                int(base_target.get("index", 0) or 0))
            if target:
                target = dict(target, online_guid=base_target["online_guid"],
                              ep_guid="", kind="title")
            else:
                target = base_target
    else:
        target = base_target
    # 起播体验：此处【不再同步解析】播放页/302/专有解码（慢源要 1~3 秒，会把用户
    # 卡在海报墙像“点了没反应”）。先瞬时返回播放地址让播放器立刻进入加载页转圈，
    # 真正的解析延迟到播放器内部取 main.m3u8 / direct 时再做（那里本就有加载指示）。
    wrapper, is_hls = _guess_wrapper(target)
    # 固定线路卡等价于用户手动选线：不带 auto=1，禁止被自动选优覆盖。
    params = {"line": target["line"], "index": target["index"]}
    if not target.get("_fixed_line"):
        params["auto"] = "1"
    q = urllib.parse.urlencode(params)
    if is_hls:
        play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}"
        # 后台立刻预热全部普通线路（含解析 + 播放列表），播放器取 main 时大概率已就绪
        try:
            _tgts = [t for t in store.target_lines(target)[:_PREWARM_MAX]
                     if not t.get("_pansou_manual") and not t.get("xsrc")
                     and not any(t.get(k) for k in ("catvod", "drpy", "guard"))
                     and (t.get("m3u8") or
                          re.search(r"\.m3u8(\?|$)", str(t.get("url") or ""), re.I))]
            if _tgts:
                _warm_preset_lines(mg, _tgts, request)
            else:
                _warm_playlist(mg, target, request)
        except Exception:
            pass
    else:
        play_link = f"{ONLINE_PREFIX}/{mg}/direct?{q}"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": play_link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": body.get("video_guid", vg_for(mg)),
        "video_index": 0, "wrapper": wrapper,
        "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/direct")
def _direct_to_placeholder(mg: str, request: Request) -> RedirectResponse:
    """直链(mp4)取流失败：302 回占位 master，播放器不弹错、线路菜单可手动切换。
    带 ph=1，preset 见此标记直接回占位（不再给同一条坏线，避免重定向环）。"""
    didx = int(request.query_params.get("index", 0) or 0)
    return RedirectResponse(
        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index={didx}&ph=1", status_code=302,
        headers={"Cache-Control": "no-store", "X-fnmedia-Placeholder": "1"})


async def direct_proxy(mg: str, request: Request):
    """mp4 等非 HLS 直链代理：同源、带 Range 支持（播放器 seek）。"""
    failed_g = await _await_pending_chart(mg)
    target = store.resolve_media_guid(mg)
    if not target:
        # 未绑定/无源：302 到占位 master（HLS），保持播放器存活、菜单可切，不报硬错误
        didx = int(request.query_params.get("index", 0) or 0)
        return RedirectResponse(
            f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index={didx}", status_code=302,
            headers={"Cache-Control": "no-store", "X-fnmedia-Placeholder": "1"})
    # direct 的 query 会显式带线路（HLS 预判为 MP4 时回跳而来）；必须解析该线路，
    # 不能沿用默认线路，否则可能拿到另一资源站的 HLS 而再次弹回，形成重定向环。
    qline = request.query_params.get("line")
    if qline and qline != target.get("line"):
        if target.get("kind") == "episode" and target.get("ep_guid"):
            switched = store.get_episode_target(target["ep_guid"], qline)
        else:
            switched = store.resolve_episode(
                target["online_guid"], qline, int(target.get("index", 0) or 0))
        if switched:
            target = switched
    # 线路健康探测 + 自动换线（直链/小雅等非 HLS 线路）
    request.state._play_mg = mg
    if _autofail_on():
        target = await _ensure_playable(target, request)
    else:
        await _resolve_if_needed(target, request)
    src_url = target["url"]
    if re.search(r"\.m3u8(\?|$)", src_url, re.I):
        # 源是 HLS 却走到直链（MP4 预判失误）：带线路/集序重定向到 preset
        q = urllib.parse.urlencode({"line": target["line"],
                                    "index": target.get("index", 0),
                                    "auto": "1"})
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}",
                                status_code=302)
    # 网盘原画直出 mp4：src 是本服务根相对地址，经回环自调用（注入网盘 Cookie）
    src_url = _self_url(src_url)
    fwd = dict(target.get("headers") or {})
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        req = ext_client(request).build_request("GET", src_url, headers=fwd)
        r = await ext_client(request).send(req, stream=True)
    except Exception as exc:
        # 直链连接失败：记忆坏线并自动换线重试一次
        if _autofail_on():
            _mark_line_bad(target)
            alt = await _find_playable(target, request, skip_current=True)
            if alt is not None:
                _log.info("直链失败，自动换线 %s -> %s", target.get("line"),
                          alt.get("line"))
                if _client_direct_only(alt):
                    return RedirectResponse(alt["url"], status_code=302,
                                            headers={"Cache-Control": "no-store"})
                if re.search(r"\.m3u8(\?|$)", alt["url"], re.I):
                    q = urllib.parse.urlencode({"line": alt["line"],
                                                "index": alt.get("index", 0)})
                    return RedirectResponse(
                        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}", status_code=302)
                target = alt
                fwd = dict(target.get("headers") or {})
                if request.headers.get("range"):
                    fwd["Range"] = request.headers["range"]
                try:
                    req = ext_client(request).build_request(
                        "GET", target["url"], headers=fwd)
                    r = await ext_client(request).send(req, stream=True)
                except Exception as exc2:
                    return _direct_to_placeholder(mg, request)
            else:
                return _direct_to_placeholder(mg, request)
        else:
            return _direct_to_placeholder(mg, request)
    # 上游明确 4xx/5xx（文件被删/鉴权失败）：记忆坏线，换线重定向重试
    if r.status_code >= 400 and _autofail_on():
        await r.aclose()
        _mark_line_bad(target)
        alt = await _find_playable(target, request, skip_current=True)
        if alt is not None:
            _log.info("直链 HTTP%s，自动换线 -> %s", r.status_code, alt.get("line"))
            if _client_direct_only(alt):
                return RedirectResponse(alt["url"], status_code=302,
                                        headers={"Cache-Control": "no-store"})
            if re.search(r"\.m3u8(\?|$)", alt["url"], re.I):
                q = urllib.parse.urlencode({"line": alt["line"],
                                            "index": alt.get("index", 0)})
                return RedirectResponse(
                    f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}", status_code=302)
        return _direct_to_placeholder(mg, request)
    h = _safe_response_headers(r.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"

    async def pump():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(pump(), status_code=r.status_code, headers=h,
                             media_type=r.headers.get("content-type", "video/mp4"))


def vg_for(mg: str) -> str:
    t = store.resolve_media_guid(mg)
    if not t:
        return ""
    if t.get("kind") == "episode":
        return ep_video_guid(t["ep_guid"])
    return video_guid(t["online_guid"])


@app.post("/v/api/v1/person/list/{guid}")
async def person_list(guid: str, request: Request):
    if not store.get_title_by_guid(guid) and not store.get_episode(guid):
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": {"total": 0, "list": []}})


@app.post("/v/api/v1/play/record")
async def play_record(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    ig = body.get("item_guid", "")
    if _parse_line_item_guid(ig) or _parse_line_season_guid(ig):
        # 固定线路卡上报：归一化为原影片/基础集 guid 再记录，保证续播身份统一、
        # 不按资源站拆分；media/video guid 保留线路信息仅作参考，不参与历史键。
        body["item_guid"] = _normalize_history_guid(ig)
        history.record(body)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    if store.get_title_by_guid(ig) or store.get_episode(ig):
        # 记录在线播放进度到本地（继续观看），不上报官方库
        history.record(body)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    return await forward_upstream(request)


@app.delete("/v/api/v1/play/record")
async def play_record_delete(request: Request):
    import json
    ids = []
    try:
        body = await request.body()
        if body:
            j = json.loads(body)
            if isinstance(j, dict):
                g = j.get("item_guid") or j.get("guid")
                ids = g if isinstance(g, list) else ([g] if g else [])
            elif isinstance(j, list):
                ids = [x.get("item_guid") if isinstance(x, dict) else x for x in j]
    except Exception:
        ids = []
    ids = [_normalize_history_guid(g) for g in ids
           if g and (_parse_line_item_guid(g) or _parse_line_season_guid(g)
                     or store.get_title_by_guid(g) or store.get_episode(g))]
    if ids:
        history.delete(ids)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    return await forward_upstream(request)


@app.get("/v/api/v1/play/list")
async def play_list(request: Request):
    upstream = await forward_upstream(request)
    try:
        import json
        data = json.loads(upstream.body)
    except Exception:
        return upstream
    local_items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(local_items, list):
        local_items = []

    online_items = []
    # 同一部剧的多集只展示最近在看的那一集（issue #20）：以剧集 online_guid 折叠，
    # 无 ep 信息的标题按自身 guid 独立成卡。
    best_by_show: dict[str, tuple[float, dict]] = {}
    standalone = []
    for rec in history.all():
        ig = rec.get("item_guid", "")
        ep = store.get_episode(ig)
        brief = store.get_title_by_guid(ig)
        if ep:
            brief = store.get_title_by_guid(ep["online_guid"])
        if not brief:
            continue  # 缓存已淘汰的标题不再展示
        show = ep.get("online_guid") if ep else brief.get("guid") or ig
        up = float(rec.get("updated_at", 0) or 0)
        card = inj.history_item(brief, rec, ep)
        card["_show"] = show
        if show:
            prev = best_by_show.get(show)
            if prev is None or up >= prev[0]:
                best_by_show[show] = (up, card)
        else:
            standalone.append(card)
    online_items = [c for _, c in best_by_show.values()] + standalone

    # 在线历史按最近播放排序，置顶；按剧/标题 guid 与官方去重
    online_items.sort(key=lambda x: x.get("_updated_at", 0), reverse=True)
    online_guids = {x.get("guid") for x in online_items} | \
        {x.get("_show") for x in online_items if x.get("_show")}
    local_only = [x for x in local_items if x.get("guid") not in online_guids]
    # 剥掉私有字段
    for o in online_items:
        o.pop("_online", None)
        o.pop("_updated_at", None)
        o.pop("_year", None)
        o.pop("_show", None)
    online_items = clientmod.normalize_cards(online_items, _client_of(request))
    return JSONResponse({"code": 0, "msg": "", "data": online_items + local_only},
        headers={"Content-Type": "application/json; charset=utf-8"})


# 在线 RPC 不交给官方文件任务；T4 HLS 切线更新独立播放会话。
@app.post("/v/api/v1/media/p")
async def media_p(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    link = body.get("playLink", "")
    try:
        parsed = urllib.parse.urlsplit(link) if isinstance(link, str) else None
    except ValueError:
        parsed = None
    online = bool(parsed and parsed.path.startswith(ONLINE_PREFIX + "/"))
    if online:
        parts = parsed.path.removeprefix(ONLINE_PREFIX + "/").split("/")
        session = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
        if len(parts) == 2 and parts[1] == "t4" and session:
            mg = parts[0]
            target = _get_t4_service().session_target(mg, session)
            if target is None:
                return JSONResponse({"code": -1, "msg": "播放会话已过期，请重新播放",
                                     "data": {"result": "fail"}}, status_code=410)
            if body.get("req") == "media.resetQuality":
                try:
                    quality = body.get("quality") or {}
                    names = [choice["line"] for choice in _line_choices(target)]
                    line = _line_of_bitrate(quality.get("bitrate"), names)
                    if line is None:
                        raise t4.T4Error("请选择 T4 源线路，不支持服务端转码清晰度")
                    target = _t4_selected_target(mg, target, {"line": line}, request)
                    playback = await _get_t4_service().resolve(ext_client(request), target)
                    await _get_t4_media().identify(ext_client(request), playback)
                    if playback.wrapper != "HLS":
                        raise t4.T4Error("线路媒体格式不同，请重新进入播放")
                    _get_t4_service().set_session_target(mg, session, target)
                except t4.T4Error as exc:
                    return JSONResponse({"code": -1, "msg": str(exc),
                                         "data": {"result": "fail"}}, status_code=502)
            elif body.get("req") == "media.quit":
                _get_t4_service().close_session(mg, session)
    if isinstance(link, str) and link and (
            online or _get_t4_service().owns_playback_url(link)):
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"reqid": body.get("reqid", ""), "result": "succ"}})
    return await forward_upstream(request)


# ------------------------------------------------------------- HLS 代理

def _line_key_match(want: str, got: str) -> bool:
    """判断请求线路名与实际选中线路是否同一条（兼容 源前缀::显示名 与裸显示名）。"""
    if not want or not got:
        return False
    if want == got:
        return True
    def disp(k: str) -> str:
        return k.split("::", 1)[1] if "::" in k else k
    return disp(want) == disp(got)


def _pick_episode(mg: str, request: Request) -> dict | None:
    # 固定线路卡：优先按 guid 中携带的影片/线路/集序号直接解析，禁止自动选优。
    fixed = _fixed_media_target(mg, request)
    if fixed is not None:
        return fixed
    # mg 本身是 chart guid（媒体库直进）：绑定记录挂在其确定性 media guid 上，
    # 先按 media guid 取集；取不到再落入下面的常规解析（兼容旧记录/回源）。
    if mg and chart_cache.is_chart_guid(mg) and not store.resolve_media_guid(mg):
        _t = _pick_episode(media_guid(mg), request)
        if _t:
            return _t
    # 全网回源别名：内置线路全死、已按片名找到别的可播记录时，原 mg 改指新记录
    alt_og = _MEDIA_REDIRECT.get(mg)
    if alt_og:
        try:
            index = int(request.query_params.get("index", "0"))
        except ValueError:
            index = 0
        r = store.resolve_episode(alt_og, request.query_params.get("line"), index)
        if r:
            return dict(r, online_guid=alt_og, kind="title")
    # 优先：mg 已绑定具体某集（TV 选集）或标题第一集
    target = store.resolve_media_guid(mg)
    if target:
        # query 可显式覆盖集序/线路（master 变体、STRM/调试用）
        if request.query_params.get("index") is not None or request.query_params.get("line"):
            try:
                idx = int(request.query_params.get("index", target.get("index", 0)))
            except ValueError:
                idx = target.get("index", 0)
            want_line = request.query_params.get("line")
            r = store.resolve_episode(
                target["online_guid"], want_line, idx)
            # 用户显式选的线路不在本条同名记录里（同名多记录、各挂部分线路）：
            # 回退到含该线路、线路最全的同名聚合记录，避免静默放第一条死链。
            if r and want_line and not _line_key_match(want_line, r.get("line", "")):
                alt_og = store.find_title_with_line(
                    target.get("title", ""), want_line)
                if alt_og and alt_og != target["online_guid"]:
                    ar = store.resolve_episode(alt_og, want_line, idx)
                    if ar:
                        r = ar
                        target = dict(target, online_guid=alt_og)
            if r:
                return dict(r, online_guid=target["online_guid"],
                            ep_guid=target.get("ep_guid", ""), kind=target.get("kind"))
        return target
    # 兼容：未在索引中的旧 mg，退回标题解析；也支持秒进时登记的待绑定榜单片
    og = _MEDIA_REDIRECT.get(mg) or store.online_guid_by_media(mg) \
        or _PENDING_MEDIA_CHART.get(mg)
    if not og:
        return None
    try:
        index = int(request.query_params.get("index", "0"))
    except ValueError:
        index = 0
    return store.resolve_episode(og, request.query_params.get("line"), index)


def _client_direct_only(t: dict) -> bool:
    """该已解析目标必须让客户端直连 302（网盘 /d 直链等），不能走本代理抓流。

    特性中立：不直接引用私有模块名，公开构建同样成立。判定依据是解析阶段对
    “客户端直连”类型打的标记。"""
    return bool(t.get("client_direct"))


def _dodge_known_bad(picked: dict, request: Request) -> dict:
    """零成本换线：当前线在坏线记忆/源级熔断中、且用户没有显式指定该线时，换到健康候选。
    让“退出重放”不再黏在上一次切坏的线路上（不发起任何探测网络请求）。"""
    if not picked or not _autofail_on():
        return picked

    def _is_bad(p: dict) -> bool:
        if _line_is_bad(p):
            return True
        # 源级取流熔断（整站取流域名不可达）：同样零成本跳过
        try:
            sk = sourcehealth.source_key_of(p.get("line"), p)
            return bool(sk) and sourcehealth.is_dead(sk)
        except Exception:
            return False

    if not _is_bad(picked):
        return picked
    # 用户在播放器里明确选了这条坏线（query 带 line）则尊重其选择，交给探测逻辑
    if request.query_params.get("line"):
        return picked
    for c in _episode_candidates(picked):
        if not _is_bad(c):
            _log.info("跳过已知坏线/熔断源 %s -> %s", picked.get("line"), c.get("line"))
            return c
    return picked


async def _await_pending_chart(mg: str) -> str:
    """若该 media 属于秒进播放页、尚未绑定完成的榜单片，等待其后台绑定结束。"""
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    if og and chart_cache.is_chart_guid(og):
        existing = store.get_title_by_guid(og)
        if existing and not _chart_binding_matches(og, existing):
            _drop_chart_binding(og)
        if not store.resolve_media_guid(mg):
            await resolve_chart(og)
            return og if not store.resolve_media_guid(mg) else ""
    return ""


def _seg_url(mg: str, absolute: str, qs: str = "") -> str:
    suffix = f"&{qs}" if qs else ""
    return f"{ONLINE_PREFIX}/{mg}/seg?u={b64e(absolute)}{suffix}"


# 混合直连：按源站 host 记忆“裸连（不带 Referer/UA）是否可播”，避免每条线重复探测。
# 值为 (时间戳, 是否可直连)；TTL 由 config.direct_play_probe_ttl 控制。
_DIRECT_HOST_CACHE: dict[str, tuple[float, bool]] = {}


def _direct_play_on() -> bool:
    try:
        return bool(config.get().get("direct_play_enabled", False))
    except Exception:
        return False


async def _direct_eligible(client: httpx.AsyncClient, url: str) -> bool:
    """探测某分片 URL 在播放端裸连（无自定义头）下是否可直连。

    只做一次轻量 GET 读 2KB 即停；同 host 结果按 TTL 记忆。任何异常/非 2xx 都
    判定不可直连，调用方自动回退中转，绝不影响可播性。"""
    if not _direct_play_on() or not url:
        return False
    host = urllib.parse.urlsplit(url).netloc.lower()
    if not host:
        return False
    try:
        ttl = float(config.get().get("direct_play_probe_ttl", 1800) or 1800)
    except (TypeError, ValueError):
        ttl = 1800.0
    now = time.time()
    hit = _DIRECT_HOST_CACHE.get(host)
    if hit and now - hit[0] < ttl:
        return hit[1]
    ok = False
    if not bool(config.get().get("direct_play_probe", True)):
        # 关闭探测=乐观直连（仍由失败回退兜底）
        ok = True
    else:
        try:
            async with client.stream("GET", url, headers={},
                                     timeout=httpx.Timeout(8.0)) as rr:
                if rr.status_code in (200, 206):
                    async for _ in rr.aiter_bytes(2048):
                        break
                    ok = True
        except Exception:
            ok = False
    _DIRECT_HOST_CACHE[host] = (now, ok)
    if len(_DIRECT_HOST_CACHE) > 512:
        for old in list(_DIRECT_HOST_CACHE)[:256]:
            _DIRECT_HOST_CACHE.pop(old, None)
    return ok







# —— 多网盘识别注册表 ———————————————————————————————————————————————
# “网盘资源”的判定：播放地址/线路名/影片名里出现某家网盘的分享链接，或四盘
# 聚合源的中转占位（Seedhub/盘搜类）。纯通用识别（仅 re），公开/私有构建都需要，
# 因此必须放在 PRIVATE 块之外：lite 包的播放排序也会调用 _pan_match。
_PAN_PROVIDERS = [
    {"key": "quark", "name": "夸克网盘", "playable": True,
     "patterns": [r"pan\.quark\.cn/s/[A-Za-z0-9]+",
                  r"drive\.quark\.cn/[^\s\"']*"]},
    {"key": "uc", "name": "UC网盘", "playable": True,
     "patterns": [r"(?:pan|drive)\.uc\.cn/s/[A-Za-z0-9]+",
                  r"drive\.uc\.cn/[^\s\"']*"]},
    {"key": "ali", "name": "阿里云盘", "playable": True,
     "patterns": [r"(?:alipan|aliyundrive)\.com/s/[A-Za-z0-9]+",
                  r"www\.alipan\.com/s/[A-Za-z0-9]+"]},
    {"key": "baidu", "name": "百度网盘", "playable": False,
     "patterns": [r"pan\.baidu\.com/s/[A-Za-z0-9_%-]+"]},
    {"key": "xunlei", "name": "迅雷云盘", "playable": False,
     "patterns": [r"pan\.xunlei\.com/[A-Za-z0-9]+",
                  r"(?:pan\.xunlei\.com|xunlei\.com)/[^\s\"']*(?:s/|share)"]},
    {"key": "p115", "name": "115网盘", "playable": False,
     "patterns": [r"115\.com/s/[A-Za-z0-9]+"]},
    {"key": "tianyi", "name": "天翼云盘", "playable": False,
     "patterns": [r"cloud\.189\.cn/[^\s\"']+",
                  r"(?:cloud|pan)\.189\.cn/[^\s\"']*"]},
    {"key": "yidong", "name": "移动云盘", "playable": False,
     "patterns": [r"yun\.139\.com/[^\s\"']+"]},
]
for _pv in _PAN_PROVIDERS:
    _pv["re"] = re.compile("|".join(_pv["patterns"]), re.I)

# 四盘聚合源（Seedhub 等）的中转占位：redirect_to=pan_id_<id>，最终由其 302
# 解出真实夸克链接（前置 Cloudflare 时解不开则回退）。
_PAN_LINKSTART_RE = re.compile(r"link_start/[^\s\"']*redirect_to=pan_id_(\d+)")


def _pan_match(text: str):
    """在一段文本（播放地址/线路名/影片名）里识别网盘分享，返回 provider 或 None。"""
    if not text:
        return None
    s = str(text)
    for pv in _PAN_PROVIDERS:
        if pv["re"].search(s):
            return pv
    return None


# ===== 手动 PanSou 线：lite 安全兜底 =====
# 真实实现（搜索/转存状态机）在下方 GUARD 私有特性块内，公开 lite 构建整块剥离。
# 但共享的播放/选线代码仍会引用下列名字；此处给恒假/空操作定义，保证 lite 不产生
# NameError、也绝不会出现网盘线。full 版加载时块内真实定义会覆盖这些兜底。
PANSOU_LINE = "builtin-pansou::PanSou网盘高清"
PANSOU_LABEL = "🎞️网盘高清（PanSou）"


def _is_pansou_line_name(line: str) -> bool:
    # lite 恒无网盘线：只有内置 pansou 名才认
    try:
        return bool(line) and str(line).startswith("builtin-pansou::")
    except Exception:
        return False


def _pansou_available() -> bool:
    return False


def _pansou_manual_target(index: int = 0) -> dict:  # lite 不会走到（菜单不追加）
    return {}


def _pansou_manual_state(mg: str) -> str:
    return ""


def _pansou_manual_arm(mg: str, title: str, index: int = 0) -> None:
    return None


def _pansou_manual_kick(mg: str) -> None:
    return None


def _pansou_mark_user_pick(mg: str) -> None:
    return None


def _pansou_is_user_pick(mg: str) -> bool:
    return False


async def _pansou_login_qr_png(mg: str):
    # lite 恒无网盘模块：返回 None，占位路由回退普通占位分片。
    return None


_PANSOU_MAN_SNAP: dict[str, dict] = {}





# ===== 播放线路健康探测与自动换线 =====
# 问题背景：用户手动切到某条坏线（文件已删/网盘无此集/源站失效）后，插件只是把该线
# 302/下发，从不校验死活，播放器一直报错；坏线也不会被记忆，退出重放仍黏在该线。
# 方案：①坏线短时记忆 ②起播/取流前并行探测同集各候选线路，自动切到首个可用线路。
_LINE_BAD_TTL = 300.0          # 坏线记忆 5 分钟（签名时效内，避免长期误判偶发抖动）
_LINE_BAD: dict[str, float] = {}
_PROBE_TIMEOUT = 8.0
_PROBE_MAX_CANDIDATES = 8     # 最多探测候选线路数（控制起播开销）
_PROBE_RANGE = "bytes=0-2047"


def _line_bad_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{picked.get("line", "")}|{int(picked.get("index", 0) or 0)}'


def _mark_line_bad(picked: dict) -> None:
    if not picked or not picked.get("line"):
        return
    _LINE_BAD[_line_bad_key(picked)] = time.time()
    if len(_LINE_BAD) > 1000:
        for k in list(_LINE_BAD)[:500]:
            _LINE_BAD.pop(k, None)


def _line_is_bad(picked: dict) -> bool:
    ts = _LINE_BAD.get(_line_bad_key(picked))
    if ts is None:
        return False
    if time.time() - ts >= _LINE_BAD_TTL:
        _LINE_BAD.pop(_line_bad_key(picked), None)
        return False
    return True


def _autofail_on() -> bool:
    try:
        return bool(config.get().get("line_autofail", True))
    except Exception:
        return True


def _episode_candidates(picked: dict, include_current: bool = True) -> list[dict]:
    """枚举该集在各线路下的可播目标（按 store 默认顺序：默认线在前）。
    直接复用索引，不发起任何网络请求。"""
    og = picked.get("online_guid", "")
    idx = int(picked.get("index", 0) or 0)
    cur_line = picked.get("line")
    brief = store.get_title_by_guid(og) if og else None
    if not brief:
        return []
    default = store.best_line(og)
    ordered = ([default] if default else []) + \
              [ln for ln in brief.get("lines", {}) if ln != default]
    out: list[dict] = []
    seen = set()
    for ln in ordered:
        if not include_current and ln == cur_line:
            continue
        eps = store._direct_eps(brief, ln)
        if idx >= len(eps):
            continue  # 该线路没有这一集（如小雅仅 1 集），直接排除
        t = store.resolve_episode(og, ln, idx)
        if not t or not t.get("url"):
            continue
        t = dict(t, ep_guid=picked.get("ep_guid", ""),
                 kind=picked.get("kind", "title"))
        if t["url"] in seen:
            continue
        seen.add(t["url"])
        out.append(t)
    return out


def _ordered_candidates(picked: dict) -> list[dict]:
    """候选排序：当前线置顶，已知坏线沉底。"""
    cands = _episode_candidates(picked)
    cur = picked.get("line")

    def rank(t: dict) -> tuple:
        sk = sourcehealth.source_key_of(t.get("line"), t)
        # 当前线置顶 → 坏线/死源沉底 → 按源健康画像（成功率/延迟）排序
        return (t["line"] != cur,
                _line_is_bad(t),
                sourcehealth.weight(sk) if sk else (1, 5.0, 1.0))

    cands.sort(key=rank)
    return cands


async def _latency_from_playlist(body: str, cand: dict,
                                 request: Request) -> float | None:
    """从改写后的播放列表取首个分片，经本代理实测其完整下载耗时。"""
    try:
        first = next((ln.strip() for ln in body.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")), "")
        if not first:
            return None
        url = _self_url(first) if first.startswith("/") else first
        return await _first_seg_download_latency(
            src_client(request), url, cand.get("headers")
            or {"User-Agent": "Mozilla/5.0"})
    except Exception:
        return None


# 有界流式测速：读到 _PROBE_SPEED_BYTES 或超过窗口即停，把探测流量压在很小的量级，
# 避免海报翻页/点击竞速整段下载抢占 NAS 上行拖慢真实播放。同时记录这段的【吞吐】，
# 因为只比首块延迟会把“首字节快、但持续吞吐只有 ~200KB/s（边看边卡）”的慢 CDN
# 误选为最快；评分对低于流畅码率的吞吐追加惩罚。
_PROBE_SPEED_BYTES = 393216     # 目标读取 384KB（约半秒流畅码率的数据量）
_PROBE_SPEED_WINDOW = 2.0       # 最长只在分片上花 2s（读不够也停）
_PROBE_SPEED_FLOOR = 700 * 1024  # 流畅码率阈值 700KB/s；低于它按缺口折算惩罚


async def _first_seg_speed(
        cli, seg_url: str, headers: dict, timeout: float = None
        ) -> tuple[float, int, float] | None:
    """有界流式测首个分片，返回 (评分秒数, 读到字节, 吞吐B/s)。

    优先发 Range 只取 384KB；对忽略 Range 强制回整段(~1MB)的加密流，流式读到
    目标字节或 2s 即主动关闭（不下整段）。评分 = 实测耗时 + 吞吐不足惩罚：
    既能识破慢 CDN，又不让探测下载压垮上行。失败/空体返回 None。"""
    timeout = timeout or _PROBE_SPEED_WINDOW
    t = time.time()
    ttfb = None
    try:
        h = dict(headers or {})
        h["Range"] = f"bytes=0-{_PROBE_SPEED_BYTES - 1}"
        req = cli.build_request("GET", seg_url, headers=h)
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return None
            got = 0
            try:
                async with asyncio.timeout(timeout):
                    async for chunk in r.aiter_bytes():
                        if ttfb is None:
                            ttfb = time.time() - t
                        got += len(chunk)
                        if got >= _PROBE_SPEED_BYTES:
                            break
            except Exception:
                pass
            # 错误页/空体判死
            if got <= 0:
                return None
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
        elapsed = max(time.time() - t, 1e-3)
        ttfb = ttfb or elapsed
        rate = got / elapsed
        # 评分以【首字节】为主（决定秒播）；只有吞吐低于流畅码率时才加一个小幅惩罚
        # （决定会不会边看边卡），封顶 1.2s 避免淹没 TTFB 的主导地位。
        penalty = 0.0
        if rate < _PROBE_SPEED_FLOOR:
            penalty = min(1.2, (_PROBE_SPEED_FLOOR - rate) /
                          max(_PROBE_SPEED_FLOOR, 1) * 1.2)
        return ttfb + penalty, got, rate
    except Exception:
        return None


async def _first_seg_download_latency(
        cli, seg_url: str, headers: dict, timeout: float = None) -> float | None:
    """兼容封装：只返回评分秒数。"""
    m = await _first_seg_speed(cli, seg_url, headers, timeout)
    return m[0] if m else None


async def _probe_latency(cand: dict, request: Request,
                         build_hls: bool = False,
                         measure_seg: bool = False) -> float | None:
    """探测一条线路该集是否可播，并返回“起播延迟”（秒，越小越快）。

    会就地解析（专有源/跳转/分享，与真实播放同一套逻辑）。失败/文件不存在返回 None。
    build_hls=True 时，HLS 线路顺手构建改写后的播放列表并填缓存（main 取流零额外抓取）；
    竞速阶段传 False，只做轻量验活（抓原始清单，不改写），选出胜者后再构建。
    measure_seg=True（竞速/海报预测）时，即使清单已缓存也实测首分片完整下载耗时，
    避免加密慢流“首字节 0s”式误判。"""
    t0 = time.time()
    try:
        await _resolve_if_needed(cand, request)
        raw_url = cand.get("url", "")
        if not raw_url:
            return None
        # 网盘转存后是本服务根相对地址（/v/api/ext/pan/play?fid=…），经回环自调用
        url = _self_url(raw_url)
        headers = cand.get("headers") or {"User-Agent": "Mozilla/5.0"}
        cli = src_client(request)

        # 非 HLS（网盘 /d 直链、mp4 等）：Range GET 探测
        is_m3u8 = bool(cand.get("m3u8")) or bool(
            re.search(r"\.m3u8(\?|$)", url.split("?", 1)[0], re.I))
        # 网盘播放代理按 fid 实时换链：转码档返回清单、原画直出 mp4，URL 无后缀，
        # 直接交给播放列表构建（build_hls）或轻量抓清单判定，避免误走 Range 探测。
        is_pan_proxy = "/v/api/ext/pan/" in url
        if not is_m3u8 and not is_pan_proxy:
            try:
                req = cli.build_request(
                    "GET", url, headers={**headers, "Range": _PROBE_RANGE})
                r = await cli.send(req, follow_redirects=True)
                try:
                    status = r.status_code
                    ctype = (r.headers.get("content-type") or "").lower()
                    body = b""
                    try:
                        body = await r.aread()
                    except Exception:
                        pass
                finally:
                    try:
                        await r.aclose()
                    except Exception:
                        pass
            except Exception:
                return None
            if status >= 400:
                return None
            # 小雅/AList 对“目录或不存在文件”回 200 + JSON 错误（假活），判死
            if "json" in ctype or body[:1] in (b"{", b"["):
                return None
            return time.time() - t0

        # HLS
        key = _playlist_cache_key(cand)
        cached = _PLAYLIST_CACHE.get(key)
        if cached:
            body = cached[1]
            if body is not _NOT_HLS and \
                    not await _verify_rewritten_first_seg(body, request, cand):
                _PLAYLIST_CACHE.pop(key, None)
                return None
            if measure_seg and body is not _NOT_HLS:
                seg_lat = await _latency_from_playlist(body, cand, request)
                if seg_lat is not None:
                    return seg_lat
            return time.time() - t0
        if build_hls:
            mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
            if mg:
                body = await _get_playlist_cached(mg, cand, request)
                if body is None:
                    return None
                if body is not _NOT_HLS and \
                        not await _verify_rewritten_first_seg(body, request, cand):
                    _PLAYLIST_CACHE.pop(key, None)
                    return None
                return time.time() - t0
        # 轻量验活：抓原始清单（跟随嵌套 master），不做分片改写
        text, _cur = await _fetch_playlist(request, url, headers)
        if text is None or not _is_media_playlist(text):
            return None
        # 分片级验活：部分加固源（海绵/糯米的 yximgs PNG 伪装流等）清单能下、
        # 但所有分片在源站 404（清单活、分片死）。只验清单会误判可播，起播即报错。
        # 取首个分片 Range 2KB 复核：4xx/5xx 或空体即判死，触发自动换线。
        try:
            first_seg = next(
                (ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")), "")
            if first_seg:
                seg_url = urllib.parse.urljoin(_cur, first_seg)
                if measure_seg:
                    # 竞速/预测：完整下载首分片测真实取流耗时（加密流忽略 Range）
                    seg_lat = await _first_seg_download_latency(cli, seg_url, headers)
                    if seg_lat is None:
                        return None
                    return seg_lat
                sreq = cli.build_request(
                    "GET", seg_url, headers={**headers, "Range": _PROBE_RANGE})
                sr = await cli.send(sreq, follow_redirects=True)
                try:
                    if sr.status_code >= 400:
                        _log.info("线路首分片%d判死: %s 《%s》%s",
                                  sr.status_code, cand.get("line"),
                                  cand.get("title"),
                                  urllib.parse.urlsplit(seg_url).netloc)
                        return None
                    head = b""
                    try:
                        head = await sr.aread()
                    except Exception:
                        pass
                    # 源站对失效分片常回 200/404 + XML/HTML 错误页（非媒体）
                    if not head or head[:1] in (b"<", b"{", b"["):
                        return None
                finally:
                    try:
                        await sr.aclose()
                    except Exception:
                        pass
        except Exception:
            return None
        return time.time() - t0
    except Exception:
        return None


async def _probe_target(cand: dict, request: Request) -> bool:
    """探测某条线路是否可播（HLS 顺带构建播放列表缓存）。布尔版，供故障切换复用。"""
    lat = await _probe_latency(cand, request, build_hls=True)
    sk = sourcehealth.source_key_of(cand.get("line"), cand)
    if sk:
        sourcehealth.record(sk, lat is not None, lat or 0.0)
    return lat is not None


# 首分片验活的短期正缓存：真实分片 URL -> 验活时间。签名 URL 会变，自然按新
# URL 重新验活。避免正常播放每次取清单都探测上游。
_SEG_OK_CACHE: dict[str, float] = {}
_SEG_OK_TTL = 180.0


async def _verify_rewritten_first_seg(body: str, request: Request,
                                      picked: dict | None = None) -> bool:
    """对已改写的清单做首分片验活：解码 /seg?u= 里的真实上游地址，直接 Range 探测。
    识别“清单活、分片 404/错误页”的死链（如某源 yximgs CDN 删流）。不在进程内
    回环请求自身（会受 worker 并发影响而误判）。探测异常不轻易判死，避免误伤。"""
    # 本次请求的竞速阶段已对该线做过 Range 分片头验活：直接放行，不重复探测，
    # 复刻 0.2.17“验活只做一次”，省掉建清单后的又一次串行往返。
    try:
        _verified = getattr(request.state, "_race_verified", None)
        if _verified and picked and picked.get("line") in _verified:
            return True
    except Exception:
        pass
    try:
        first = next((ln.strip() for ln in body.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")), "")
        if not first or first.startswith("hls?"):
            return True  # 紧凑分片/无分片信息：交由实际分片端点处理，不预判
        qs = urllib.parse.urlparse(first if first.startswith("http")
                                   else _self_url(first)).query
        u = urllib.parse.parse_qs(qs).get("u", [""])[0]
        if not u:
            return True
        real = b64d(u)
        now = time.time()
        if _SEG_OK_CACHE.get(real) and now - _SEG_OK_CACHE[real] < _SEG_OK_TTL:
            return True
        headers = dict((picked or {}).get("headers") or
                       {"User-Agent": "Mozilla/5.0"})
        headers["Range"] = _PROBE_RANGE
        cli = ext_client(request)
        req = cli.build_request("GET", real, headers=headers)
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                return True  # 读体失败不判死
            if not b or b[:1] in (b"<", b"{", b"["):
                return False
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
        _SEG_OK_CACHE[real] = now
        if len(_SEG_OK_CACHE) > 1500:
            for old in list(_SEG_OK_CACHE)[:700]:
                _SEG_OK_CACHE.pop(old, None)
        return True
    except Exception:
        return True  # 探测本身异常：保守放行，避免误伤好线


async def _find_playable(picked: dict, request: Request,
                         skip_current: bool = False) -> dict | None:
    """并行探测候选线路，返回（按优先级排序后的）首个可用目标（故障切换用）。"""
    cands = _ordered_candidates(picked)
    if skip_current:
        cands = [c for c in cands if c["line"] != picked.get("line")]
    if not cands:
        return None
    fresh = [c for c in cands if not _line_is_bad(c)] or cands
    fresh = fresh[:_PROBE_MAX_CANDIDATES]
    for i in range(0, len(fresh), 3):
        batch = fresh[i:i + 3]
        results = await asyncio.gather(
            *[_probe_target(c, request) for c in batch])
        for cand, ok in zip(batch, results):
            if ok:
                return cand
    return None


# 内置线路全死后，按片名全网回源找到的可播记录：原 media_guid -> 真实可播 og。
# 让播放器后续仍用原 mg 请求时能命中新记录（跨同名记录播放）。
_MEDIA_REDIRECT: dict[str, str] = {}
_RESURRECT_FAIL: dict[str, float] = {}
_RESURRECT_FAIL_TTL = 10 * 60
_RESURRECT_FUT: dict[str, asyncio.Future] = {}  # 规范化片名 -> 后台回源任务（单飞）


async def _http_head_ok(url: str, headers: dict) -> bool:
    """不依赖 request 的首片段验活：Range 取 2KB，4xx/错误页/空体判死。"""
    try:
        cli = app.state.ext
        req = cli.build_request(
            "GET", url, headers={**headers, "Range": _PROBE_RANGE})
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                pass
            return bool(b) and b[:1] not in (b"<", b"{", b"[")
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
    except Exception:
        return False


async def _resurrect_start(picked: dict, mg: str) -> str:
    """启动（或复用）按片名的后台回源任务。返回 'running' / 'ok' / 'fail'。"""
    title = (picked.get("title") or "").strip()
    nkey = re.sub(r"[\s\W_]+", "", title.lower())
    if not nkey:
        return "fail"
    if mg and mg in _MEDIA_REDIRECT:
        return "ok"
    if time.time() - _RESURRECT_FAIL.get(nkey, 0) < _RESURRECT_FAIL_TTL:
        return "fail"
    fut = _RESURRECT_FUT.get(nkey)
    if fut is None:
        fut = asyncio.ensure_future(_resurrect_worker(picked, mg, nkey))
        _RESURRECT_FUT[nkey] = fut

        def _drop(_f, k=nkey):
            _RESURRECT_FUT.pop(k, None)
        fut.add_done_callback(_drop)
    if fut.done() and not fut.cancelled():
        return "ok" if fut.result() else "fail"
    return "running"


async def _resurrect_worker(picked: dict, mg: str, nkey: str) -> bool:
    """后台按片名找真正能播的同名记录。两阶段：秒回直链源 → 加固源。"""
    title = (picked.get("title") or "").strip()
    want_idx = int(picked.get("index", 0) or 0)

    def match(b):
        return re.sub(r"[\s\W_]+", "", (b.get("title") or "").lower()) == nkey

    async def evaluate(briefs, direct_only):
        for b in briefs:
            if not match(b):
                continue
            og = b.get("guid")
            if not og:
                continue
            for ln, eps in (b.get("lines") or {}).items():
                if not eps:
                    continue
                ep = store.resolve_episode(og, ln, want_idx)
                if not ep or not ep.get("url"):
                    continue
                cand = dict(ep)
                if direct_only and not re.search(
                        r"\.(m3u8|mp4)(\?|$)", cand["url"], re.I):
                    continue
                # 解析到真实地址（加固源 detail+play），再验首片段
                try:
                    tgt = dict(cand, online_guid=og, kind="title")
                    await _resolve_if_needed(tgt, None)
                    if not tgt.get("url") or tgt.get("guard"):
                        continue
                    hdrs = tgt.get("headers") or {"User-Agent": "Mozilla/5.0"}
                    real = _self_url(tgt["url"])
                    is_hls = bool(tgt.get("m3u8")) or bool(
                        re.search(r"\.m3u8(\?|$)", real.split("?", 1)[0], re.I))
                    seg_url = real
                    if is_hls:
                        cur, seg_url = real, ""
                        for _d in range(3):  # 跟随嵌套 master 取首个分片
                            rr = await app.state.ext.get(
                                cur, headers=hdrs, timeout=15,
                                follow_redirects=True)
                            txt = rr.text
                            sl = [x.strip() for x in txt.splitlines()
                                  if x.strip() and not x.lstrip().startswith("#")]
                            if not sl:
                                break
                            if _is_media_playlist(txt):
                                seg_url = urllib.parse.urljoin(cur, sl[0])
                                break
                            cur = urllib.parse.urljoin(cur, sl[0])
                        if not seg_url:
                            continue
                    if not await _http_head_ok(seg_url, hdrs):
                        continue
                except Exception:
                    continue
                # 命中：记录可播 og，并补一条可解析线路到该 og（_pick_episode 用）
                if mg:
                    _MEDIA_REDIRECT[mg] = og
                _log.info("内置线路全死，全网回源《%s》-> %s", title, ln)
                return True
        return False

    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10, fast_only=True), timeout=20)
        if await evaluate(briefs, True):
            return True
    except Exception:
        pass
    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10), timeout=30)
        if await evaluate(briefs, False):
            return True
    except Exception:
        pass
    _RESURRECT_FAIL[nkey] = time.time()
    return False



# ===== 完全自动选最快线 =====
_LINE_FAST_TTL = 3600.0         # 本片最快线记忆 1 小时：重播/续播直接复用、免竞速；
                                # 期间该线若取流失败会被标记坏线，remembered 自动失效并换线
_LINE_FAST: dict[str, tuple[float, str]] = {}   # 集 key -> (ts, line)
# 固化 0.2.42 最优基准：竞速并发 4（与已验证最快版本一致）
_RACE_CONCURRENCY = 4


def _episode_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{int(picked.get("index", 0) or 0)}'


def _remember_fast(picked: dict, line: str, latency: float | None = None) -> None:
    if not line:
        return
    _LINE_FAST[_episode_key(picked)] = (time.time(), line)
    if len(_LINE_FAST) > 1000:
        for k in list(_LINE_FAST)[:500]:
            _LINE_FAST.pop(k, None)
    # 同步给本片预测缓存（海报预热 / 菜单置顶 / 角标共用）
    try:
        linepredict.remember(
            _episode_key(picked), line,
            float(latency) if latency is not None else 0.0)
    except Exception:
        pass


def remembered_fast_line(picked: dict) -> str | None:
    """该集上一次竞速/预热选出的最快线（未过期、且当前未被标记为坏线）。

    优先读 per-title 竞速记忆；没有时回落到海报预热的预测缓存 linepredict。"""
    key = _episode_key(picked)
    rec = _LINE_FAST.get(key)
    if rec:
        ts, line = rec
        if time.time() - ts >= _LINE_FAST_TTL:
            _LINE_FAST.pop(key, None)
        else:
            if _line_is_bad({**picked, "line": line}):
                _LINE_FAST.pop(key, None)
            else:
                return line
    # 海报预热已预测出本片最快线：直接采用（其自身有 30 分钟 TTL）
    pf = linepredict.fastest(key)
    if pf and not _line_is_bad({**picked, "line": pf}):
        _LINE_FAST[key] = (time.time(), pf)
        return pf
    return None


def _fastest_on() -> bool:
    try:
        return bool(config.get().get("line_fastest", True))
    except Exception:
        return True


def _predict_order_lines(cands: list[dict], picked: dict) -> list[dict]:
    """把【本片预热预测】出的最快线提到最前（稳定排序，其余保持原序）。

    不再使用全局源测速排名：源级速度代表不了本片分片 CDN。只有对这部片真实探测
    过的最快线（linepredict）才有置顶资格；未预热的影片不做任何猜测性重排。"""
    fast = linepredict.fastest(_episode_key(picked))
    if not fast:
        return cands
    return sorted(cands, key=lambda c: c.get("line") != fast)


def _race_candidates(picked: dict) -> list[dict]:
    """竞速候选：预测/默认线最前；其余名额优先【直链 m3u8】且【跨资源站取样】，
    坏线排除（除非全坏）。

    关键：不能只取清单前 n 条——实测同一片前 4~6 条可能恰好是此刻 ~2.4s 的慢/死线
    （优酷/暴风/非凡），而 10~500ms 的快线（量子/极速/新狼）排在第 7 条之后，旧逻辑
    根本不让它们进竞速，预算耗尽后回占位。跨源取样 + 直链优先保证快线一定有机会起跑。"""
    allc = _episode_candidates(picked)
    default = store.best_line(picked.get("online_guid", ""))
    fast = remembered_fast_line(picked)
    allc = [c for c in allc if not _line_is_bad(c)] or allc
    try:
        n = int(config.get().get("line_fastest_max", 8))
    except Exception:
        n = 8
    n = max(2, n)
    ordered = _predict_order_lines(allc, picked)

    def _is_direct(c: dict) -> bool:
        return bool(c.get("direct") or c.get("m3u8")) or bool(
            re.search(r"\.m3u8(\?|$)", str(c.get("url") or ""), re.I))

    def _prov(c: dict) -> str:
        return str(c.get("line", "")).split("::", 1)[0]

    def _src_dead(c: dict) -> bool:
        # 源级取流熔断：某资源站取流域名整片不可达时（搜索域可能仍正常），
        # 其所有线路在竞速里沉底，避免每部片都先撞长超时再换线（复刻 0.2.17 稳定性）。
        try:
            sk = sourcehealth.source_key_of(c.get("line"), c)
            return bool(sk) and sourcehealth.is_dead(sk)
        except Exception:
            return False

    def rank(c: dict) -> tuple:
        return (_src_dead(c),
                c["line"] != fast if fast else True,
                c["line"] != default if default else True)

    # 预测线 + 默认线强制保留在最前（但若其源已熔断则不强制置顶，交给死源沉底）
    pinned = sorted(
        [c for c in ordered if ((fast and c["line"] == fast)
          or (default and c["line"] == default)) and not _src_dead(c)],
        key=rank)
    pinned_keys = {id(c) for c in pinned}
    rest = [c for c in ordered if id(c) not in pinned_keys]
    # 剩余名额：健康源在前、直链优先、每源只取一条，尽量覆盖更多源；名额富余再补其余。
    # 关键：熔断源只沉底、绝不排除——健康候选不足 n 时用它们补足，保证竞速总有候选，
    # 避免“源被熔断→候选只剩 1 条且恰好是死链→起播直接失败”。
    picked_list = list(pinned)
    seen = {_prov(c) for c in picked_list}
    rest.sort(key=lambda c: (_src_dead(c), not _is_direct(c)))
    for prefer_direct in (True, False):
        for c in rest:
            if len(picked_list) >= n:
                break
            if id(c) in {id(x) for x in picked_list}:
                continue
            if prefer_direct and not _is_direct(c):
                continue
            prov = _prov(c)
            if prov in seen and prefer_direct:
                continue
            seen.add(prov)
            picked_list.append(c)
    return picked_list[:n]


async def _race_fastest(picked: dict, request: Request) -> dict | None:
    """并行竞速探测候选线路，选首字节延迟最低者（默认高质量线给小幅领先优势）。

    返回胜出的已解析目标（HLS 胜者会再构建播放列表缓存）；全部失败返回 None。"""
    cands = _race_candidates(picked)
    if not cands:
        return None
    default = store.best_line(picked.get("online_guid", ""))
    try:
        # 复刻 v0.2.17：单条探活预算 4s，gather 等全部候选跑完再选最快，不做“总预算
        # 提前截断”。0.2.17 实测秒播且稳——慢/死线各占自己的 4s，快线几十毫秒即返回，
        # 竞速结束后胜者【当场把播放清单建好】，main 紧接着命中缓存一次出真片。
        timeout = float(config.get().get("line_fastest_timeout", 4.0))
        prefer = float(config.get().get("line_fastest_prefer", 0.25))
    except Exception:
        timeout, prefer = 4.0, 0.25
    sem = asyncio.Semaphore(_RACE_CONCURRENCY)

    async def run(cand: dict):
        async with sem:
            # 复刻 v0.2.17：起播竞速只做轻量验活（抓清单 + 2KB 分片头），
            # 不下载 384KB 测吞吐——多候选在慢网下整段探测会把竞速拖到数秒，
            # 这正是新版起播比 0.2.17 慢的根因。吞吐惩罚仅用于海报预热阶段。
            lat = await asyncio.wait_for(
                _probe_latency(cand, request, build_hls=False, measure_seg=False),
                timeout=timeout)
        return cand, lat

    # gather 等全部候选（各自有 4s 上限），用 return_exceptions 吞掉慢/死线异常，
    # 不再到点 cancel——这正是 v0.2.17 “一次竞速选全、胜者当场建清单”的秒播关键。
    results = await asyncio.gather(*[run(c) for c in cands],
                                   return_exceptions=True)
    ok = [r for r in results
          if isinstance(r, tuple) and r[1] is not None]
    for c, _ in ok:
        _LINE_BAD.pop(_line_bad_key(c), None)  # 探活成功，清掉可能的旧坏线标记
    if not ok:
        for c in cands:
            _mark_line_bad(c)
        return None

    def eff(item):
        c, lat = item
        return lat - (prefer if default and c["line"] == default else 0.0)

    winner, wlat = min(ok, key=eff)
    _remember_fast(picked, winner["line"], wlat)
    # 竞速阶段已对所有成功候选做过 Range 2KB 分片头验活（轻量探测），登记到请求级
    # 标记，后续 _verify_rewritten_first_seg 对这些线直接放行，避免重复一次串行 Range
    # 往返（复刻 0.2.17：验活只做一次，不在建清单后再探一遍）。
    try:
        verified = getattr(request.state, "_race_verified", None)
        if verified is None:
            verified = set()
            request.state._race_verified = verified
        for c, _lat in ok:
            if c.get("line"):
                verified.add(c["line"])
    except Exception:
        pass
    # 胜者若是 HLS，构建改写后的播放列表供 main 直接取用；标记为冠军清单，
    # 与海报预建一样享 15min stale 保鲜（下次点同片即使过 120s 也秒回旧清单）。
    if re.search(r"\.m3u8(\?|$)", winner.get("url", "").split("?", 1)[0], re.I):
        mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
        if mg:
            await _get_playlist_cached(mg, winner, request, champion=True)
    _log.info("线路竞速: 《%s》%s 选中 %s (%.2fs, %d 候选)",
              picked.get("title"), picked.get("ep_title") or "",
              winner.get("line"), wlat, len(ok))
    return winner


# ===== 海报阶段本片线路轻量预热（linepredict）=====
# 注意：海报探测必须用【独立】信号量，绝不能与播放清单预热(_prewarm_sem)共用——
# 海报预热每部片会长时间(≤3.6s)占许可，翻页时几十部在途会把播放预热饿死，
# 导致 iOS 起播前串行抓到的各线路清单全是冷的、各撞 ~2.4s 预算回占位（不秒播）。
_POSTER_PROBE_SEM: asyncio.Semaphore | None = None


def _poster_probe_sem() -> asyncio.Semaphore:
    global _POSTER_PROBE_SEM
    if _POSTER_PROBE_SEM is None:
        _POSTER_PROBE_SEM = asyncio.Semaphore(3)   # 同时最多 3 部片在探测，避免压源站
    return _POSTER_PROBE_SEM


class _ProbeReq:
    """无用户请求时的轻量垫片：probe/fetch 只用 request.app.state 上的客户端。"""
    class _State:
        pass

    def __init__(self, mg: str = ""):
        self.app = type("A", (), {"state": app.state})()
        self.state = type("S", (), {"_play_mg": mg})()


def schedule_prewarm_title(og: str, index: int = 0) -> None:
    """v0.2.42 按需模式：不为海报/搜索列表后台预热线路。"""
    return None


def warm_catalog_briefs(briefs: list) -> None:
    """海量(catalog)库首屏：把目录片并入 store，随后与热门库同等对待——后台按片名
    全网补全 CMS 线路 + 本片最快线预热并预建冠军清单，用户点进即秒播。

    catalog 片渲染时只活在目录窗口里、尚未进 store，直接调 schedule_prewarm_title 会
    因 get_title_by_guid 为空而静默跳过（实测海量库首屏零预热、每次点击冷竞速 2s、
    前几条慢线占满预算时还会误回占位）。这里先 remember_merged 落库再预热。"""
    try:
        if not briefs or not bool(config.get().get("linepredict_enabled", True)):
            return
        # 批量大小由调用方控制（首屏渲染/后台全量预热各自分批），这里不再硬截断
        items = []
        for b in briefs:
            g = b.get("guid")
            if not g or store.get_title_by_guid(g):
                if g:
                    schedule_prewarm_title(g, 0)
                continue
            items.append(b)
        if items:
            try:
                store.remember_merged(items)
            except Exception:
                pass
        for b in (items or briefs):
            g = b.get("guid")
            if not g:
                continue
            try:
                # 浏览期批量补线默认关闭（避免首屏 18 部片排队跨源搜索拖慢二级菜单）；
                # 起播路径仍会单独补当前影片，线路菜单完整性不受影响。
                if bool(config.get().get("browse_fill_lines_enabled", False)):
                    start_fill_lines(g)        # 后台补齐全网同名 CMS 线路
            except Exception:
                pass
            schedule_prewarm_title(g, 0)   # 用目录片既有线路先预热，补全后再自然更新
    except Exception:
        pass


async def startup_prewarm_hot() -> None:
    """重启后的轻量启动任务（按需模式，不做全量预热）。

    历史教训：全量后台预热（逐片 resolve / 无界翻页拉取）会长期抢占事件循环与
    HTTP 连接池，把海报回源从亚秒拖到几十秒、并引发媒体库加载慢/网络错误。
    现仅在启动后刷新【海量库内存路由】（轻量、必要，决定海量库能否出卡片）；
    所有影片级的探活/建清单一律改为按需：用户浏览到哪、schedule_prewarm_title
    预热到哪，点击时现场竞速，不产生常驻后台重任务。
    """
    try:
        await asyncio.sleep(8)
        if not bool(config.get().get("linepredict_enabled", True)):
            return
        try:
            if catalog.routes_stale():
                await asyncio.wait_for(
                    catalog.refresh_routes(config.enabled_sources()), timeout=90)
                _log.info("startup catalog routes refreshed (on-demand mode)")
            # 预热海量库各分类首页到 dense 缓存：TV/盒子/安卓启动会并发拉取多个媒体库，
            # 未预热时每个 catalog 首页（客户端首屏 page_size=72，约 4 个 CMS 分页）都要
            # 现场跨源深翻（实测 0.9~3.9s），与二级目录冷片搜索争抢事件循环/连接池，把
            # folder 搜索从亚秒拖到 1.5s+ 触发安卓占位。预热 80 条覆盖首屏，库根命中内存。
            async def _warm_one(cat):
                try:
                    if catalog.available(cat):
                        await catalog.get_window(cat, 0, 80)
                except Exception:
                    pass

            try:
                await asyncio.wait_for(
                    asyncio.gather(*[_warm_one(c)
                                     for c, _t, _ in catalogmod.CATALOG_CATEGORIES]),
                    timeout=30)
                _log.info("startup catalog home prewarmed")
            except Exception:
                _log.info("startup catalog home prewarm partial", exc_info=True)
            # 预热 standalone 独立源库首屏窗口：这些库根首次打开要跨四类深翻
            # （1~5s），与二级目录冷片搜索争抢连接池。预热后命中短时缓存，秒回。
            async def _warm_standalone(sid):
                try:
                    if catalog.standalone_available(sid):
                        await catalog.standalone_window(sid, 0, 72)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[_warm_standalone(s["id"])
                                     for s in standalone_sources_active()]),
                    timeout=30)
                _log.info("startup standalone home prewarmed")
            except Exception:
                _log.info("startup standalone prewarm partial", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info("startup catalog routes warm skipped", exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.info("startup prewarm skipped", exc_info=True)


async def _prewarm_title_task(og: str, index: int, ekey: str) -> None:
    try:
        try:
            n = max(1, int(config.get().get("linepredict_max_lines", 2) or 2))
        except Exception:
            n = 2
        # 构造一个 picked 壳以复用 target_lines / 候选逻辑
        picked = {"online_guid": og, "index": int(index or 0),
                  "title": (store.get_title_by_guid(og) or {}).get("title", "")}
        targets = store.target_lines(picked)
        # 只预热普通廉价 CMS 直链：排除网盘手动线/加固/爬虫/drpy/小雅/需按需解析的线
        def _cheap(t: dict) -> bool:
            if t.get("_pansou_manual") or t.get("line") == PANSOU_LINE:
                return False
            if t.get("xsrc") or t.get("guard") or t.get("drpy") or t.get("catvod") \
                    or t.get("spider_src") or t.get("resolve") or t.get("redirect"):
                return False
            return bool(t.get("m3u8")) or bool(
                re.search(r"\.m3u8(\?|$)", str(t.get("url") or ""), re.I))
        cheap = [t for t in targets if _cheap(t)]
        # 跨源取候选：每个资源站只取它的第一条廉价线，避免同一源的两条线占满探测名额、
        # 漏掉排序靠后但对本片更快的源（实测极速线排第 10，取前 3 条会完全漏探）。
        # 预算内尽量多覆盖不同源（上限 max_lines+2），仍只打廉价 m3u8、总量很小。
        seen_prov: set[str] = set()
        cands = []
        for t in cheap:
            prov = str(t.get("line", "")).split("::", 1)[0]
            if prov in seen_prov:
                continue
            seen_prov.add(prov)
            cands.append(t)
            if len(cands) >= max(n, 5):
                break
        if not cands:
            return
        # 探速阶段不需要落播放清单缓存；选出冠军后再用带 mg 的垫片把冠军清单建好
        req = _ProbeReq("")
        best = None
        async with _poster_probe_sem():
            async def one(c):
                try:
                    # 后台预热不挡首屏，给比播放竞速更宽的预算以提高命中率
                    lat = await asyncio.wait_for(
                        _probe_latency(c, req, build_hls=False,
                                       measure_seg=True), timeout=2.4)
                    return c, lat
                except Exception:
                    return c, None
            res = await asyncio.gather(*[one(c) for c in cands])
        # 回写源级健康画像：预热阶段的探测成败累积进 sourcehealth，取流整站不可达
        # （搜索域正常但播放 CDN 挂）时能在起播前就熔断、零成本跳过，复刻 0.2.17
        # “快源优先、不被死源拖尾”的稳定性。
        for c, lat in res:
            try:
                sk = sourcehealth.source_key_of(c.get("line"), c)
                if sk:
                    sourcehealth.record(sk, lat is not None, lat or 0.0)
            except Exception:
                pass
        for c, lat in res:
            if lat is not None and (best is None or lat < best[1]):
                best = (c, lat)
        # 只把“够快”的候选记为冠军：否则唯一可探源很慢（实测有 25s）时也会被钉死
        # 成首选，点击即卡/失败。慢于阈值不记预测，让起播时现场竞速在多条线里挑快的。
        if best is not None and best[1] < linepredict.SLOW_SEC:
            linepredict.remember(ekey, best[0]["line"], best[1])
            # 预建冠军线播放清单：点击时 main 直接命中缓存，零上游抓取 = 真秒播。
            # 失败静默（点击竞速仍会兜底）。用独立短预算，避免占住海报探测信号量。
            try:
                build_req = _ProbeReq(og)
                bc = dict(best[0])
                bc["online_guid"] = og
                bc["index"] = int(index or 0)
                await asyncio.wait_for(
                    _get_playlist_cached(og, bc, build_req, champion=True),
                    timeout=5.0)
            except Exception:
                pass
            _log.debug("海报预热: 《%s》选中 %s (%.2fs, %d 候选)",
                       picked.get("title"), best[0].get("line"), best[1], len(cands))
        else:
            _log.debug("海报预热: 《%s》未得到可用线 (%d 候选)",
                       picked.get("title"), len(cands))
    except Exception as e:
        _log.debug("海报预热异常 %s: %r", og, e)
    finally:
        linepredict.clear_inflight(ekey)


async def _try_guard_fallback(picked: dict):
    """块外安全包装：完整版把加固源作为在线播放兜底线路；lite 包剥离了该实现，
    globals 里不存在时返回 None（绝不 NameError）。"""
    fn = globals().get("_guard_playback_fallback")
    if fn is None:
        return None
    try:
        return await fn(picked)
    except Exception:  # noqa: BLE001
        return None


async def _ensure_playable(picked: dict, request: Request,
                           auto: bool = False) -> dict:
    """取流前确保目标可播。

    - auto=True（播放器自动起播，非用户手动切线）且开启最快线：并行竞速选最快线。
    - 否则（用户手动选了某条线）：先探当前线，失败才记忆坏线并故障切换到首个可用线。
    - 以上常规(CMS)线路都拿不到时，若开启「加固源兜底」，用片名在加固源元搜索救活。
    返回应播放的 picked（可能已换成另一线路）。"""
    if not picked or not picked.get("online_guid"):
        return picked
    if auto and _autofail_on() and _fastest_on():
        # ① 该片预热/竞速记忆的最快线：直接复用、零测速（冠军清单已在海报阶段预建）。
        #    即使是慢档也直放——它已是这片当时实测最快的线，且该线首个分片若是慢 CDN，
        #    点击时再跑 2s 竞速只会让用户白等（实测慢档线首片常很快，慢在预热瞬时）。
        #    慢档预测会在下次浏览时后台重探纠正；真正播不动由下面故障切换兜底。
        rf = remembered_fast_line(picked)
        if rf:
            reuse = next((c for c in _episode_candidates(picked)
                          if c.get("line") == rf and not _line_is_bad(c)), None)
            if reuse is not None:
                return reuse
        # ② 首次播放该片：仍做一次本片多线竞速（2s 预算），但候选顺序按全局测速排名
        #    优化——不能无条件直接用全局最快源：源测速用固定样例，代表不了每部片各自的
        #    分片 CDN（实测全局最快的量子在《黑球》首片 2.6s，而暴风只要 0.27s）。
        winner = await _race_fastest(picked, request)
        if winner is not None:
            return winner
        # 竞速全失败：退回常规故障切换（探当前线/换线），其后仍失败再加固兜底
    if not _autofail_on():
        await _resolve_if_needed(picked, request)
        if await _probe_target(picked, request):
            return picked
        return await _try_guard_fallback(picked) or picked
    cur_bad = _line_is_bad(picked)
    if not cur_bad:
        if await _probe_target(picked, request):
            return picked
        _mark_line_bad(picked)
        _log.warning("线路不可用，自动换线: %s 《%s》%s",
                     picked.get("line"), picked.get("title"),
                     picked.get("ep_title"))
    good = await _find_playable(picked, request, skip_current=True)
    if good is not None:
        _log.info("自动切换到可用线路: %s -> %s", picked.get("line"), good.get("line"))
        return good
    # 所有普通线路都失败：加固源作为最后一条播放线路兜底（仅 full、运行时就绪）
    return await _try_guard_fallback(picked) or picked


async def _resolve_if_needed(picked: dict, request: Request) -> dict:
    """把播放页(/share/<id>)或 302 跳转地址解析成真实媒体地址（原地更新）。"""
    if picked and picked.get("redirect"):
        real = await providers.resolve_redirect(
            src_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["redirect"] = False
            picked["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            src_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["resolve"] = False
    return picked


# ===== 起播速度优化：播放列表短时缓存/预热 =====
# 说明：分辨率（清晰度）探测模块已整体搁置（ffprobe/额外分片占用资源、慢源下不稳定），
# preset 线路菜单只显示线路名、不再追加分辨率；quality.py 仅保留 PNG 伪装分片解包用途。
# 后续有更优的识别方案再行接入。


# 改写后的媒体播放列表短时内存缓存，避免同一集起播/重试反复抓上游。
# 上游地址带时效签名，TTL 取短；只缓存成功结果。
_PLAYLIST_CACHE: dict[str, tuple[float, str]] = {}
_PLAYLIST_TTL = 120.0
# 冠军清单（海报阶段为本片最快线预建）的键集合：享受更长的“保鲜宽限期”。线路预测
# 有效 30 分钟，普通清单 120s 就过期会导致用户浏览后几分钟再点仍冷抓占位；冠军清单在
# 宽限期内即便过期也先回旧清单秒播、后台静默重建（stale-while-revalidate），分片真死
# 仍由首片验活+自动换线兜底。普通清单不受影响。
_CHAMPION_KEYS: set[str] = set()
_PLAYLIST_STALE = 15 * 60.0

# 单飞：同一「集+线路」只允许一次在途抓取，预热与真正取 main 共用同一个 Future
_PLAYLIST_FUT: dict[str, asyncio.Future] = {}
# main 整条“解析+换线+建清单”的在途服务任务（mg|line|index -> Future）：慢源首个
# 请求只等到预算，超时回占位但后台任务继续跑完并写播放列表缓存，后续轮询直接命中。
_MAIN_SERVE_FUT: dict[str, asyncio.Future] = {}
# 仅【慢源】（加固/爬虫/drpy/专有解码/小雅，常需十几秒解析）才用短预算先占位；普通
# CMS 直链不用预算、直接等出正片（恢复秒播），只在真正抓不到时才回占位。
try:
    _MAIN_SLOW_BUDGET = float(config.get().get("main_ready_budget", 2.4))
except Exception:
    _MAIN_SLOW_BUDGET = 2.4
_MAIN_SLOW_BUDGET = min(max(_MAIN_SLOW_BUDGET, 0.8), 2.8)


def _is_slow_line(p: dict) -> bool:
    """是否为解析很慢的非标准源（加固/爬虫/drpy/专有解码/小雅），需先占位后台自愈。
    普通 CMS 直链（暴风/量子等 m3u8）返回 False，走直接秒播。"""
    if not isinstance(p, dict):
        return False
    if p.get("guard") or p.get("spider_src") or p.get("drpy") \
            or p.get("catvod"):
        return True
    if p.get("xsrc"):
        return True
    ln = str(p.get("line") or "")
    return ln.startswith(("guard-", "spider-", "drpy-"))

# 紧凑分片注册表：夸克/UC 转码 HLS 的签名分片 URL 极长（~600 字符，base64 后近千），
# 一部 90 分钟的片子上千分片会撑出 1MB+ 的清单，且每个分片 URL 超长，经外网网关
# （Lucky/nginx/fngateway）容易被长度/缓冲限制拦掉，播放器拿到清单却拉不到分片。
# 故把「清单ID + 序号 → 真实分片URL」留在服务端，清单里只给短 URL（~60 字符）。
_HLS_REG: dict[str, tuple[float, list[str]]] = {}
_HLS_REG_TTL = 3 * 3600.0
_HLS_LONG_URL = 320          # 分片 URL 超过该长度即走紧凑注册表
_HLS_REG_MAX = 4000


def _hls_reg_put(urls: list[str]) -> str:
    import secrets
    rid = secrets.token_hex(6)
    _HLS_REG[rid] = (time.time(), urls)
    if len(_HLS_REG) > _HLS_REG_MAX:
        for old in sorted(_HLS_REG, key=lambda k: _HLS_REG[k][0])[:len(_HLS_REG) - _HLS_REG_MAX]:
            _HLS_REG.pop(old, None)
    return rid


def _hls_reg_get(rid: str, i: int) -> str:
    ent = _HLS_REG.get(rid)
    if not ent:
        return ""
    ts, urls = ent
    if time.time() - ts > _HLS_REG_TTL:
        _HLS_REG.pop(rid, None)
        return ""
    return urls[i] if 0 <= i < len(urls) else ""

# 哨兵：该线路解析后实为 mp4 直链（非 HLS），调用方应 302 到 direct
_NOT_HLS = object()


def _playlist_cache_key(picked: dict) -> str:
    return f'{picked.get("online_guid","")}|{picked.get("line","")}|{int(picked.get("index",0) or 0)}'


async def _build_playlist_body(mg: str, picked: dict, request: Request):
    """抓取该线路的上游媒体列表，并把分片改写为经本代理的绝对地址。
    返回 (body_text, base_url)；失败返回 (None, None)。"""
    headers = picked["headers"]
    was_redirect = bool(picked.get("redirect"))
    await _resolve_if_needed(picked, request)
    src_url = _self_url(picked["url"])
    text = cur = None

    # 普通在线线路（公开版唯一路径）：解析后若其实是 mp4 直链（wrapper 预判为 HLS 时
    # 的兜底），交由调用方 302；否则抓取上游媒体列表。
    if not re.search(r"\.m3u8(\?|$)", src_url, re.I) \
            and re.search(r"\.mp4(\?|$)", src_url, re.I):
        return _NOT_HLS, None
    # 网盘转码档已在上面的 pan 分支抓好绝对化清单，勿再回环抓取覆盖
    if text is None:
        text, cur = await _fetch_playlist(request, src_url, headers)
    # 跳转型源站极慢且间歇抖动：解析后的真实地址抓取失败时，对原始跳转地址
    # 强制重新解析一次再抓，避免整列表 502。
    if text is None and was_redirect:
        from store import store as _store
        og = picked.get("online_guid")
        raw = _store.get_title_by_guid(og) if og else None
        jump = None
        if raw:
            for _ln, eps in (raw.get("lines") or {}).items():
                for e in eps:
                    if e.get("redirect"):
                        jump = e.get("url")
                        break
                if jump:
                    break
        if jump:
            fresh = await providers.resolve_redirect(
                src_client(request), jump, (headers or {}).get("Referer", ""))
            if fresh:
                text, cur = await _fetch_playlist(request, fresh, headers)
                if text is not None:
                    picked["url"] = fresh
    if text is None:
        return None, None

    # v0.2.42：清单去广告只跑本地规则，不在起播关键路径读取分片 SPS 指纹。
    try:
        import adfilter as _adfilter
        if _adfilter.enabled():
            text, _ad_removed = _adfilter.filter_hls(text, cur or src_url)
    except Exception:
        _log.info("hls adfilter failed", exc_info=True)

    sel_qs = ""
    if picked.get("line"):
        sel_qs = f'line={urllib.parse.quote(picked["line"])}&index={picked["index"]}'

    # AES-128 加密 HLS：部分移动/TV 原生播放器拉到密钥后无法正确解密（报 http error）。
    # 在服务端取一次密钥，改写每个分片的 IV/密钥参数，由 /seg 统一解密后回传纯 TS，
    # 并从输出列表删除 EXT-X-KEY，彻底规避端侧 AES 兼容差异。
    decrypt_key: bytes | None = None
    explicit_iv: bytes | None = None
    key_marker = next((ln for ln in text.splitlines()
                       if ln.strip().startswith("#EXT-X-KEY")
                       and "METHOD=AES-128" in ln), "")
    if key_marker:
        try:
            import quality as _quality
            parsed = await _quality._parse_hls_key(
                src_client(request), text, cur, headers)
            if parsed:
                decrypt_key, explicit_iv = parsed
        except Exception:
            decrypt_key = None

    media_seq = 0
    mm = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", text)
    if mm:
        media_seq = int(mm.group(1))
    seg_index = 0
    out_lines = []

    # 先收集所有分片绝对地址（与下方输出顺序一致：URI 标签按出现序，分片行按出现序）
    seg_urls: list[str] = []
    for raw0 in text.splitlines():
        ln0 = raw0.strip()
        if ln0.startswith("#"):
            if ln0.startswith("#EXT-X-MAP") or ln0.startswith("#EXT-X-KEY"):
                m0 = re.search(r'URI="([^"]+)"', ln0, re.I)
                if m0:
                    seg_urls.append(urllib.parse.urljoin(cur, m0.group(1)))
        elif ln0:
            seg_urls.append(urllib.parse.urljoin(cur, ln0))
    # 任一地址过长即整表走紧凑注册表（同表统一，避免混用）
    use_compact = any(len(u) > _HLS_LONG_URL for u in seg_urls)
    compact_rid = _hls_reg_put(seg_urls) if use_compact else ""

    # 混合直连：仅普通 CMS 的无加密、无广告、非紧凑 HLS 才考虑让播放端直连源站分片；
    # AES 解密(decrypt_key)/去广告命中/紧凑表/网盘/解析/加固/T4 等特殊线一律保持中转。
    direct_segments = False
    direct_segment_urls: list[str] = []
    if _direct_play_on() and not use_compact and decrypt_key is None \
            and not _ad_removed and seg_urls and not any(
                picked.get(k) for k in ("pan", "xsrc", "catvod",
                                        "guard", "drpy", "t4")):
        try:
            if await _direct_eligible(src_client(request), seg_urls[0]):
                direct_segments = True
                direct_segment_urls = seg_urls
                _log.info("直连生效 media=%s line=%s host=%s",
                          mg, picked.get("line", ""),
                          urllib.parse.urlsplit(seg_urls[0]).netloc)
        except Exception:
            direct_segments = False

    def seg_decrypt_qs() -> str:
        iv = explicit_iv if explicit_iv is not None else (
            (media_seq + seg_index).to_bytes(16, "big"))
        return f"dec=1&k={b64e(decrypt_key.hex())}&iv={b64e(iv.hex())}"

    compact_i = 0
    # 直连模式下按出现顺序消费已绝对化的源站地址（与 seg_urls 收集顺序一致）。
    direct_iter = iter(direct_segment_urls) if direct_segments else None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY") and decrypt_key is not None:
            # 服务端已解密：客户端无需也不能再拿到密钥标签
            continue
        if line.startswith("#EXT-X-MAP") or \
                (line.startswith("#EXT-X-KEY") and 'URI="' in line):
            def repl(m):
                nonlocal compact_i
                uri = m.group(1)
                if direct_iter is not None:
                    src_abs = next(direct_iter, None)
                    if src_abs:
                        return f'URI="{src_abs}"'
                if compact_rid:
                    ref = f"hls?r={compact_rid}&i={compact_i}"
                else:
                    ref = _seg_url(mg, urllib.parse.urljoin(cur, uri), sel_qs)
                compact_i += 1
                return f'URI="{ref}"'
            raw = re.sub(r'URI="([^"]+)"', repl, line, flags=re.I)
            out_lines.append(raw)
        elif line.startswith("#") or not line:
            out_lines.append(raw)
        else:
            if direct_iter is not None:
                src_abs = next(direct_iter, None)
                if src_abs:
                    out_lines.append(src_abs)
                    seg_index += 1
                    continue
            if compact_rid:
                out_lines.append(
                    f"hls?r={compact_rid}&i={compact_i}")
                compact_i += 1
            else:
                if decrypt_key is not None:
                    parts = [p for p in (sel_qs, seg_decrypt_qs()) if p]
                    extra = "&".join(parts)
                else:
                    extra = sel_qs
                out_lines.append(_seg_url(mg, urllib.parse.urljoin(cur, line), extra))
            seg_index += 1
    return "\n".join(out_lines) + "\n", cur


async def _get_playlist_cached(mg: str, picked: dict, request: Request,
                               *, champion: bool = False):
    """取改写后的播放列表（内存缓存 + 单飞）。返回文本或 None。
    若该集/线路已有在途抓取（点击时预热启动），直接等同一个结果，不重复请求上游。

    champion=True：这是海报阶段预建的本片冠军清单。登记进 _CHAMPION_KEYS 享长保鲜：
    超过普通 TTL 但仍在 _PLAYLIST_STALE 宽限期内时，立即返回旧清单并后台静默重建
    （stale-while-revalidate），保证用户在 30 分钟预测有效期内点开都秒播。"""
    key = _playlist_cache_key(picked)
    if champion:
        _CHAMPION_KEYS.add(key)
    now = time.time()
    hit = _PLAYLIST_CACHE.get(key)
    if hit:
        age = now - hit[0]
        if age < _PLAYLIST_TTL:
            return hit[1]
        # 冠军清单过期但仍在宽限期：回旧清单秒播，后台静默重建（单飞、非阻塞）
        if key in _CHAMPION_KEYS and age < _PLAYLIST_STALE and key not in _PLAYLIST_REBUILD:
            _refresh_playlist(key, mg, picked, request)
            if hit[1] and _playlist_has_segment(hit[1]):
                return hit[1]
    fut = _PLAYLIST_FUT.get(key)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _PLAYLIST_FUT[key] = fut

        async def produce():
            try:
                body, _base = await _build_playlist_body(mg, picked, request)
                if body is not None and body is not _NOT_HLS:
                    _PLAYLIST_CACHE[key] = (time.time(), body)
                    if champion:
                        _CHAMPION_KEYS.add(key)
                    if len(_PLAYLIST_CACHE) > 600:
                        # 淘汰最旧条目，但保护冠军清单（它们支撑首屏秒播）
                        for k in list(_PLAYLIST_CACHE):
                            if k not in _CHAMPION_KEYS:
                                _PLAYLIST_CACHE.pop(k, None)
                                _CHAMPION_KEYS.discard(k)
                            if len(_PLAYLIST_CACHE) <= 450:
                                break
                if not fut.done():
                    fut.set_result(body)
            except Exception as exc:
                if not fut.done():
                    fut.set_result(None)
                _log.info("playlist build failed: %s", exc)
            finally:
                _PLAYLIST_FUT.pop(key, None)

        asyncio.create_task(produce())
    return await fut


# 冠军清单过期后的后台重建任务（单飞，与首请求路径的 _PLAYLIST_FUT 分开）
_PLAYLIST_REBUILD: dict[str, asyncio.Task] = {}


def _refresh_playlist(key: str, mg: str, picked: dict, request: Request) -> None:
    if key in _PLAYLIST_REBUILD:
        return
    async def _do():
        try:
            body, _base = await _build_playlist_body(mg, picked, request)
            if body is not None and body is not _NOT_HLS \
                    and _playlist_has_segment(body):
                _PLAYLIST_CACHE[key] = (time.time(), body)
        except Exception:
            pass
        finally:
            _PLAYLIST_REBUILD.pop(key, None)
    try:
        _PLAYLIST_REBUILD[key] = asyncio.create_task(_do())
    except RuntimeError:
        pass


def _warm_playlist(mg: str, picked: dict, request: Request) -> None:
    """点击播放时后台预热默认线路播放列表（单飞，真正取 main 时并入同一任务）。"""
    if picked.get("t4"):
        return
    key = _playlist_cache_key(picked)
    hit = _PLAYLIST_CACHE.get(key)
    if hit and time.time() - hit[0] < _PLAYLIST_TTL:
        return
    if key in _PLAYLIST_FUT:
        return  # 已在途
    asyncio.create_task(_get_playlist_cached(mg, picked, request))


# preset 菜单预热线：飞牛播放器拿到多线路 master 后，会在【起播前】对每条变体串行预取
# 一次 main（冷清单每条要真打上游 150~300ms，20 条串行累计 4~25s，表现为“起播慢”，
# 实测 play/play 到首个分片被拖到 4.7s）。preset 返回前并发后台预热所有普通 CMS 线
# （单飞、有缓存即跳过），播放器随后逐条预取 main 时直接命中内存缓存，把串行等待摊平
# 成一次并发耗时。【起播线（第一个变体）不限并发、最先预热】，保证它在播放器第一次取
# main 前尽量就绪；其余菜单变体限并发预热，避免一次压垮源站。网盘/加固/爬虫/小雅等需
# 按需解析的贵线不预热（只有用户手切才解析）。
_PREWARM_MAX = 24
_PREWARM_SEM: "asyncio.Semaphore | None" = None


def _prewarm_sem() -> "asyncio.Semaphore":
    global _PREWARM_SEM
    if _PREWARM_SEM is None:
        # App/Flutter 会串行预取 master 里每条变体的 main，一部片可达 ~18 条线；
        # 并发过小会让排后面的慢线在播放器拉到时还没建完（实测 6 并发时末位慢线
        # 仍冷抓 2s+）。清单抓取本身很轻，放宽到 12 让全线路在 App 跳转窗口内建热。
        _PREWARM_SEM = asyncio.Semaphore(12)
    return _PREWARM_SEM


def _warm_preset_lines(mg: str, targets: list[dict], request: Request) -> None:
    """v0.2.42 按需模式：不在后台为所有线路预建清单。"""
    return None


@app.get(f"{ONLINE_PREFIX}/{{mg}}/preset.m3u8")
async def hls_preset(mg: str, request: Request):
    _pl = request.query_params.get("line")
    idx0 = int(request.query_params.get("index", 0) or 0)
    # 用户手切网盘线的意图必须【最先】登记：影片此刻可能尚未绑定，下面
    # _pick_episode 为空会直接回占位 master 提前返回；若标记放在那之后，首次进
    # 播放页就会漏登记，导致 main 误判“非手切”回落 CMS、网盘线永远 arm 不起来。
    if _pl == PANSOU_LINE:
        _pansou_mark_user_pick(mg)
    # direct/mp4 取流失败回跳入口：直接给占位 master，不再枚举可能同样失效的线路
    if request.query_params.get("ph") == "1":
        return _placeholder_master_response(idx0)
    # 榜单片可能尚未绑定：确保后台绑定任务已启动，但 preset【绝不长时间等待】
    #（搜索最坏 25s，会先撞上客户端~3s 首字节超时，导致连占位都拿不到）。仅极短
    # 等一下，未就绪立刻回占位 master，由 main(phwait) 继续等待并自动切正片。
    _cg = chart_guid_by_media(mg)
    if _cg and not store.resolve_media_guid(mg):
        start_resolve_chart(_cg)
    picked = _pick_episode(mg, request)
    if not picked:
        return _placeholder_master_response(idx0)
    # 起播即跳过已知坏线（用户显式选线除外）
    picked = _dodge_known_bad(picked, request)
    # 注意：此处不 await _resolve_if_needed —— preset 只输出线路变体菜单，
    # 播放页/跳转/专有解码延迟到该线路被选中取 main 时再做，避免拖慢首屏。
    # play/play 选定线路后会带 line/index；此时只输出该线路，避免与清晰度菜单重复
    explicit_line = request.query_params.get("line")
    # 普通库片（非榜单、非手动选线）：后台按片名补齐全网同名 CMS 线路，使线路菜单
    # 与热门库一样完整、名称规范。非阻塞，本次先用既有线路秒播，下次取菜单即完整。
    if not explicit_line:
        try:
            _og = picked.get("online_guid") or store.online_guid_by_media(mg)
            if _og:
                start_fill_lines(_og)
        except Exception:
            pass
    # 用户手动指定 PanSou 高清线：只输出这一条（真正搜索+转存延迟到拉占位分片时）。
    if explicit_line == PANSOU_LINE:
        # 意图已在函数入口（picked 早退之前）登记；此处直接输出网盘单变体。
        targets = [_pansou_manual_target(
            int(request.query_params.get("index", 0) or 0))]
    elif explicit_line:
        # _pick_episode 已按 line/index 解析出该线路完整 target（含
        # redirect/resolve/xsrc 标志）。App/Flutter 起播的 302 总会带显式 line，
        # 若只回这一条，播放器线路菜单就只有当前线、也没有网盘高清线。因此枚举该
        # 片全部 CMS 线路（选中线置顶），与不带 line 时一致；真正解析仍推迟到取
        # 该线 main 时。
        all_t = [t for t in store.target_lines(picked)
                 if t.get("line") != explicit_line]
        targets = [picked] + all_t
    else:
        targets = store.target_lines(picked)
    # 菜单末尾【全局】追加一条“网盘高清（PanSou）”手动线（完整版且启用时）。
    # 任何起播入口（App/Flutter 带显式 line、网页不带 line）都要加，保证所有线路菜单
    # 都能看到它。它不进全局搜索、不参与测速、不自动触发，只有用户手动切到它才转存。
    if not any(t.get("_pansou_manual") for t in targets) and _pansou_available():
        targets.append(_pansou_manual_target(int(picked.get("index", 0) or 0)))
    # 起播速度优先：preset 只枚举候选线路，不在关键路径解析播放页/302/专有解码，
    # 那些动作延迟到该线路真正被选中取 main 时才做。
    hls_targets = []
    # 任何可按需解析的专有直链（小雅网盘 / JS / 加固等）都要进菜单，死活在 main 探测
    _SPECIAL = ("catv" + "od", "dr" + "py", "gu" + "ard")

    def _need_resolve(t: dict) -> bool:
        return bool(t.get("xsrc")) or any(t.get(k) for k in _SPECIAL)

    # 显式指定线路（App/Flutter 经 stream 302 起播）：master 只输出【该选中线】单变体
    # （网盘手切线另加），复刻 v0.2.17。否则 iOS 原生播放器会按 master 里的变体顺序
    # 串行预取每一条 main.m3u8，慢线(2.5~6s/条)累计成“加载半天”。App 的线路切换菜单
    # 来自 stream/play 的 qualities，用户切线时会带新 line 重新请求，不依赖这里多变体；
    # 其余线路仍由下方 _warm_preset_lines 在后台并发建热，切线时命中缓存。
    _explicit_all = targets
    # 自动起播标记：stream 302 / play_link 会带 auto=1（即使同时带默认 line）。
    _auto_start = request.query_params.get("auto") == "1"
    if explicit_line and explicit_line != PANSOU_LINE:
        if _auto_start:
            # 四端统一“起播瞬间走本片最快线”：单变体优先选海报预热/竞速冠军线
            # （已预建清单→秒播）；无冠军时回落到 302 给的默认线。仍只输出单变体，
            # 避免 iOS/TV 串行预取所有变体。
            _fast = remembered_fast_line(picked)
            _sel = None
            if _fast:
                _sel = next((t for t in targets if t.get("line") == _fast
                             and not _line_is_bad(t)), None)
            if _sel is None:
                _sel = next((t for t in targets if t.get("line") == explicit_line),
                            picked)
        else:
            _sel = next((t for t in targets if t.get("line") == explicit_line), picked)
        targets = [_sel]
    for t in targets:
        if t.get("m3u8") or re.search(r"\.(m3u8|mp4)(\?|$)", t.get("url", ""), re.I) \
                or t.get("resolve") or t.get("redirect") or _need_resolve(t):
            hls_targets.append(t)
    if not hls_targets:
        return _placeholder_master_response(idx0)
    # 显式线单变体时，仍在后台并发预热该片全部普通 CMS 线（供线路菜单切线秒切），
    # 但不把它们写进本次 master，避免播放器串行预取拖慢首帧。
    if explicit_line and explicit_line != PANSOU_LINE:
        _warm_all = []
        for t in _explicit_all:
            if (t.get("m3u8") or re.search(r"\.m3u8(\?|$)", str(t.get("url") or ""), re.I)) \
                    and not t.get("_pansou_manual") and not t.get("xsrc") \
                    and not any(t.get(k) for k in _SPECIAL) \
                    and t.get("line") != explicit_line:
                _warm_all.append(t)
        if _warm_all:
            _warm_preset_lines(mg, _warm_all[:_PREWARM_MAX], request)

    # 分辨率探测模块已搁置：线路菜单只显示线路名，不追加分辨率标签。
    idx = int(picked.get("index", 0) or 0)

    # 自动起播：无 line（网页）或带 auto=1（App/TV/iOS 的 302/play_link）。
    # 用户从线路菜单手切（有 line 且无 auto=1）不算自动，严格尊重所选线。
    auto_mode = _fastest_on() and ((not explicit_line) or _auto_start)

    def _is_manual_pansou(t: dict) -> bool:
        return bool(t.get("_pansou_manual")) or t.get("line") == PANSOU_LINE

    # 只按【本片预测/竞速出的最快线】置顶；没有本片数据时保持来源顺序，绝不拿全局
    # 源排名做猜测性重排（源级速度代表不了本片分片 CDN）。
    if auto_mode:
        fast = remembered_fast_line(picked)

        def _auto_key(t):
            return 0 if (fast and t["line"] == fast) else 1
        hls_targets.sort(key=_auto_key)
    else:
        # 手动/菜单模式：网盘直链原画排在在线线之前
        hls_targets.sort(key=lambda t: (
            0 if _pan_match(str(t.get("url") or "")) else 1))
    # 无论如何：手动 PanSou 高清线永远排最后，且不作为自动首选（默认不触发搜索）
    hls_targets.sort(key=lambda t: 1 if _is_manual_pansou(t) else 0)

    def variant_uri(t: dict, is_auto_first: bool = False) -> str:
        q = {"index": idx, "line": t["line"]}
        # 手动 PanSou 线即使排首位也绝不带 auto=1（它必须由用户主动选择才触发搜索）
        if is_auto_first and not _is_manual_pansou(t):
            q["auto"] = "1"
        return "main.m3u8?" + urllib.parse.urlencode(q)

    # 起播前并发后台预热最可能起播的若干普通 CMS 线，摊平播放器对各变体串行预取
    # main 的累计冷抓耗时（实测无预热时 20 条串行 4s+，是“起播慢”的主因）。
    _warm_preset_lines(mg, hls_targets, request)

    if len(hls_targets) == 1:
        # 单线路：同样带 NAME，飞牛播放器才会在“线路”菜单里显示这一条；否则菜单为空。
        _only = hls_targets[0]
        _oname = _line_display_name(_only).replace('"', '')
        master = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                  f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,PROGRAM-ID=1,NAME="{_oname}"\n'
                  f"{variant_uri(_only, auto_mode)}\n")
    else:
        # 多线路：无 RESOLUTION 的变体，飞牛 HLS 播放器呈现为可切换「线路」
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        # PanSou 变体给极低码率（自适应码率不会自动选它，只能靠用户按名称手动切）；
        # 普通 CMS 线给递增码率并避开 1，二者不冲突。
        for i, t in enumerate(hls_targets):
            name = _line_display_name(t)
            if _is_manual_pansou(t):
                bw = 1
            else:
                bw = 1_000_000 + i * 100_000
            # NAME 供支持的播放器显示线路名；BANDWIDTH 差异化避免变体被合并
            out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},'
                       f'PROGRAM-ID=1,NAME="{name}"')
            # 仅首个（自动起播）变体带 auto=1
            out.append(variant_uri(t, auto_mode and i == 0))
        master = "\n".join(out) + "\n"
    return Response(master, media_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*"})


_PROVIDER_CN = {
    "baofeng": "暴风", "ffzy": "非凡", "lzi": "量子", "wujin": "无尽",
    "youku": "优酷", "guangsu": "光速", "ruyi": "如意", "jisu": "极速",
    "xinlang": "新狼", "dytt": "电影天堂", "s360": "360",
}
_GROUP_CN = {"m3u8": "主线路", "yun": "云播", "ff": "非凡", "uk": "优酷",
             "gs": "光速", "bf": "暴风", "lz": "量子"}


def _provider_display(provider: str) -> str:
    """资源站 id → 播放器里显示的名字。

    优先用用户在资源站里设置的名称（自定义源 id 形如 custom_xxxx，否则会直接
    露出技术 id，见 issue #6），再回退内置中文名表，最后才用原始 id。"""
    try:
        s = next((x for x in config.all_sources() if x.get("id") == provider), None)
        if s and str(s.get("name") or "").strip():
            return str(s["name"]).strip()
    except Exception:
        pass
    return _PROVIDER_CN.get(provider, provider)


def _friendly_line(key: str) -> str:
    """把 'provider::组名' 转成好认的线路名，如 baofeng::bfzym3u8 -> 暴风。"""
    provider, _, group = key.partition("::")
    pcn = _provider_display(provider)
    if not group:
        return pcn
    if group in _GROUP_CN:
        return f"{pcn}·{_GROUP_CN[group]}"
    # guard-/spider-/drpy- 等技术源：组名常是中文（如 🐻剧圈┃多线），取 ┃| 前可读部分
    if re.match(r"(guard|spider|drpy)-", provider, re.I):
        nice = re.sub(r"[┃|].*$", "", group).strip()
        nice = re.sub(r"^[\s\U0001F000-\U0001FAFF☀-➿]+", "", nice)
        if nice and not re.fullmatch(r"[A-Za-z0-9_-]+", nice):
            return nice
    stem = group
    for suf in ("zym3u8", "m3u8", "yun"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    stem = stem.upper() if stem.isascii() and stem else ""
    return f"{pcn}·{stem}" if stem else pcn


def _line_display_name(t: dict) -> str:
    """播放器线路菜单显示名：网盘线路加 ☁网盘名 前缀，便于区分高质量网盘资源。

    网盘线的来源多为 guard-Xxx 这类技术源名，直接展示不友好；改用线路组名里
    的中文可读部分（去掉 ┃多线 等装饰与 emoji 源前缀）。"""
    line = t.get("line", "")
    url = str(t.get("url") or "")
    if t.get("_pansou_manual") or line == PANSOU_LINE:
        return PANSOU_LABEL
    pv = _pan_match(url) if url else None
    if pv is not None:
        tag = pv.get("name", "网盘").replace("网盘", "")
        group = line.split("::", 1)[1] if "::" in line else ""
        nice = re.sub(r"[┃|].*$", "", group).strip(" 🐌🍓🐻🍉🛡️")
        # 去掉常见纯英文技术源尾巴
        nice = re.sub(r"(guard-|spider-|drpy-)", "", nice, flags=re.I).strip()
        return f"☁{tag} {nice}".strip() if nice else f"☁{tag} 网盘"
    return _friendly_line(line)


def _is_media_playlist(text: str) -> bool:
    return "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text


def _playlist_has_segment(text: str) -> bool:
    """改写后的媒体清单是否真的含至少一条可播分片/子清单 URI。

    上游冷启动可能回 200 但体为空、只有 #EXT 头、是 JSON 错误页或广告过滤后
    不剩任何分片——这种“200 空清单”会让播放器无分片可拉、直接判播放失败。"""
    if not text or not _is_media_playlist(text):
        return False
    for raw in text.splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            return True
    return False


def _first_variant(text: str, base: str) -> str | None:
    """从 master 播放列表里取第一个变体（子 m3u8）的绝对地址。"""
    uri = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        uri = s
        break
    if not uri:
        return None
    return urllib.parse.urljoin(base, uri)


async def _fetch_playlist(request: Request, url: str, headers: dict,
                          tries: int = 3, timeout: float = 15.0):
    """抓取并跟随嵌套 master，返回最终媒体列表文本与其基准 URL。失败返回 (None,url)。"""
    # 网盘播放代理首次需转存+实时换链，给更宽裕的超时
    if "/v/api/ext/pan/" in url and timeout <= 15.0:
        timeout = 45.0
    cur, text = url, None
    for _ in range(4):
        try:
            r = await ext_get_retry(request, cur, headers, tries=tries, timeout=15.0)
        except Exception:
            return None, cur
        if r.status_code != 200:
            return None, cur
        text = r.text
        if _is_media_playlist(text):
            break
        nxt = _first_variant(text, cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return text, cur


def _cached_sibling_playlist(picked: dict, *, prefer: str | None = None):
    """同片同集已预建好的【真实】播放清单（不现场抓上游）。

    自动起播时，点击 URL 携带的 auto=1 线可能与后台补线后重新选出的冠军线名字不一致
    （catalog 片目录首屏用不带源前缀的线名，补线聚合后变成 源::线），导致按精确键
    取清单 miss、白等 2.4s 冷抓甚至回占位。这里允许在【同一片同一集】范围内复用任一
    仍新鲜、确有分片的清单先秒播，线路菜单里各线仍可手动切。prefer 给定时优先它。
    仅用于 auto 起播，不影响用户显式选线。"""
    og = picked.get("online_guid", "")
    idx = int(picked.get("index", 0) or 0)
    if not og:
        return None
    now = time.time()
    prefix = f"{og}|"
    suffix = f"|{idx}"
    best = None
    pref = None
    for key, (ts, body) in list(_PLAYLIST_CACHE.items()):
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        if now - ts >= _PLAYLIST_TTL:
            continue
        if body is None or body is _NOT_HLS or not _playlist_has_segment(body):
            continue
        if "_placeholder/" in body[:400]:
            continue
        line = key[len(prefix):-len(suffix)] if len(key) > len(prefix) + len(suffix) else ""
        if prefer and line == prefer:
            pref = body
        elif best is None:
            best = body
    return pref if pref is not None else best


@app.get(f"{ONLINE_PREFIX}/{{mg}}/main.m3u8")
async def hls_main(mg: str, request: Request):
    phwait = request.query_params.get("phwait") == "1"
    picked = None
    _req_t0 = time.time()
    # 首次 main 的总预算（低于客户端 ~3s 起播超时）：榜单【绑定等待】也必须算进这个
    # 总预算，否则冷榜单片“绑定等待 + 抓清单”两段叠加会顶过 3s（实测 3.3~3.6s），
    # 客户端在首个 main 返回前就判超时；海量库已绑定无此段故秒播。绑定没完成先回
    # 直播式占位（200），后台单飞继续绑定，播放器按短占位分片快速重拉 main 即转正。
    def _budget_left(floor: float = 0.6) -> float:
        return max(floor, _MAIN_SLOW_BUDGET - (time.time() - _req_t0))

    # 占位自修复通道：preset 尚未拿到任何线路时让播放器进入占位，本端点负责等待
    # 后台绑定/找线；期间一律回占位 HLS（200），绝不返回 4xx/5xx 锁死菜单。
    if phwait:
        og = chart_guid_by_media(mg)
        # mg 本身是 chart guid（媒体库直进）时，绑定记录挂在其 media guid 上
        mg_eff = media_guid(og) if (og and og == mg) else mg
        already = store.resolve_media_guid(mg) or (
            og and og == mg and store.resolve_media_guid(mg_eff))
        if og and not already:
            # 播放页即时绑定：不限流（信号量只限制浏览预取）。冷却已过即重新解析：
            # 快源未命中 45s 后可重试（慢源可能已就绪），dead(全无源) 10 分钟后再试。
            force = _chart_cooldown_left(og) <= 0
            start_resolve_chart(og, force=force)
            # 复刻 v0.2.17：普通榜单片这里【同步等绑定完成】（其内部聚合有 25s 上限，
            # 外层用 8s 硬顶仅防永久挂死），不再用 2.4s 提前回占位——绑定后紧接着的
            # 竞速+建清单一次返回真片，正是 0.2.17 秒播体验的来源。
            try:
                await asyncio.wait_for(resolve_chart(og, force=force), timeout=8.0)
            except Exception:
                pass
        if not (_pick_episode(mg, request)
                or (og == mg and _pick_episode(mg_eff, request))):
            # 非榜单片也给一次后台按片名找线机会（由 preset/点击侧已触发则复用）。
            # 始终回【直播式】占位（无 ENDLIST，播放器持续重拉不报错）。
            return _placeholder_playlist_response(mg)
        # 已绑定：落入正常流程（继续按下面的 query 决定线路；phwait 不指定具体线，
        # 走默认/竞速），播放真实清单。

    explicit_line = request.query_params.get("line")

    # ===== 手切网盘线：即便该片尚未绑定任何 CMS 线路（picked 为空）也要能 arm =====
    # 网盘线不依赖 CMS 绑定（自己按片名搜索+转存）。此前这里先做 _pick_episode 为空就
    # 提前回占位，导致未绑定时网盘意图虽在 preset 登记了、main 却永远 arm 不起来。
    if explicit_line == PANSOU_LINE and _pansou_is_user_pick(mg):
        _base = store.resolve_media_guid(mg)
        _ttitle = ((_base or {}).get("title") or (_base or {}).get("ep_title")
                   or _title_for_media(mg))
        # 标题库/绑定里都还拿不到片名（刚搜索、尚未点进详情绑定，或重启后纯 mg）：
        # 无法按片名搜网盘，先回占位；播放器随后的 play/play（App，带绑定）或重拉
        # main（绑定完成后）会带片名重新 arm。绝不拿空关键词去搜。
        _pidx = int(request.query_params.get(
            "index", (_base or {}).get("index", 0)) or 0)
        if _pansou_manual_state(mg) != "ready":
            if _ttitle:
                _pansou_manual_arm(mg, _ttitle, _pidx)
            return _placeholder_playlist_response(mg)
        _snap = _PANSOU_MAN_SNAP.get(mg)
        if _snap:
            picked = dict(_snap, index=_pidx)
            request.state._failover_picked = picked
            request.state._play_mg = mg

    failed_g = await _await_pending_chart(mg)
    if not picked:
        picked = _pick_episode(mg, request)
    if not picked:
        # v0.2.42：普通 main 不再用无限占位掩盖“未找到片源”；
        # 只有显式 phwait 的准备通道保留占位。
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)

    # 线路健康探测 + 自动选最快线
    explicit_line = request.query_params.get("line")
    # v0.2.42：只有网页自动起播（无显式 line）才竞速；App/iOS 经 media_range
    # 带来默认 line + auto=1 时直接用该线，不再为每条候选等待 gather 完成。
    auto_play = request.query_params.get("auto") == "1" and not explicit_line
    request.state._play_mg = mg  # 供探测复用播放列表构建、填充缓存

    # ===== 手动 PanSou 高清线：必须有真实手切意图才搜索/转存 =====
    # PanSou 变体 BANDWIDTH=1 最低，自适应码率不会选它；但播放器【打开线路菜单】或
    # 【上一部选过网盘、切下一部时残留 line】也会直接请求网盘 main。这些都不是手切：
    # 只有经 preset?line=PanSou（web/TV 菜单手点）或 play/play 网盘 bitrate（App）置过
    # 意图才算。无意图时【绝不 arm/转存】，直接回落该片默认最快 CMS 线，保证网盘永不作
    # 默认、永不拖慢换片起播。
    if explicit_line == PANSOU_LINE and not _pansou_is_user_pick(mg):
        base0 = store.resolve_media_guid(mg)
        if base0:
            try:
                didx0 = int(request.query_params.get(
                    "index", base0.get("index", 0)) or 0)
            except ValueError:
                didx0 = int(base0.get("index", 0) or 0)
            def_line0 = store.best_line(base0["online_guid"])
            if def_line0 and def_line0 != PANSOU_LINE:
                rt0 = store.resolve_episode(base0["online_guid"], def_line0, didx0)
                if rt0:
                    picked = dict(rt0, online_guid=base0["online_guid"],
                                  kind=base0.get("kind", "title"))
                    explicit_line = picked.get("line")
                    auto_play = True

    if explicit_line == PANSOU_LINE:
        base = store.resolve_media_guid(mg)
        title = (base or {}).get("title") or (base or {}).get("ep_title") or ""
        pidx = int(request.query_params.get("index", (base or {}).get("index", 0)) or 0)
        st = _pansou_manual_state(mg)
        if st != "ready":
            # 仅待命、记录片名，绝不在这里搜索/转存（main 会被菜单预取）。
            # 真正触发发生在用户切到该线、播放器拉占位分片时（placeholder kick）。
            _pansou_manual_arm(mg, title, pidx)
            return _placeholder_playlist_response(mg)
        snap = _PANSOU_MAN_SNAP.get(mg)
        if not snap:
            return _placeholder_playlist_response(mg)
        picked = dict(snap, index=pidx)
        request.state._failover_picked = picked
        request.state._play_mg = mg
        # 走正常管道：回环抓取（转码档会把夸克 CDN 分片改写成经本代理 /seg，
        # 自动注入 Cookie；裸连 CDN 会 412）。若解析为 mp4 原画(_NOT_HLS)再 302。
        # 新转存的大文件夸克首次生成转码清单可能很慢（>10s），绝不能让 main 一直挂着
        # 顶过客户端~3s 超时：给 2.4s 预算，超时先回占位，并后台继续建清单，下次轮询秒回。
        async def _build_pan():
            try:
                return await _get_playlist_cached(mg, picked, request)
            except Exception:
                return None
        try:
            body = await asyncio.wait_for(
                asyncio.shield(_build_pan()), timeout=_MAIN_SLOW_BUDGET)
        except asyncio.TimeoutError:
            return _placeholder_playlist_response(mg)
        if body and (body is _NOT_HLS or _playlist_has_segment(body)):
            if body is _NOT_HLS:
                return RedirectResponse(picked["url"], status_code=302,
                                        headers={"Cache-Control": "no-store"})
            return Response(body, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-cache",
                                     "Access-Control-Allow-Origin": "*"})
        # 抓取瞬间失败/空清单：回占位等下次轮询，不弹错误锁菜单
        return _placeholder_playlist_response(mg)

    # 起播即跳过已知坏线（用户显式选线除外），避免无谓探测延迟
    picked = _dodge_known_bad(picked, request)

    async def _serve_normal(_p: dict):
        # 【自动起播秒播快路径】auto 起播时，先看本片本片是否已有预建好的真实清单：
        # 优先用预测/竞速冠军线；键错位（补线改名）时退而复用同片任一新鲜清单，直接秒播，
        # 不做任何上游竞速/抓取。用户显式选线(auto_play=False)绝不走这里。
        # 【播放中静默换更快线】auto 起播且当前线真实播放持续慢、同片另一条线
        # 明显更快（seg 端点实时分片测速判定）：播放器周期重拉 main 时直接返回更快线
        # 已建好的清单，不打断当前播放、只让后续分片走快线；带冷却防抖。手动选线不走。
        if auto_play and not _is_slow_line(_p):
            try:
                _faster = linepredict.pick_faster_line(
                    _p.get("online_guid", ""), int(_p.get("index", 0) or 0),
                    _p.get("line", ""))
                if _faster and _faster != _p.get("line"):
                    _fp = next((c for c in _episode_candidates(_p)
                                if c.get("line") == _faster and not _line_is_bad(c)
                                and not _is_pansou_line_name(_faster)), None)
                    if _fp is not None:
                        _fbc = _PLAYLIST_CACHE.get(_playlist_cache_key(_fp))
                        if (_fbc and _fbc[1] and _fbc[1] is not _NOT_HLS
                                and _playlist_has_segment(_fbc[1])
                                and "_placeholder/" not in _fbc[1][:400]):
                            _log.info("播放中检测到慢线，静默切换 %s -> %s",
                                      _p.get("line"), _faster)
                            return Response(_fbc[1],
                                            media_type="application/vnd.apple.mpegurl",
                                            headers={"Cache-Control": "no-cache",
                                                     "Access-Control-Allow-Origin": "*"})
            except Exception:
                pass
        if auto_play:
            _fast = remembered_fast_line(_p)
            _ready = _cached_sibling_playlist(_p, prefer=_fast)
            if _ready is not None:
                return Response(_ready, media_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-cache",
                                         "Access-Control-Allow-Origin": "*"})
        # 线路健康探测 + 自动选最快线（含为胜者建清单，可能耗时）：必须在总预算内，
        # 否则自动竞速慢源时首个 main 仍可能超过客户端~3s 超时。
        if _autofail_on():
            _p = await _ensure_playable(_p, request, auto=auto_play)
            # 若自动换到了别的线路，后续 _pick_episode 仍按 query 的旧线解析；用换好的目标
            request.state._failover_picked = _p

        body = await _get_playlist_cached(mg, _p, request)
        if body is not None and body is not _NOT_HLS and not _playlist_has_segment(body):
            # 200 空/畸形清单（上游冷返回、广告过滤后无分片等）：等同抓流失败处理，
            # 避免播放器拿到无分片清单直接判“播放失败”。
            _log.info("清单无分片，按抓流失败处理: %s", _p.get("line"))
            body = None
        if body is None:
            # 当前线抓取失败：记忆并自动换一条已探测可用的线路重试一次
            if _autofail_on():
                _mark_line_bad(_p)
                alt = await _find_playable(_p, request, skip_current=True)
                if alt is not None:
                    _log.info("播放列表抓取失败，自动换线 %s -> %s",
                              _p.get("line"), alt.get("line"))
                    _p = alt
                    request.state._failover_picked = alt
                    body = await _get_playlist_cached(mg, _p, request)
        if body is None:
            # v0.2.42：普通 CMS 抓流失败直接报错，不返回假 HLS 造成无限占位；
            # 加固/爬虫等慢源仍保留占位自愈通道。
            if _is_slow_line(_p):
                return _placeholder_playlist_response(mg)
            return Response("fetch fail upstream", status_code=502)
        if body is _NOT_HLS:
            # 预判为 HLS、解析后实为 mp4：302 到直链代理（direct 复用已解析结果）
            q = urllib.parse.urlencode({"line": _p["line"], "index": _p["index"]})
            return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)
        # 清单活、分片死（如某源 CDN 删流）：验首片失败则标坏线 + 后台按片名全网回源。
        if _autofail_on() and not await _verify_rewritten_first_seg(body, request, _p):
            _mark_line_bad(_p)
            status = await _resurrect_start(_p, mg)
            if status == "ok":
                alt = _pick_episode(mg, request)
                if alt:
                    body2 = await _get_playlist_cached(mg, alt, request)
                    if body2 and body2 is not _NOT_HLS \
                            and _playlist_has_segment(body2):
                        return Response(body2, media_type="application/vnd.apple.mpegurl",
                                        headers={"Cache-Control": "no-cache",
                                                 "Access-Control-Allow-Origin": "*"})
            if status == "fail":
                return Response("暂无可播线路（该源分片已失效，且未找到其他可用源）",
                                status_code=502)
            if _is_slow_line(_p):
                return _placeholder_playlist_response(mg)
            # v0.2.42 running：让客户端 3 秒后重试，不返回占位视频。
            return Response("正在为你寻找可用线路…", status_code=503,
                            headers={"Retry-After": "3",
                                     "Access-Control-Allow-Origin": "*"})
        _sk = sourcehealth.source_key_of(_p.get("line"), _p)
        if _sk:
            sourcehealth.record(_sk, True)
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache",
                                 "Access-Control-Allow-Origin": "*"})

    # 起播策略（复刻 v0.2.17 的秒播语义）：
    #  - 普通 CMS 直链：【同步等到真清单】，竞速 gather 各候选(单条≤4s)、胜者当场建清单，
    #    main 一次返回正片，尽量不提前回占位（这正是 0.2.17 “快且稳”的关键）。给 8s 宽
    #    预算：覆盖一轮竞速+建清单，仅在极端卡死时才兜底占位，避免早期 3.5s 截断导致大量
    #    本可一次出片的请求退化为“占位→轮询”，引入额外往返与不稳定。
    #  - 仅加固/爬虫/drpy/专有解码/小雅这类十几秒的慢源：短预算到点先回占位、后台自愈，
    #    播放器周期重拉转正片（全功能版相对公开 0.2.17 必须保留的兜底）。
    slow_source = _is_slow_line(picked)
    if not slow_source:
        # v0.2.42：普通 CMS 每次 main 独立执行。不要在外层再做单飞合并，
        # 否则前一次探测卡住时，后续重试全部挂在同一个 _serve_normal 上。
        return await _serve_normal(picked)
    # 仅慢源保留单飞与短预算自愈，避免后台重复启动昂贵转存/爬虫任务。
    bk = f"{mg}|{picked.get('line')}|{picked.get('index', 0)}"
    fut = _MAIN_SERVE_FUT.get(bk)
    if fut is None:
        fut = asyncio.ensure_future(_serve_normal(picked))
        _MAIN_SERVE_FUT[bk] = fut
        fut.add_done_callback(lambda _f: _MAIN_SERVE_FUT.pop(bk, None))
    try:
        return await asyncio.wait_for(asyncio.shield(fut),
                                      timeout=_budget_left(floor=0.2))
    except asyncio.TimeoutError:
        return _placeholder_playlist_response(mg)


def _cors_preflight() -> Response:
    """播放路径的 CORS 预检：移动/TV 等跨域客户端在拉 m3u8/seg/key 前可能先发
    OPTIONS。必须由本服务直接应答标准 CORS 头，不能兜底透传给官方后端（后者不带
    CORS 头，会导致客户端判定跨域失败、报 http error）。"""
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "3600",
        "Content-Length": "0",
    })


@app.options(f"{ONLINE_PREFIX}/{{rest:path}}")
async def hls_cors_preflight(rest: str):
    return _cors_preflight()


# ---- 线路未就绪占位视频（保持播放器存活、线路菜单可切，不弹“播放出错”）----
_PH_DIR = Path(__file__).resolve().parent / "assets" / "placeholder"
_PH_SEG_TS = "_placeholder/placeholder.ts"
_PH_SEG_DUR = 2.0
_PH_WIN = 3  # 直播式滑动窗口分片数（3×2s≈6s：缩短冷片占位后的转正重拉间隔）


def _placeholder_playlist_response(mg: str = "", live: bool = True) -> Response:
    """返回循环提示 HLS（HTTP 200）。飞牛对 4xx/5xx 清单会弹“播放出错”并锁死
    线路菜单；200 的有效 HLS 让播放器停在提示画面、菜单仍可切换。

    用直播式滑动窗口（无 ENDLIST、带 MEDIA-SEQUENCE）：客户端只预取窗口内少数分片
    并周期性重拉 main，后台转存/搜索完成后下一次轮询即可自动切回正片。分片用相对
    路径，相对 /{mg}/main.m3u8 解析为 /{mg}/_placeholder/placeholder.ts。"""
    seq = int(time.time() // int(_PH_SEG_DUR))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(_PH_SEG_DUR)}",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}",
             "#EXT-X-DISCONTINUITY-SEQUENCE:0"]
    for i in range(_PH_WIN):
        lines.append(f"#EXTINF:{_PH_SEG_DUR:.3f},")
        lines.append(f"{_PH_SEG_TS}?n={seq + i}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_master_response(idx: int = 0) -> Response:
    """线路未就绪时的【master】占位（HTTP 200）。返回单变体，其 main 带 phwait=1，
    hls_main 见到该标记会持续返回占位媒体清单（不挑线路、不报 4xx/5xx），后台绑定/
    找线完成后由后续正常请求自动切回正片。保证任何情况下播放器都不弹“播放出错”、
    线路菜单不被锁死。"""
    q = urllib.parse.urlencode({"index": idx, "phwait": "1"})
    body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1000000,PROGRAM-ID=1,NAME="正在准备线路…"\n'
            f"main.m3u8?{q}\n")
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_ts_response() -> Response:
    p = _PH_DIR / "placeholder.ts"
    if not p.exists():
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="video/mp2t",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/_placeholder/placeholder.ts")
async def placeholder_ts(mg: str, request: Request):
    # 占位分片被播放器拉取：只有该 mg 已为【网盘线待命/进行中】时才触发搜索+转存。
    # 普通慢线/未绑定也用同一个占位分片，但它们从不 arm 网盘状态，故不会误触发。
    # PanSou 变体 BANDWIDTH=1，自适应码率不会选它，能走到这里拉分片即用户手切网盘线。
    try:
        _st = _pansou_manual_state(mg)
        if _st == "need_login":
            # 网盘未登录：直接在播放画面里出登录二维码（烧进 TS 分片）。
            # kick 负责在扫码成功后自动恢复转存；二维码生成失败则回退普通占位。
            _pansou_manual_kick(mg)
            qr = await _pansou_login_qr_png(mg)
            if qr is not None:
                _png, _name = qr
                ts_bytes = await panqr.build_login_ts_async(
                    _png, f"{_name}登录", "请用对应网盘 App 扫码，登录后自动播放")
                if ts_bytes:
                    return Response(content=ts_bytes, media_type="video/mp2t",
                                    headers={"Cache-Control": "no-store",
                                             "Access-Control-Allow-Origin": "*",
                                             "X-fnmedia-PanLogin": "1"})
        elif _st in ("armed", "pending", "ready", "dead"):
            _pansou_manual_kick(mg)
    except Exception:
        pass
    return _placeholder_ts_response()


@app.get(f"{ONLINE_PREFIX}/{{mg}}/seg")
async def hls_seg(mg: str, request: Request):
    u = request.query_params.get("u", "")
    try:
        src_url = b64d(u)
    except Exception:
        return Response("bad u", status_code=400)
    await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    headers = picked["headers"] if picked else {}
    # 透传 Range（播放器 seek）
    fwd = dict(headers)
    # 域名解析为通用逻辑（公开版也需要，用于下方网盘 Cookie 判定），不放进私有块，
    # 否则剥离 GUARD 块后下方 _host 引用会 NameError。
    _host = urllib.parse.urlsplit(src_url).netloc.lower()
    # 加密 HLS 由本服务解密：不能按密文转发任意字节 Range，需完整拉取后回传纯 TS。
    if request.query_params.get("dec") == "1":
        fwd.pop("Range", None)
    if "range" in request.headers and request.query_params.get("dec") != "1":
        fwd["Range"] = request.headers["range"]
    try:
        # 分片在播放中途持续拉取，对瞬时抖动必须重试，否则单次失败播放器直接报错
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:
        _log.warning("seg fetch fail %s %s", type(exc).__name__,
                     urllib.parse.urlsplit(src_url).netloc)
        return Response(f"seg fail {type(exc).__name__}", status_code=502)

    # 流式快路径：普通、无需服务端处理的分片【边收边发】，首字节不再等整段下完，
    # 对慢 CDN（分片 1~10s）能显著降低播放器缓冲等待、减少“放一会卡住”。
    # 仍需整段处理的情形不走这里：AES 解密(dec=1)、夸克/UC 网盘分片（保证 Cookie
    # 注入语义统一）。PNG 伪装分片在生成器里读首块嗅探，发现 PNG 头则转缓冲剥壳。
    is_decrypt = request.query_params.get("dec") == "1"
    _need_pan_cookie = _host.endswith("drive.quark.cn") or _host.endswith(".quark.cn") \
        or _host.endswith(".uc.cn")
    if (not is_decrypt and not _need_pan_cookie
            and r.status_code in (200, 206) and hasattr(r, "aiter_bytes")):
        # 播放中实时分片测速上下文（用于持续慢源静默换线）
        _tog = (picked.get("online_guid", "") if picked else "") or \
            store.online_guid_by_media(mg)
        _tidx = int((picked or {}).get("index", request.query_params.get("index", 0)) or 0)
        _tline = request.query_params.get("line") or \
            ((picked or {}).get("line", ""))
        _t0 = time.monotonic()

        async def _stream_seg():
            buf = b""
            sniffed = False
            try:
                async for chunk in r.aiter_bytes(64 * 1024):
                    if not sniffed:
                        sniffed = True
                        buf += chunk
                        if buf[:8] == b"\x89PNG\r\n\x1a\n":
                            # PNG 伪装分片：收齐后剥壳再发，退出流式改缓冲路径
                            async for rest in r.aiter_bytes(64 * 1024):
                                buf += rest
                            try:
                                import quality as _q
                                payload, _ = _q._unwrap_segment(buf)
                                buf = payload if payload is not None else buf
                            except Exception:
                                pass
                            yield buf
                            return
                        if buf:
                            yield buf
                            buf = b""
                        continue
                    yield chunk
            finally:
                try:
                    await r.aclose()
                except Exception:
                    pass
                # 记录该线真实分片下载耗时（整段流式完成时刻）
                if _tog and _tline and not _is_pansou_line_name(_tline):
                    try:
                        linepredict.record_seg(
                            _tog, _tidx, _tline, time.monotonic() - _t0)
                    except Exception:
                        pass

        hs = _safe_response_headers(r.headers, {"content-security-policy"})
        hs["Access-Control-Allow-Origin"] = "*"
        hs.pop("content-length", None)  # 剥 PNG 壳后长度可能变化，用分块传输
        hs.setdefault("Content-Type", "video/mp2t")
        if r.status_code in _RETRY_STATUS:
            _log.warning("seg upstream %s %s", r.status_code,
                         urllib.parse.urlsplit(src_url).netloc)
        return StreamingResponse(_stream_seg(),
                                 status_code=r.status_code, headers=hs)

    body = r.content
    # 服务端解密 AES-128 HLS 分片：m3u8 已移除 EXT-X-KEY，这里把密文 TS 解成纯 TS，
    # 并强制按完整 200 内容返回（密文 206 不能直接解密/拼接）。
    decrypt_resp_status = r.status_code
    if request.query_params.get("dec") == "1":
        try:
            import quality as _quality
            key = bytes.fromhex(b64d(request.query_params.get("k", "")))
            ivhex = b64d(request.query_params.get("iv", ""))
            iv = bytes.fromhex(ivhex)
            if len(key) == 16 and len(iv) == 16:
                dec = await _quality._aes_cbc_decrypt(body, key, iv)
                if dec and len(dec) >= 188 and dec[0] == 0x47:
                    body = dec
                    decrypt_resp_status = 200
        except Exception as exc:
            _log.warning("seg decrypt fail: %s", type(exc).__name__)
    ctype = r.headers.get("content-type", "")
    unwrapped = False
    # 部分源把真实 TS 分片伪装成「1×1 PNG 头 + TS」。剥掉 PNG 外壳
    # 还原成纯 TS，飞牛播放器才能正常解码（通用兼容，对所有源生效）
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body = payload
                ctype = "video/mp2t"
                unwrapped = True
        except Exception:
            pass
    drop = {"content-security-policy"}
    if request.query_params.get("dec") == "1":
        # 已在服务端解密并返回完整纯 TS：旧的 Range/content-range 必须移除，
        # 否则严格客户端会因 200 + Content-Range/Accept-Ranges 语义矛盾拒绝该分片。
        drop.update({"content-range", "accept-ranges"})
    if unwrapped:
        drop.add("content-type")
    h = _safe_response_headers(r.headers, drop)
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    if unwrapped or "content-type" not in {k.lower() for k in h}:
        h["Content-Type"] = ctype or "video/mp2t"
    if r.status_code in _RETRY_STATUS:
        _log.warning("seg upstream %s %s", r.status_code,
                     urllib.parse.urlsplit(src_url).netloc)
    return Response(content=body, status_code=decrypt_resp_status, headers=h)


@app.get(f"{ONLINE_PREFIX}/{{mg}}/hls")
async def hls_compact(mg: str, request: Request):
    """紧凑分片：清单只带 r=<注册表ID>&i=<序号>，真实 CDN（夸克/UC 等）地址留
    在服务端。用于签名分片超长、清单过大的转码 HLS，规避外网网关 URL/体积限制。"""
    rid = request.query_params.get("r", "")
    try:
        idx = int(request.query_params.get("i", "0"))
    except ValueError:
        idx = 0
    src_url = _hls_reg_get(rid, idx)
    if not src_url:
        return Response("expired", status_code=410)
    fwd: dict = {}
    # 网盘 CDN 分片必须带登录 Cookie（裸取 412），按域名兜底注入
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        _log.warning("hls compact seg fail %s", type(exc).__name__)
        return Response("seg fail", status_code=502)
    body = r.content
    ctype = r.headers.get("content-type", "")
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body, ctype = payload, "video/mp2t"
        except Exception:
            pass
    h = _safe_response_headers(r.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    h.setdefault("Content-Type", ctype or "video/mp2t")
    return Response(content=body, status_code=r.status_code, headers=h)


# ------------------------------------------------------------- 海报代理

def _decode_tok(tok: str) -> str:
    tok = tok.split(".", 1)[0]
    return b64d(tok)


def _decode_poster_tok(tok: str) -> tuple[str, str, int | None, int | None]:
    """解码海报 token，返回 (图床URL, 网盘名, 健康等级, 速度档)。

    兼容旧版纯 URL token 与 JSON{u,p,h,s} token。横幅裁切不走 token，
    由海报路径的 ?crop=16x9 查询参数在 _serve_online_poster 处理，保持
    图片路径仍是客户端可直接加载的普通 base64 token。"""
    raw = _decode_tok(tok)
    pan_label, grade, speed = "", None, None
    url = raw
    if raw[:1] in ("{", "["):
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                url = j.get("u", "")
                pan_label = str(j.get("p", "") or "")
                if j.get("h") is not None:
                    grade = int(j["h"])
                if j.get("s") is not None:
                    speed = int(j["s"])
        except Exception:
            url = raw
    return url, pan_label, grade, speed


def _poster_fallback() -> Response:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#1f6feb"/><stop offset="1" stop-color="#7c3aed"/>
  </linearGradient></defs>
  <rect width="300" height="450" fill="url(#g)"/>
  <circle cx="150" cy="180" r="58" fill="rgba(255,255,255,.18)"/>
  <polygon points="132,145 132,215 192,180" fill="#fff" opacity=".92"/>
  <text x="150" y="285" text-anchor="middle" fill="#fff" font-family="sans-serif"
    font-size="26" font-weight="700">在线影视</text>
  <text x="150" y="322" text-anchor="middle" fill="rgba(255,255,255,.72)"
    font-family="sans-serif" font-size="15">fnmedia-ext</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})



_POSTER_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


async def _poster_fetch(request: Request, url: str, headers: dict,
                        host: str):
    """海报回源带一次短重试：直连出现连接级异常时，用全新短生命周期客户端再取一次，
    避免偶发 ConnectTimeout/ReadError 直接把整面海报墙变成占位图。正常 HTTP 错误不重试。"""
    try:
        return await _poster_http(request).get(url, headers=headers)
    except _POSTER_NETWORK_ERRORS as first:
        _log.info("poster retry host=%s err=%s", host, type(first).__name__)
        try:
            # netutil 复用既有“直连失败按主机自动切代理”能力；未配置代理时
            # 也相当于一条全新直连，能避开失效连接/连接池中的异常连接。
            async with netutil.async_client(timeout=12.0) as cli:
                return await cli.get(url, headers=headers)
        except Exception:
            raise first


async def _serve_online_poster(tok_path: str, request: Request) -> Response:
    # 兼容 /online/<token>.jpg 与官方图片前缀 /v/api/v1/sys/img/online/<token>.jpg
    # 机制对齐 v0.2.42：纯透传 + 浏览器 max-age 强缓存，服务端不做任何内存/磁盘缓存，
    # 事件循环零额外缓存逻辑，海报墙瞬载。
    tok_path = tok_path.lstrip("/")
    marker = "online/"
    if marker in tok_path:
        tok_path = tok_path.split(marker, 1)[1]
    try:
        u, pan_label, health_grade, _speed_grade = _decode_poster_tok(tok_path)
    except Exception:
        return _poster_fallback()
    # 播放历史横幅：?crop=16x9 时中心裁切成 16:9（在线源只有竖幅封面）。
    want_landscape = request.query_params.get("crop", "") in ("16x9", "1", "landscape")
    if not u.startswith("http"):
        return _poster_fallback()
    want_badge = bool(pan_label) or health_grade is not None
    headers = {"User-Agent": "Mozilla/5.0"}
    host = urllib.parse.urlsplit(u).netloc.lower()
    # 豆瓣图床需要带豆瓣 Referer，否则可能 403
    if host.endswith("doubanio.com") or host.endswith("douban.com") or \
            host.endswith("doubanio.viki.moe"):
        headers["Referer"] = "https://movie.douban.com/"
    try:
        r = await _poster_fetch(request, u, headers, host)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or not r.content:
            return _poster_fallback()
        # 部分图床给 webp/jpg 标成 application/octet-stream，按魔数嗅探真实类型
        sniff = ""
        head = r.content[:12]
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            sniff = "image/webp"
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            sniff = "image/png"
        elif head[:3] == b"\xff\xd8\xff":
            sniff = "image/jpeg"
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            sniff = "image/gif"
        if not (ctype.startswith("image/") or sniff):
            return _poster_fallback()
        ctype = ctype if ctype.startswith("image/") else sniff
    except Exception as exc:
        # 诊断海报回源异常：原实现直接吞异常回占位，无法区分连接超时、读超时、
        # 连接池耗尽或证书问题。只记录类型与摘要，不改变原有回退行为。
        _log.info("poster upstream failed host=%s err=%s: %s",
                  host, type(exc).__name__, str(exc)[:180])
        return _poster_fallback()

    # 在海报上烧角标（网盘资源 / 源健康点）。GIF 不动，合成失败回退原图。
    out_bytes, out_type = r.content, (ctype or "image/jpeg")
    if want_badge and "gif" not in out_type:
        try:
            import posterbadge
            if posterbadge.available():
                badged = await asyncio.to_thread(
                    posterbadge.badge, r.content, pan_label, health_grade)
                if badged:
                    out_bytes, out_type = badged, "image/jpeg"
        except Exception:  # noqa: BLE001
            pass
    # 横幅裁切（播放历史 backdrop）：中心裁成 16:9，GIF 不动，失败回退原图。
    if want_landscape and "gif" not in out_type:
        try:
            import posterbadge
            if posterbadge.available():
                cropped = await asyncio.to_thread(posterbadge.landscape, out_bytes)
                if cropped:
                    out_bytes, out_type = cropped, "image/jpeg"
        except Exception:  # noqa: BLE001
            pass
    return Response(content=out_bytes, status_code=200,
                    media_type=out_type,
                    headers={"Cache-Control": "max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/online/{tok_path:path}")
async def online_poster_short(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/v/api/v1/sys/img/online/{tok_path:path}")
async def online_poster(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/_ext/healthz")
async def healthz():
    return {"ok": True, "service": "fnmedia-ext", "upstream": _up_status()}


def _up_status() -> str:
    return "unknown"


# ------------------------------------------------------------- 管理 API

@app.get("/v/api/ext/whoami")
async def ext_whoami(request: Request):
    """回显当前请求被判定为哪种客户端，供在出问题的设备上打开管理台自检。"""
    cl = _client_of(request)
    return {"code": 0, "msg": "", "data": {
        "kind": cl.kind, "label": cl.label, "is_tv": cl.is_tv,
        "is_mobile": cl.is_mobile, "is_web": cl.is_web,
        "strict_card": cl.strict_card,
        "ua": request.headers.get("user-agent", "")}}


@app.get("/v/api/ext/live")
async def ext_live(request: Request):
    """实时播放诊断：各端最近请求、当前播放会话、转发来源与错误。"""
    return {"code": 0, "msg": "", "data": livemod.snapshot()}


@app.post("/v/api/ext/live/clear")
async def ext_live_clear(request: Request):
    livemod.reset()
    return {"code": 0, "msg": "", "data": True}


@app.get("/v/api/ext/titles-cache")
async def ext_titles_cache(request: Request):
    """在线资源缓存 titles.json 的条数/字节（管理页性能卡，issue #42）。
    文件超过配置字节上限时，顺手调度一次后台裁剪落盘（重启后打开管理页即自动瘦身）。"""
    try:
        p = store._state
        size = p.stat().st_size if p.exists() else 0
    except Exception:
        size = 0
    with store._lock:
        entries = len(store._titles)
        dirty = bool(store._dirty)
    try:
        import store as _storemod
        _, max_bytes, _ = _storemod._cache_cfg()
        if max_bytes > 0 and size > max_bytes and not dirty:
            with store._lock:
                store._dirty = True
            store._schedule_flush()
    except Exception:
        pass
    return {"code": 0, "entries": entries, "bytes": size, "dirty": dirty}


@app.post("/v/api/ext/titles-cache/clear")
async def ext_titles_cache_clear(request: Request):
    """清空在线资源缓存并重启服务（慢盘用户搜索退化时一键回到冷态，issue #42）。"""
    try:
        store.close()
        with store._lock:
            store._titles, store._orig = {}, {}
            store._ep_index, store._media_index = {}, {}
            store._dirty = False
        try:
            store._state.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        return {"code": 5000, "msg": f"清空失败：{exc}"}

    def _restart():
        import os
        time.sleep(1.2)
        os.system("systemctl restart fnmedia-ext >/dev/null 2>&1 &")

    threading.Thread(target=_restart, daemon=True).start()
    return {"code": 0, "msg": "已清空，服务重启中"}


def _cache_sizes() -> dict:
    """各类可清缓存的体积/条数（管理页缓存管理卡）。"""
    out = {}
    try:
        p = store._state
        out["titles"] = {"bytes": p.stat().st_size if p.exists() else 0,
                         "entries": len(store._titles), "label": "在线资源缓存"}
    except Exception:
        out["titles"] = {"bytes": 0, "entries": 0, "label": "在线资源缓存"}
    try:
        cp = chartmod.STATE
        out["charts"] = {"bytes": cp.stat().st_size if cp.exists() else 0,
                         "label": "热门榜单缓存"}
    except Exception:
        out["charts"] = {"bytes": 0, "label": "热门榜单缓存"}
    with catalog._lock:
        out["catalog"] = {"routes": len(catalog._routes),
                          "pages": len(catalog._page_cache),
                          "label": "海量库分类/分页缓存"}
    try:
        out["search"] = {"entries": len(_search_cache), "label": "搜索结果内存缓存"}
    except Exception:
        out["search"] = {"entries": 0, "label": "搜索结果内存缓存"}
    try:
        out["health"] = {"entries": len(sourcehealth.snapshot())
                         if hasattr(sourcehealth, "snapshot") else 0,
                         "label": "源健康画像"}
    except Exception:
        out["health"] = {"entries": 0, "label": "源健康画像"}
    return out


@app.get("/v/api/ext/cache")
async def ext_cache_status(request: Request):
    return {"code": 0, "data": _cache_sizes()}


@app.post("/v/api/ext/cache/clear")
async def ext_cache_clear(request: Request):
    """一键清除指定缓存。targets: search/charts/catalog/titles/health 的子集；
    省略或 ["all"] 清前四项（health 默认保留，避免冷启动选源退化）。"""
    body = await request.json()
    targets = body.get("targets")
    if not targets or targets == ["all"]:
        targets = ["search", "charts", "catalog", "titles"]
    targets = {str(t) for t in targets}
    done, need_restart = [], False
    if "search" in targets:
        _search_cache.clear(); _AGG_CACHE.clear(); done.append("search")
    if "charts" in targets:
        try:
            chart_cache.reset(); done.append("charts")
        except Exception:
            pass
    if "catalog" in targets:
        try:
            catalog.reset(); done.append("catalog")
            _schedule_routes_refresh()
        except Exception:
            pass
    if "health" in targets:
        try:
            sourcehealth.reset(); done.append("health")
        except Exception:
            pass
    if "titles" in targets:
        need_restart = True
        done.append("titles")

        def _clear_titles_restart():
            import os
            try:
                store.close()
                with store._lock:
                    store._titles, store._orig = {}, {}
                    store._ep_index, store._media_index = {}, {}
                    store._dirty = False
                store._state.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(1.0)
            os.system("systemctl restart fnmedia-ext >/dev/null 2>&1 &")

        threading.Thread(target=_clear_titles_restart, daemon=True).start()
    return {"code": 0, "data": {"cleared": done, "restart": need_restart},
            "msg": "已清除：" + "、".join(done) + ("，服务重启中" if need_restart else "")}


@app.get("/v/api/ext/status")
async def ext_status(request: Request):
    overrides = config.get().get("source_priority") or {}
    priorities = {s["id"]: providers.source_priority(s, overrides)
                  for s in config.all_sources()}
    return {"ok": True,
            "config": {k: v for k, v in config.get().items() if k != "admin_token"},
            "source_priority": priorities,
            "cached_titles": len(store._titles),
            "hot": hot_cache.status(),
            "charts": chart_cache.status(),
            "catalog": catalog.status()}


# --------------------------------------------------------------- 版本更新
@app.get("/v/api/ext/update/check")
async def ext_update_check(request: Request):
    force = request.query_params.get("force") in ("1", "true", "yes")
    return await updater.check(force=force)


@app.get("/v/api/ext/update/status")
async def ext_update_status(request: Request):
    return updater.status()


@app.post("/v/api/ext/update/now")
async def ext_update_now(request: Request):
    # 仅管理台显式触发；下载校验后交给 fnOS 应用中心正规升级（会短暂重启服务）。
    # 该动作具备系统级副作用，要求显式确认 + 自定义头（跨站简单请求无法携带，
    # 起到 CSRF 防护作用）；只读的检查/状态接口不加此限制。
    try:
        body = await request.json()
    except Exception:
        body = {}
    if request.headers.get("x-fnmedia-admin") != "1" or body.get("confirm") is not True:
        return JSONResponse({"code": 403,
                             "msg": "需要在管理页显式确认后才能执行更新"},
                            status_code=403)
    return await updater.start_update()


@app.post("/v/api/ext/source/priority")
async def ext_source_priority(request: Request):
    body = await request.json()
    mapping = body.get("priority") if isinstance(body.get("priority"), dict) else body
    ok, msg = config.set_source_priority(mapping if isinstance(mapping, dict) else {})
    if "prefer_quality_source" in body:
        config.save({"prefer_quality_source": bool(body["prefer_quality_source"])})
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"source_priority": config.source_priorities()}}


def _virtual_lib_list() -> list[dict]:
    """供管理台展示的虚拟媒体库清单（榜单/海量/单源/加固/网盘），含显隐与排序。"""
    out = []
    out.append({"guid": T4_LIB_GUID, "key": "t4", "title": T4_LIB_TITLE,
                "kind": "t4", "group": "t4", "group_on": bool(_t4_sources()),
                "count": len(_t4_sources())})
    # 榜单四库
    for cat, title, _c in chartmod.CHART_CATEGORIES:
        out.append({"guid": CHART_LIB_GUIDS[cat],
                    "key": vlibs.make_key(vlibs.K_CHART, cat),
                    "title": title, "kind": "chart",
                    "group_on": charts_enabled(),
                    "count": len(chart_cache.items(cat))})
    # 海量四库
    for cat, title, _c in catalogmod.CATALOG_CATEGORIES:
        out.append({"guid": CATALOG_LIB_GUIDS[cat],
                    "key": vlibs.make_key(vlibs.K_CATALOG, cat),
                    "title": title, "kind": "catalog",
                    "group_on": catalog_enabled() and catalog.available(cat),
                    "count": catalog.total(cat) if catalog.available(cat) else 0})
    # 单源独立库
    for s in standalone_sources_active():
        out.append({"guid": standalone_lib_guid(s["id"]),
                    "key": vlibs.make_key(vlibs.K_STANDALONE, s["id"]),
                    "title": standalone_lib_name(s), "kind": "standalone",
                    "group_on": True, "source": s["id"],
                    "count": 0})

    hidden = vlibs.hidden_libs()
    pinned = config.pinned_lib_guids()
    order = vlibs.lib_order()
    for d in out:
        d["hidden"] = d["key"] in hidden
        d["pinned"] = d["guid"] in pinned
        try:
            d["order"] = order.index(d["key"])
        except ValueError:
            d["order"] = -1
    # 默认顺序输出（管理端可据 order 自行排）
    return out


@app.get("/v/api/ext/libs")
async def ext_libs(request: Request):
    return {"code": 0, "msg": "", "data": {
        "hot_enabled": charts_enabled(),
        "catalog_enabled": catalog_enabled(),
        "placement": vlibs.lib_placement_default(),
        "anchor": vlibs.lib_anchor(),
        "order": vlibs.lib_order(),
        "libs": _virtual_lib_list()}}


@app.post("/v/api/ext/lib/visibility")
async def ext_lib_visibility(request: Request):
    """单库显示开关：{key, hidden:true|false}"""
    body = await request.json()
    key = str(body.get("key") or "").strip()
    if not key:
        return JSONResponse({"code": 400, "msg": "缺少 key", "data": None})
    vlibs.set_hidden(key, bool(body.get("hidden")))
    return {"code": 0, "msg": "",
            "data": {"hidden": sorted(vlibs.hidden_libs())}}


@app.post("/v/api/ext/lib/order")
async def ext_lib_order(request: Request):
    """调整虚拟库顺序与相对本地库位置。

    入参：{order:[key...], placement:"front"|"back"}（均可选）
    """
    body = await request.json()
    if isinstance(body.get("order"), list):
        vlibs.set_order([str(x) for x in body["order"]])
    placement = str(body.get("placement") or "").strip()
    if placement in ("front", "back", "after"):
        patch = {"lib_placement": placement}
        if "anchor" in body:
            patch["lib_anchor"] = str(body.get("anchor") or "")[:120]
        config.save(patch)
    return {"code": 0, "msg": "", "data": {
        "order": vlibs.lib_order(),
        "placement": vlibs.lib_placement_default(),
        "anchor": vlibs.lib_anchor()}}


@app.get("/v/api/ext/local-libs")
async def ext_local_libs(request: Request):
    """飞牛本地媒体库清单（复用当前浏览器登录态向官方查询），供排序锚点选择。"""
    try:
        up = await forward_upstream(request)
        payload = await _json_from_upstream(up)
        rows = (payload or {}).get("data")
        virt = _all_virtual_guids()
        out = []
        if isinstance(rows, list):
            for d in rows:
                if d.get("guid") in virt:
                    continue
                out.append({"guid": d.get("guid"), "title": d.get("title") or ""})
        return {"code": 0, "msg": "", "data": out}
    except Exception as exc:
        return {"code": 0, "msg": "", "data": [], "note": str(exc)[:120]}


def _all_virtual_guids() -> set:
    g = set(CHART_LIB_GUIDS.values()) | set(CATALOG_LIB_GUIDS.values()) | \
        {standalone_lib_guid(s["id"]) for s in config.all_sources()}
    return g


# ------------------------------------------------------------ 子账号授权
@app.get("/v/api/ext/accounts")
async def ext_accounts(request: Request):
    """列出飞牛影视账号及其库授权，供管理页配置。"""
    users = fnusers.list_users()
    acl = vlibs._acl()
    for u in users:
        u["acl"] = acl.get(u["guid"])  # None=默认；[]=全禁
    return {"code": 0, "msg": "", "data": {
        "users": users,
        "default_visible": bool(config.get().get("acl_default_visible", True)),
        "default_online": bool(config.get().get("acl_default_online", True)),
        "libs": [{"key": d["key"], "title": d["title"]}
                 for d in _virtual_lib_list()]}}


@app.post("/v/api/ext/account/acl")
async def ext_account_acl(request: Request):
    """设置账号授权：{guid, allow:[key...含可选 __online__]|null, default_visible?, default_online?}"""
    body = await request.json()
    guid = str(body.get("guid") or "").strip()
    if guid:
        allow = body.get("allow")
        if allow is None:
            vlibs.set_account_acl(guid, None)
        elif isinstance(allow, list):
            vlibs.set_account_acl(guid, [str(x) for x in allow])
        else:
            return JSONResponse({"code": 400, "msg": "allow 须为数组或 null",
                                 "data": None})
    patch = {}
    if "default_visible" in body:
        patch["acl_default_visible"] = bool(body["default_visible"])
    if "default_online" in body:
        patch["acl_default_online"] = bool(body["default_online"])
    if patch:
        config.save(patch)
    return {"code": 0, "msg": "", "data": {"acl": vlibs._acl()}}


@app.post("/v/api/ext/lib/pin")
async def ext_lib_pin(request: Request):
    body = await request.json()
    guid = str(body.get("guid") or "")
    on = bool(body.get("pinned"))
    ok, msg = config.set_lib_pinned(guid, on)
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"pinned_libs": sorted(config.pinned_lib_guids())}}


@app.post("/v/api/ext/hot/refresh")
async def ext_hot_refresh(request: Request):
    await chart_cache.refresh(force=True)
    try:
        if catalog.routes_stale():
            await asyncio.wait_for(
                catalog.refresh_routes(config.enabled_sources()), timeout=90)
    except Exception:
        pass
    return {"code": 0, "msg": "", "data": chart_cache.status()}


@app.get("/v/api/ext/online")
async def ext_online(q: str = "", limit: int = 200):
    items = []
    for g, b in store._titles.items():
        items.append({
            "guid": g, "title": b.get("title"), "year": b.get("year"),
            "area": b.get("area"), "remarks": b.get("remarks"),
            "score": b.get("score"), "provider": b.get("online_id", "").split(":")[0],
            "episodes": sum(len(e) for e in b.get("lines", {}).values()),
            "poster": b.get("poster"),
        })
    items.sort(key=lambda x: x.get("year") or "", reverse=True)
    if q:
        items = [x for x in items if q.lower() in (x["title"] or "").lower()]
    return {"code": 0, "data": items[:limit]}


@app.post("/v/api/ext/config")
async def ext_set_config(request: Request):
    body = await request.json()
    allowed = {
        "max_per_keyword", "filter_junk", "junk_keywords", "online_first",
        "hot_library_enabled", "hot_library_title", "hot_limit",
        "hot_per_source", "hot_pages_per_source", "hot_refresh_interval",
        "titles_cache_max_entries", "titles_cache_max_mb",
        "titles_flush_debounce",
        "catalog_library_enabled",
        "t4_library_enabled",
        "prefer_quality_source",
        "auto_best_source",
        "line_fastest", "line_fastest_max", "line_fastest_timeout",
        "line_fastest_prefer", "line_fastest_budget", "main_ready_budget",
        "line_autofail",
        "browse_fill_lines_enabled",
        "prewarm_mode",
        "direct_play_enabled", "direct_play_probe",
        "pan_cache_cleanup_enabled", "pan_cache_ttl_hours",
        "update_proxy",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    if "prewarm_mode" in patch:
        # 只允许两种预热方式，非法值回退默认 realtime（方案A）
        patch["prewarm_mode"] = "persist" if str(patch["prewarm_mode"]) == "persist" else "realtime"
    if "hot_library_title" in patch:
        patch["hot_library_title"] = str(patch["hot_library_title"]).strip()[:20] or HOT_DEFAULT_TITLE
    if "hot_limit" in patch:
        patch["hot_limit"] = max(60, min(800, int(patch["hot_limit"])))
    if "hot_per_source" in patch:
        patch["hot_per_source"] = max(20, min(240, int(patch["hot_per_source"])))
    if "hot_pages_per_source" in patch:
        patch["hot_pages_per_source"] = max(1, min(10, int(patch["hot_pages_per_source"])))
    if "hot_refresh_interval" in patch:
        patch["hot_refresh_interval"] = max(1 * 3600, min(24 * 3600,
                                                          int(patch["hot_refresh_interval"])))
    # issue #42：titles.json 缓存策略钳制（非法输入忽略该项）
    try:
        if "titles_cache_max_entries" in patch:
            patch["titles_cache_max_entries"] = max(
                100, min(20000, int(patch["titles_cache_max_entries"])))
        if "titles_cache_max_mb" in patch:
            patch["titles_cache_max_mb"] = max(
                0, min(512, float(patch["titles_cache_max_mb"])))
        if "titles_flush_debounce" in patch:
            patch["titles_flush_debounce"] = max(
                0, min(120, float(patch["titles_flush_debounce"])))
    except (TypeError, ValueError):
        for k in ("titles_cache_max_entries", "titles_cache_max_mb",
                  "titles_flush_debounce"):
            patch.pop(k, None)
    if "update_proxy" in patch:
        pu = str(patch["update_proxy"]).strip()[:300]
        patch["update_proxy"] = pu if re.match(r"^(https?|socks5h?)://", pu) else ""
    if "pan_cache_ttl_hours" in patch:
        try:
            # 允许 1 小时 ~ 30 天
            patch["pan_cache_ttl_hours"] = max(
                1, min(720, int(float(patch["pan_cache_ttl_hours"]))))
        except (TypeError, ValueError):
            patch.pop("pan_cache_ttl_hours", None)
    if "pan_cache_cleanup_enabled" in patch:
        patch["pan_cache_cleanup_enabled"] = bool(
            patch["pan_cache_cleanup_enabled"])
    for _bk in ("direct_play_enabled", "direct_play_probe",
                "browse_fill_lines_enabled"):
        if _bk in patch:
            patch[_bk] = bool(patch[_bk])
    sources_changed = False
    if "sources" in body and isinstance(body["sources"], list):
        cur = {s["id"]: s for s in config.all_sources()}
        for ch in body["sources"]:
            sid = ch.get("id")
            if sid in cur:
                if isinstance(ch.get("enabled"), bool) and \
                        cur[sid].get("enabled") != ch["enabled"]:
                    cur[sid]["enabled"] = ch["enabled"]
                    sources_changed = True
                if isinstance(ch.get("standalone"), bool) and \
                        bool(cur[sid].get("standalone")) != ch["standalone"]:
                    cur[sid]["standalone"] = ch["standalone"] and cur[sid].get("kind") != "t4"
                    sources_changed = True
                if isinstance(ch.get("loose_filter"), bool):
                    cur[sid]["loose_filter"] = ch["loose_filter"]
                if isinstance(ch.get("adult"), bool):
                    cur[sid]["adult"] = ch["adult"] and bool(cur[sid].get("standalone"))
        patch["sources"] = list(cur.values())
    config.save(patch)
    _search_cache.clear(); _AGG_CACHE.clear()
    # 方案B 开关切换：即时启用/停用冠军线记忆持久化
    if "prewarm_mode" in patch:
        try:
            linepredict.set_persist(patch["prewarm_mode"] == "persist")
        except Exception:
            pass
    if sources_changed:
        _schedule_routes_refresh()
    return {"code": 0, "msg": "saved"}


@app.post("/v/api/ext/subscription-proxy")
async def ext_set_subscription_proxy(request: Request):
    """设置/清空「仅拉取订阅资源」的代理（订阅配置/spider jar/DRPY脚本）。

    播放、搜索、CMS 探测不走此代理。body: {"proxy":"http://user:pass@host:port"或""}
    会即时保存并用该代理探测一次（拉一个轻量 URL），返回连通结果。
    """
    body = await request.json()
    px = str(body.get("proxy", "") or "").strip()
    ok, msg = config.set_subscription_proxy(px)
    if not ok:
        return JSONResponse({"code": 400, "msg": msg}, status_code=400)
    # 实测连通：经该代理拉一个小资源（默认 GitHub generate_204），失败也保存但提示
    test_ok, test_msg = False, ""
    import subfetch
    if px:
        try:
            import httpx as _httpx
            with _httpx.Client(trust_env=False, verify=False, timeout=12,
                               proxy=px) as cli:
                rr = cli.get("https://www.gstatic.com/generate_204")
                test_ok = rr.status_code in (200, 204)
                test_msg = f"HTTP {rr.status_code}"
        except Exception as exc:  # noqa: BLE001
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": subfetch.subscription_proxy(),
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


@app.post("/v/api/ext/source-proxy")
async def ext_set_source_proxy(request: Request):
    """设置/清空「访问资源站取源」专用代理（搜索/分类/测速/解析；视频流仍直连）。

    issue #30：部分运营商直连资源站超时/被墙。保存后即时用该代理对一个内置源做
    一次轻量测速，返回连通结果（失败也保存，由用户自行决定）。"""
    body = await request.json()
    px = str(body.get("proxy", "") or "").strip()
    ok, msg = config.set_source_proxy(px)
    if not ok:
        return JSONResponse({"code": 400, "msg": msg}, status_code=400)
    # 代理模式：off/on/auto（缺省保持不变）
    if body.get("mode") is not None:
        mok, mmsg = config.set_source_proxy_mode(str(body.get("mode")))
        if not mok:
            return JSONResponse({"code": 400, "msg": mmsg}, status_code=400)
    mode = config.source_proxy_mode()
    # 切换模式或改代理时，清空 auto 学习到的"需代理主机"，避免旧判断残留
    netutil.clear_proxy_hosts()
    test_ok, test_msg = False, ""
    if px:
        try:
            import time as _t
            src = next((s for s in config.enabled_sources()
                        if s.get("api", "").startswith("http")), None)
            api = (src or {}).get("api", "")
            t0 = _t.monotonic()
            async with netutil.async_client(timeout=10.0, px=px) as cli:
                rr = await cli.get(api, params={"ac": "list", "pg": 1},
                                   headers={"User-Agent": "Mozilla/5.0"})
            ms = int((_t.monotonic() - t0) * 1000)
            test_ok = rr.status_code == 200
            test_msg = f"{(src or {}).get('name', api[:30])} HTTP {rr.status_code} / {ms}ms"
        except Exception as exc:  # noqa: BLE001
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": netutil.source_proxy(),
                     "mode": mode,
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


@app.post("/v/api/ext/junk-keywords")
async def ext_junk_keywords(request: Request):
    body = await request.json()
    if body.get("reset"):
        kws = config.reset_junk_keywords()
    else:
        kws = config.set_junk_keywords(body.get("keywords", []))
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "saved", "data": {"junk_keywords": kws}}


def _schedule_routes_refresh():
    try:
        if not catalog._refreshing:
            asyncio.create_task(catalog.refresh_routes(config.enabled_sources()))
    except Exception:
        pass


@app.post("/v/api/ext/source/add")
async def ext_source_add(request: Request):
    body = await request.json()
    ok, info = config.add_source(body.get("name", ""), body.get("api", ""),
                                 bool(body.get("enabled", True)),
                                 bool(body.get("standalone", False)))
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear(); _AGG_CACHE.clear()
    if body.get("standalone"):
        _schedule_routes_refresh()
    return {"code": 0, "msg": "added", "data": {"id": info}}


@app.post("/v/api/ext/source/update")
async def ext_source_update(request: Request):
    """编辑资源站名称 / 订阅(API)链接。自定义源可改链接（id 随地址哈希重建），
    内置源仅允许改名。改后清搜索缓存并刷新海量库路由。"""
    body = await request.json()
    sid = str(body.get("id", ""))
    cur = next((s for s in config.all_sources() if s["id"] == sid), None)
    if not cur:
        return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
    name = body.get("name") if body.get("name") is not None else None
    api = body.get("api") if body.get("api") is not None else None
    if api is not None and cur.get("guard"):
        return JSONResponse({"code": 400, "msg": "加固源订阅请在「TVBox 加固」页管理"},
                            status_code=400)
    try:
        ok, info = config.update_source(sid, name=name, api=api)
    except Exception as exc:
        return JSONResponse({"code": 500, "msg": str(exc)}, status_code=500)
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok", "data": {"id": info}}


@app.post("/v/api/ext/source/standalone")
async def ext_source_standalone(request: Request):
    body = await request.json()
    sid = body.get("id", "")
    on = bool(body.get("standalone"))
    target = next((s for s in config.all_sources() if s["id"] == sid), None)
    ok, info = config.set_standalone(sid, on)
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}


@app.post("/v/api/ext/source/loose-filter")
async def ext_source_loose(request: Request):
    body = await request.json()
    sid = body.get("id", "")
    on = bool(body.get("loose_filter"))
    ok, info = config.set_loose_filter(sid, on)
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    # 放宽规则变化要清掉该源作用域的页缓存，旧的过滤结果才不会残留
    try:
        with catalog._lock:
            stale = [k for k in catalog._page_cache if k[0] == sid]
        for k in stale:
            catalog._page_cache.pop(k, None)
            for buf in (catalog._dense, catalog._seen_guids,
                        catalog._scanned_page, catalog._dense_count):
                buf.pop(catalog._scope_key(sid, k[1]), None)
    except Exception:
        pass
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}




@app.post("/v/api/ext/source/delete")
async def ext_source_delete(request: Request):
    body = await request.json()
    ok, info = config.remove_source(body.get("id", ""))
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "deleted"}


@app.post("/v/api/ext/source/restore")
async def ext_source_restore(request: Request):
    config.restore_builtin()
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "restored"}


@app.post("/v/api/ext/source/test")
async def ext_source_test(request: Request):
    body = await request.json()
    api = (body.get("api") or "").strip()
    sid = body.get("id")
    src = next((s for s in config.all_sources() if s["id"] == sid), None)
    if not api and sid:
        if not src:
            return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
        api = src["api"]
    keyword = (body.get("keyword") or "庆余年").strip()
    result = await providers.probe_source(api, keyword=keyword, source=src)
    return {"code": 0, "msg": "", "data": result}


@app.post("/v/api/ext/source/test-all")
async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]（对齐 v0.2.42 管理台）。"""
    async def one(s):
        try:
            r = await providers.probe_source(
                s["api"], keyword="庆余年", source=s)
            if not isinstance(r, dict):
                r = {}
            r.update(id=s["id"], name=s["name"],
                     enabled=bool(s.get("enabled")),
                     builtin=bool(s.get("builtin")))
            return r
        except Exception:
            return {"id": s.get("id"), "name": s.get("name"),
                    "ok": False, "error": "探测异常",
                    "enabled": bool(s.get("enabled")),
                    "builtin": bool(s.get("builtin"))}
    results = await asyncio.gather(
        *[one(s) for s in config.all_sources()], return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    return {"code": 0, "msg": "", "data": out}


@app.post("/v/api/ext/source/health-check")
async def ext_source_health_check(request: Request):
    """用常见热门关键词批量检测纯 CMS 资源站有效性。

    任一热门词搜到结果即有效；全部搜不到才判失效，并把失效源写入源健康画像
    （record False 累积触发熔断），起播/竞速自动跳过。仅检测纯苹果 CMS 源，
    加固/爬虫/小雅等专有源不走此检测。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    # 限制并发，避免一次性打爆源站
    try:
        concurrency = max(1, min(8, int(body.get("concurrency") or 4)))
    except (TypeError, ValueError):
        concurrency = 4
    targets = [s for s in config.all_sources() if _is_pure_cms(s)]
    sem = asyncio.Semaphore(concurrency)

    async def one(s):
        async with sem:
            try:
                r = await providers.probe_source_alive(s)
            except Exception as exc:  # noqa: BLE001
                r = {"alive": False, "ok": False, "status": 0,
                     "elapsed_ms": 0, "count": 0, "sample": "",
                     "error": type(exc).__name__, "hit": "", "tried": []}
            sk = sourcehealth.source_key_of(s.get("id"), s) or s.get("id", "")
            # 回写源健康：失效累计失败（达阈值熔断），有效则清零失败计数
            if sk:
                sourcehealth.record(sk, bool(r.get("alive")),
                                    (r.get("elapsed_ms") or 0) / 1000.0)
            return {
                "id": s.get("id"), "name": s.get("name", ""),
                "alive": bool(r.get("alive")), "hit": r.get("hit", ""),
                "tried": r.get("tried", []), "count": r.get("count", 0),
                "elapsed_ms": r.get("elapsed_ms", 0),
                "status": r.get("status", 0), "error": r.get("error", ""),
                "dead": sourcehealth.is_dead(sk) if sk else False,
            }

    results = await asyncio.gather(*[one(s) for s in targets])
    alive_n = sum(1 for r in results if r["alive"])
    return {"code": 0, "msg": "", "data": {
        "total": len(results), "alive": alive_n,
        "dead": len(results) - alive_n,
        "keywords": list(providers.PROBE_HOT_KEYWORDS),
        "results": results}}


def _is_pure_cms(s: dict) -> bool:
    """纯苹果 CMS 源（非 jar 爬虫 / 加固 / 小雅 / dduo / DRPY / T4）。"""
    if s.get("guard") or s.get("spider_src") or s.get("catvod") \
            or s.get("xsrc") or s.get("drpy") or s.get("t4"):
        return False
    if str(s.get("id", "")).startswith(("spider-", "guard-", "drpy-", "t4-")):
        return False
    return bool(str(s.get("api", "")).startswith(("http://", "https://")))


@app.get("/v/api/ext/source/export")
async def ext_source_export():
    """导出纯 CMS 源为可移植 JSON（只含名称/接口/启用/独立库/非过滤等通用字段，
    不含 id/内置标记，便于跨设备导入；jar/加固/小雅等不属本页范围）。"""
    out = []
    for s in config.all_sources():
        if not _is_pure_cms(s):
            continue
        out.append({
            "name": s.get("name", ""),
            "api": s.get("api", ""),
            "enabled": bool(s.get("enabled", True)),
            "standalone": bool(s.get("standalone", False)),
            "loose_filter": bool(s.get("loose_filter", False)),
        })
    return {"code": 0, "msg": "", "data": {"version": 1, "sources": out}}


@app.post("/v/api/ext/source/import-file")
async def ext_source_import_file(request: Request):
    """从导出的 JSON（或含 sources 数组的兼容结构）批量导入纯 CMS 源。
    自动去重（按规范化 API），返回新增/跳过计数；jar/爬虫/加固条目忽略并计数。"""
    body = await request.json()
    raw = body.get("sources")
    if raw is None and isinstance(body.get("data"), dict):
        raw = body["data"].get("sources")
    if not isinstance(raw, list):
        return JSONResponse({"code": 400, "msg": "JSON 需含 sources 数组"},
                            status_code=400)
    added, skipped, noncms = 0, 0, 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        api = str(it.get("api", "")).strip()
        name = str(it.get("name", "")).strip()
        if not api.startswith(("http://", "https://")):
            noncms += 1
            continue
        # 兼容 TVBox 形态：带 jar/csp/type!=0/1 的不当作纯 CMS
        if it.get("jar") or str(it.get("type", "")).strip() not in ("", "0", "1", 0, 1):
            noncms += 1
            continue
        ok, _info = config.add_source(
            name, api, bool(it.get("enabled", True)),
            bool(it.get("standalone", False)))
        if ok:
            added += 1
            if it.get("loose_filter"):
                try:
                    sid = config.make_custom_id(api)
                    config.save({sid: {"loose_filter": True}})
                except Exception:
                    pass
        else:
            skipped += 1
    _search_cache.clear(); _AGG_CACHE.clear()
    if added:
        _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {"added": added, "skipped": skipped,
                                            "ignored_noncms": noncms}}



async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]。"""
    async def one(s):
        r = await providers.probe_source(s["api"], source=s)
        r.update(id=s["id"], name=s["name"], enabled=bool(s.get("enabled")),
                 builtin=bool(s.get("builtin")))
        return r
    results = await asyncio.gather(*[one(s) for s in config.all_sources()],
                                   return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    return {"code": 0, "msg": "", "data": out}


@app.post("/v/api/ext/source/import-tvbox")
async def ext_import_tvbox(request: Request):
    """导入 TVBox 配置中的 CMS 与远程 T4 源。

    入参：{url?} 或 {text?}，外加 standalone（可选）。对候选源做轻量探测，
    CMS 探测通过后导入；T4 只登记来源，不预运行插件。
    """
    import tvbox as tvboxmod
    body = await request.json()
    standalone = bool(body.get("standalone"))
    try:
        if body.get("url"):
            sites, _subs = await tvboxmod.load_remote(str(body["url"]).strip())
        elif body.get("text"):
            cfg = tvboxmod.parse_config_text(str(body["text"]))
            if not cfg:
                return JSONResponse({"code": 400, "msg": "无法解析配置（需为 JSON 或其 base64）"},
                                    status_code=400)
            sites = tvboxmod.extract_sites(cfg)
        else:
            return JSONResponse({"code": 400, "msg": "缺少 url 或 text"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"code": 502, "msg": f"获取配置失败：{type(exc).__name__}"},
                            status_code=502)

    candidates: list[dict] = []
    t4_candidates: list[dict] = []
    seen_api: set[str] = set()
    skipped: list[str] = []
    for s in sites:
        status, norm = tvboxmod.classify_site(s)
        if status == "add_t4":
            t4_candidates.append(norm)
            continue
        if not norm:
            nm = str(s.get("name") or s.get("key") or s.get("api") or "未命名")
            if nm not in skipped:
                skipped.append(nm[:20])
            continue
        key = norm["api"].rstrip("/").lower()
        if key in seen_api:
            continue
        seen_api.add(key)
        candidates.append(norm)

    existing = {s.get("api", "").rstrip("/").lower() for s in config.all_sources()}
    added_names, dup = [], 0
    # 并发探测，最多导入 40 个可用源，避免一次性拉爆
    sem = asyncio.Semaphore(8)

    async def check(c):
        async with sem:
            try:
                probe = await asyncio.wait_for(
                    providers.probe_source(c["api"], timeout=8.0), timeout=10)
            except Exception:
                probe = {"ok": False}
            return c, probe

    checked = await asyncio.gather(*[check(c) for c in candidates[:60]])
    for c, probe in checked:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            dup += 1
            continue
        if not probe.get("ok"):
            skipped.append(c["name"][:20])
            continue
        ok, info = config.add_source(c["name"], c["api"], True, standalone)
        if ok:
            added_names.append(c["name"])
            existing.add(key)
        else:
            dup += 1
        if len(added_names) >= 40:
            break
    t4_report = config.import_t4_sources(t4_candidates)
    if added_names or t4_report["added"]:
        _search_cache.clear(); _AGG_CACHE.clear()
        if standalone:
            _schedule_routes_refresh()
        else:
            # 新源加入后也刷新一次聚合路由，便于海量库覆盖
            _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {
        "scanned": len(sites),
        "candidates": len(candidates),
        "added": len(added_names) + t4_report["added"],
        "added_names": added_names + t4_report["added_names"],
        "t4": t4_report,
        "duplicate": dup,
        "skipped": len(skipped),
        "skipped_detail": skipped[:40],
    }}


@app.post("/v/api/ext/source/import-subscription")
async def ext_import_subscription(request: Request):
    """统一导入 TVBox / 影视仓 / OK影视 订阅（一个入口，自动分类、导入即可用）。

    入参：{url?} 或 {text?}，可选 standalone（CMS 源是否同时作独立媒体库）、
    enable_guard（True/False/"smart"，默认 "smart"：安全加固源自动启用、
    P2P/磁力/云盘/需外部配置的风险源导入但默认关）。

    一份订阅一次解析，按运行时分流：
      - 标准苹果CMS（type 0/1）：轻量探测通过后加入自定义源，立即可搜可播；
      - csp_*Guard 加固源：写入私有 imported.json 并同步进源底表（仅完整版）；
      - 其余爬虫 / 网盘 / 直播：跳过并给出原因。
    返回分类报告 + 加固运行时就绪状态（未就绪时引导一键部署，源仍已登记）。
    """
    import tvbox as tvboxmod
    body = await request.json()
    standalone = bool(body.get("standalone"))
    enable_guard = body.get("enable_guard", "smart")
    if enable_guard not in (True, False, "smart"):
        enable_guard = "smart"

    try:
        if body.get("builtin"):
            # 内置推荐订阅（如一键导入饭太硬）：在 tvbox 内完成多镜像尝试与解密。
            _builtin = str(body["builtin"]).strip().lower()
            if _builtin == "fantaiying":
                raw_sites, _spider, _base, _used = await tvboxmod.load_fantaiying()
                _subs, _rules = 0, []
            elif _builtin in ("cat_lite", "catlite", "mao_lite"):
                # 内置轻量猫源：纯苹果CMS，无 jar/爬虫，零运行时，full/lite 通用。
                raw_sites, _spider, _base, _used = tvboxmod.load_cat_lite_sync()
                _subs, _rules = 0, []
            else:
                return JSONResponse({"code": 400, "msg": f"未知内置订阅：{_builtin}"},
                                    status_code=400)
        elif body.get("url"):
            raw_sites, _subs, _spider, _base, _rules = await tvboxmod.load_subscription_full(
                url=str(body["url"]).strip())
        elif body.get("text"):
            raw_sites, _subs, _spider, _base, _rules = await tvboxmod.load_subscription_full(
                text=str(body["text"]))
        else:
            return JSONResponse({"code": 400, "msg": "缺少 url 或 text"},
                                status_code=400)
    except Exception as exc:
        return JSONResponse({"code": 502,
                             "msg": f"获取/解析订阅失败：{type(exc).__name__}"},
                            status_code=502)

    # 合并订阅自带的 m3u8 去广告规则（TVBox/OK影视 Pro rules），去重持久化
    ad_rules_added = 0
    try:
        import adfilter as _adfilter
        ad_rules_added = _adfilter.merge_subscription_rules(_rules or [])
    except Exception:
        _log.info("merge ad rules failed", exc_info=True)

    groups = tvboxmod.classify_subscription(raw_sites, _spider, _base)
    t4_report = config.import_t4_sources(groups.get("t4", []))
    cms_cands = groups["cms"]
    guard_sites = groups["guard"]
    csp_items = groups.get("csp", [])
    drpy_items = groups.get("drpy", [])
    jar_url_global = groups.get("jar_url", "")
    skipped = list(groups["skipped"])

    # ---------- 苹果 CMS：并发探测，通过即加入并启用 ----------
    existing = {s.get("api", "").rstrip("/").lower() for s in config.all_sources()}
    cms_added, cms_dup = [], 0
    cms_failed: list[str] = []
    sem = asyncio.Semaphore(8)

    async def _probe(c):
        async with sem:
            try:
                pr = await asyncio.wait_for(
                    providers.probe_source(c["api"], timeout=8.0), timeout=10)
            except Exception:
                pr = {"ok": False}
            return c, pr

    checked = await asyncio.gather(*[_probe(c) for c in cms_cands[:80]])
    for c, pr in checked:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            cms_dup += 1
            continue
        if not pr.get("ok"):
            cms_failed.append(c["name"][:20])
            continue
        ok, _info = config.add_source(c["name"], c["api"], True, standalone)
        if ok:
            cms_added.append(c["name"])
            existing.add(key)
        else:
            cms_dup += 1
        if len(cms_added) >= 60:
            break
    for nm in cms_failed:
        tag = nm + "|探测不可达"
        if not any(x.get("name") == nm and x.get("reason") == "探测不可达"
                   for x in skipped):
            skipped.append({"name": nm, "reason": "探测不可达"})

    # ---------- csp_*Guard 加固源（仅完整私有构建） ----------
    guard_report = None
    runtime = None

    # ---------- 普通 csp_ 爬虫 与 远程 drpyS（仅完整私有构建） ----------
    csp_report = None
    drpy_report = None

    if cms_added or t4_report["added"] or (guard_report and guard_report.get("enabled_count")) \
            or (csp_report and csp_report.get("added")) \
            or (drpy_report and drpy_report.get("added")):
        _search_cache.clear(); _AGG_CACHE.clear()
        _schedule_routes_refresh()

    return {"code": 0, "msg": "", "data": {
        "scanned": len(raw_sites),
        "subscriptions": _subs,
        "t4": t4_report,
        "cms": {
            "candidates": len(cms_cands),
            "added": len(cms_added),
            "added_names": cms_added,
            "duplicate": cms_dup,
        },
        "guard": guard_report,
        "csp": csp_report,
        "drpy": drpy_report,
        "runtime": runtime,
        "skipped": len(skipped),
        "skipped_detail": skipped[:60],
        "ad_rules_added": ad_rules_added,
    }}


@app.get("/v/api/ext/adfilter/status")
async def ext_adfilter_status():
    """m3u8 去广告状态：总开关 + 动态检测 + 已合并的订阅规则组数/域名数。"""
    import adfilter as _adf
    st = _adf.stats()
    st["dynamic"] = _adf._dynamic_enabled()
    st["sps"] = _adf.sps_enabled()
    # 附带各规则组的名称/域名，便于管理页展示
    st["groups"] = [{"name": g.get("name", ""),
                     "hosts": g.get("hosts", []),
                     "regex": len(g.get("regex", []))}
                    for g in _adf._load_rules()]
    return {"code": 0, "msg": "", "data": st}


@app.post("/v/api/ext/adfilter/toggle")
async def ext_adfilter_toggle(request: Request):
    """开关 m3u8 去广告。body: {"enabled":bool} / {"dynamic":bool} / {"sps":bool}"""
    import adfilter as _adf
    body = await request.json()
    if "dynamic" in body:
        config.save({"ad_filter_dynamic": bool(body.get("dynamic"))})
    if "sps" in body:
        config.save({"ad_filter_sps": bool(body.get("sps"))})
        if not bool(body.get("sps")):
            _adf.reset_sps_cache()
    if "enabled" in body:
        _adf.set_enabled(bool(body.get("enabled")))
    return {"code": 0, "msg": "",
            "data": {"enabled": _adf.enabled(),
                     "dynamic": _adf._dynamic_enabled(),
                     "sps": _adf.sps_enabled()}}


@app.post("/v/api/ext/adfilter/rules")
async def ext_adfilter_replace_rules(request: Request):
    """整体替换去广告规则（高级）。body: {"rules":[{name,hosts[],regex[]}]}"""
    import adfilter as _adf
    body = await request.json()
    n = _adf.replace_rules(body.get("rules") or [])
    return {"code": 0, "msg": "", "data": {"groups": n}}


@app.get("/v/online-admin")
async def admin_page():
    from admin_ui import ADMIN_HTML
    return Response(
        ADMIN_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"})


@app.get("/v/ext/alipay-qr")
async def alipay_qr():
    """关于页支付宝收款码（随包静态图，full/lite 通用）。"""
    qr = Path(__file__).resolve().parent / "assets" / "alipay.jpg"
    if not qr.exists():
        return JSONResponse({"code": 404, "msg": "no qr"}, status_code=404)
    return FileResponse(qr, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/v/ext/wechat-qr")
async def wechat_qr():
    """关于页微信赞赏码（随包静态图，full/lite 通用）。"""
    qr = Path(__file__).resolve().parent / "assets" / "wechat-qr.jpg"
    if not qr.exists():
        return JSONResponse({"code": 404, "msg": "no qr"}, status_code=404)
    return FileResponse(qr, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})






# ---------------- 在线源健康画像（成功率/延迟，自动降权，通用能力） ----------------
@app.get("/v/api/ext/source-health")
async def source_health():
    snap = sourcehealth.snapshot()
    # 按健康等级、成功率、延迟排序输出，便于管理页展示
    rows = sorted(
        snap.values(),
        key=lambda s: (s.get("grade", 1),
                       -s.get("success_rate", 0),
                       s.get("avg_latency", 99)))
    return JSONResponse({"code": 0, "data": {"sources": rows}})


@app.post("/v/api/ext/source-health/reset")
async def source_health_reset(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sourcehealth.reset((body or {}).get("key"))
    return JSONResponse({"code": 0})


# ---------------- 本片线路预测（linepredict）：海报预热→本片最快线→秒播 ----------------
@app.get("/v/api/ext/linepredict")
async def linepredict_status():
    """返回当前已预测的本片最快线数与在途预热数（调试/管理页用）。"""
    return JSONResponse({"code": 0, "data": {
        "remembered": linepredict.count(),
        "inflight": linepredict.inflight_count(),
        "recent": [dict(r, title=(store.get_title_by_guid(r.get("og", "")) or {}
                                  ).get("title", r.get("og", "")[:12]))
                   for r in linepredict.snapshot(40)],
        "config": {
            "enabled": bool(config.get().get("linepredict_enabled", True)),
            "max_lines": int(config.get().get("linepredict_max_lines", 2) or 2),
            "ttl_min": int(config.get().get("linepredict_ttl_min", 30) or 30),
            "prewarm_mode": str(config.get().get("prewarm_mode", "realtime")),
            "chart_bindings": chartbind.bind_store.status()}}})


@app.post("/v/api/ext/linepredict/config")
async def linepredict_config(request: Request):
    body = await request.json()
    patch = {}
    if "enabled" in body:
        patch["linepredict_enabled"] = bool(body["enabled"])
    if "max_lines" in body:
        try:
            patch["linepredict_max_lines"] = min(3, max(1, int(body["max_lines"])))
        except (TypeError, ValueError):
            pass
    if patch:
        config.save(patch)
    return JSONResponse({"code": 0, "data": patch})


@app.post("/v/api/ext/linepredict/reset")
async def linepredict_reset():
    linepredict.reset()
    _LINE_FAST.clear()
    return JSONResponse({"code": 0})




# ------------------------------------------------------------- 兜底透传

@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    # 本地视频/文件等大响应必须流式透传（不缓冲整文件），避免打满内存拖死系统；
    # 该函数内部对普通 JSON 自动回落到缓冲透传。
    return await forward_upstream_streaming(request)
