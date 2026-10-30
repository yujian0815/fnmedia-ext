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
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)

import features
import providers
import sourcehealth
import client as clientmod
import live as livemod
import inject as inj
import config as cfgmod
from config import config
import hot as hotmod
from hot import hot_cache
import charts as chartmod
from charts import chart_cache
import catalog as catalogmod
from catalog import catalog
import history as histmod
from history import history
import marks as marksmod
from marks import marks
import updater
from store import (store, make_guid, media_guid, video_guid, is_online_guid,
                   season_guid, episode_guid, ep_media_guid, ep_video_guid)

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


def standalone_lib_name(src: dict) -> str:
    base = "📡" + (src.get("name") or src.get("id") or "")
    return base






def charts_enabled() -> bool:
    return bool(config.get().get("hot_library_enabled", True))


def catalog_enabled() -> bool:
    """海量四库独立开关（与热门榜单互不影响）。"""
    return bool(config.get().get("catalog_library_enabled", True))


def any_virtual_lib_enabled() -> bool:
    if charts_enabled() or catalog_enabled():
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
    cat = item["cat"]
    typ = "Movie" if cat == "movie" else "TV"
    year = item.get("year") or ""
    rating = item.get("rating") or 0
    tags = item.get("tags") or []
    overview = item.get("desc") or ("、".join(tags) if tags else "")
    title = item["title"]
    guid = item["guid"]
    return {
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
        "poster": inj._poster(item.get("cover")),
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


def start_resolve_chart(guid: str) -> asyncio.Future | None:
    """启动榜单 guid 的后台解析（单飞），立即返回在途 Future；不阻塞当前请求。"""
    existing = store.get_title_by_guid(guid)
    if existing:
        return None
    if time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL:
        return None
    fut = _chart_resolving.get(guid)
    if fut is not None:
        return fut
    fut = asyncio.get_event_loop().create_future()
    _chart_resolving[guid] = fut

    async def work():
        try:
            item = chart_cache.get_guid(guid)
            if not item:
                fut.set_result(None)
                return
            briefs = await asyncio.wait_for(
                _aggregate_briefs(item["title"], 12), timeout=25)
            # 相关性：归一化片名必须包含完整榜单片名（分词全部命中），否则视为无源
            want = _norm_title(item["title"])
            want_tokens = [_norm_title(t) for t in re.split(r"[\s]+", item["title"]) if _norm_title(t)]
            best = None
            for b in briefs:
                n = _norm_title(b.get("title", ""))
                if n and (want in n or (len(want_tokens) > 1
                                        and all(tok in n for tok in want_tokens))):
                    best = b
                    break
            if best is None and briefs:
                # 宽松兜底：取线路最多者（片名归一后高度重叠）
                best = briefs[0]
            if best is not None:
                # 关键：把可播放 brief 的身份换成 chart guid，使播放链挂到榜单卡片
                bound = dict(best)
                bound["guid"] = guid
                bound["chart"] = {"title": item["title"], "cat": item["cat"],
                                  "cover": item.get("cover", ""),
                                  "rating": item.get("rating", 0)}
                _chart_resolved[guid] = bound
                store.remember_merged([bound])
                _PENDING_MEDIA_CHART.pop(media_guid(guid), None)
                _CHART_FAILED_TS.pop(guid, None)
            else:
                _CHART_FAILED_TS[guid] = time.time()
            fut.set_result(best)
        except Exception:
            fut.set_result(None)
        finally:
            _chart_resolving.pop(guid, None)

    asyncio.create_task(work())
    return fut


async def resolve_chart(guid: str) -> dict | None:
    """用榜单片名去 CMS 聚合搜索，把最匹配的可播放 brief 挂到该 chart guid。

    单 flight；命中后在内存和 store 都登记，之后整条 season/episode/play 链复用。
    """
    existing = store.get_title_by_guid(guid)
    if existing:
        return existing
    fut = start_resolve_chart(guid)
    if fut is None:
        return store.get_title_by_guid(guid)
    return await fut


def chart_guid_by_media(mg: str) -> str:
    """识别 media_guid 是否属于榜单/热门片（包括秒进时尚未绑定完成的）。"""
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    return og if og and chart_cache.is_chart_guid(og) else ""


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.up = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=UPSTREAM_SOCK),
        timeout=httpx.Timeout(120), trust_env=False)
    app.state.ext = httpx.AsyncClient(
        timeout=httpx.Timeout(30), trust_env=False, follow_redirects=True)
    app.state.stop_hot = asyncio.Event()
    app.state.hot_task = asyncio.create_task(_hot_worker(app))

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
        await app.state.up.aclose()
        await app.state.ext.aclose()


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
                # 路由重建后后台预热聚合四库前段，进入媒体库/滚动即时有内容
                asyncio.create_task(catalog.warm_aggregate())
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


@app.middleware("http")
async def _access(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if "/api/" in p or p.startswith(ONLINE_PREFIX) or p.startswith("/v/media"):
        _log.info("%s %s -> %s", request.method,
                  (p + ("?" + request.url.query if request.url.query else ""))[:120],
                  resp.status_code)
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
                       request.method, path, request.url.query, status,
                       origin, title=title, stage=stage, note=note)
    except Exception:
        pass  # 诊断本身绝不影响正常请求


def up_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.up


def ext_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.ext


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
                            fast_only: bool = False) -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。
    fast_only=True 时只查秒回的标准 CMS 直链源（跳过加固/drpy 等慢源），供播放
    失败时快速回源，避免被 redroid 慢源拖到网关超时。"""
    # 可选特性源不进入全局搜索（公开构建下该开关恒关，等价于不过滤）。
    sources = [s for s in config.enabled_sources()
               if not (features.ADULT and s.get("adult"))]
    if fast_only:
        sources = [s for s in sources
                   if not s.get("guard") and not s.get("spider_src")
                   and not str(s.get("id", "")).startswith(("spider-", "drpy-"))
                   and s.get("api")
                   and s.get("id") not in ("dduo", "xiaoya")]
    titles = await providers.search_all(keyword, sources)
    if maxn is None:
        maxn = int(config.get().get("max_per_keyword", 12))
    # 跨源同名合并：同 规范化片名+年份 聚成一个条目，每个来源各成一条
    # 播放线路（供播放器「切换线路」）；过滤解说/预告/无直链。
    loose_ids = {s["id"] for s in sources if s.get("loose_filter")}
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
        if year:
            return (ntitle, year)
        votes = year_votes.get(ntitle)
        if votes:
            best_year = max(votes, key=votes.get)
            return (ntitle, best_year)
        return (ntitle, year)

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
    # 排序：热门资源优先高质量源（源质量权重），同权重再按可播直链总数（跨线路）降序
    prefer_q = bool(config.get().get("prefer_quality_source", True))
    overrides = config.get().get("source_priority") or {}

    def _group_key(g) -> tuple:
        lines_n = sum(len(v) for v in g["lines"].values())
        if not prefer_q:
            return (0, lines_n)
        pr = providers.source_priority({"id": g["rep"].provider}, overrides)
        return (pr, lines_n)

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
    return store.remember_merged(briefs)


async def online_search(keyword: str, client: httpx.AsyncClient) -> list:
    now = time.time()
    if keyword in _search_cache and now - _search_cache[keyword][0] < CACHE_TTL:
        return _search_cache[keyword][1]
    fut = _search_inflight.get(keyword)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _search_inflight[keyword] = fut

        async def work():
            try:
                briefs = await _aggregate_briefs(keyword)
                items = [inj.search_item(b) for b in briefs]
                _search_cache[keyword] = (time.time(), items)
                if not fut.done():
                    fut.set_result(items)
            except Exception:
                # 客户端断连会让 fut 被取消，此时再 set_result 会抛 InvalidStateError
                if not fut.done():
                    fut.set_result([])
            finally:
                _search_inflight.pop(keyword, None)

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
    inj.set_badge_provider(lambda b: _brief_line_flags(b)[:2])
except Exception:  # noqa: BLE001
    pass


def _brief_poster_badged(brief: dict) -> str:
    """按 brief 线路的网盘/健康情况生成带角标的海报路径。"""
    cover = brief.get("poster") or ""
    if not cover:
        return ""
    pan_label, grade, _ = _brief_line_flags(brief)
    try:
        return inj._poster_badged(cover, pan_label, grade)
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
    return row


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
            rows = [r for r in rows if r.get("type") in media_wanted]
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
            rows = [r for r in rows if r.get("type") in wanted]

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
            rows = [r for r in rows if r.get("type") in media_wanted]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows if r.get("type") in wanted]
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
    return row


def _catalog_filter(rows: list[dict], body: dict) -> list[dict]:
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        mw = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in mw:
            mw.discard("Movie,TV"); mw.update({"Movie", "TV"})
        if mw:
            rows = [r for r in rows if r.get("type") in mw]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows if r.get("type") in wanted]
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
    # 热门四库与海量四库各有独立开关；两类都关时才完全不注入（单源独立库/加固专区
    # 仍应展示，故这里只判断“聚合四库”开关）。
    if not any_virtual_lib_enabled():
        return upstream
    payload = await _json_from_upstream(upstream)
    dbs = payload.get("data") if payload else None
    vlib_guids = (set(CHART_LIB_GUIDS.values()) | set(CATALOG_LIB_GUIDS.values())
                  | {standalone_lib_guid(s["id"]) for s in config.all_sources()})
    # 官方该接口偶发鉴权/抖动失败（TV 端会整体“加载失败”）。只要拿到过有效列表，
    # 或本次列表为空，都用本地虚拟库补全；官方失败则只返回我们的虚拟库，不把错误透传给 App。
    if isinstance(dbs, list) and any(d.get("guid") in vlib_guids for d in dbs):
        return upstream
    if not isinstance(dbs, list):
        if not (payload and payload.get("code") == 0):
            dbs = []  # 官方失败：以我们的虚拟库兜底
        else:
            return upstream
    try:
        inserts = []
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
        # —— 置顶控制 ——
        # 置顶库（pinned_libs）插到列表最前并给较大 sort_num；未置顶的虚拟库仍排在
        # 官方本地库之前，但置于置顶组之后。客户端按返回顺序/ sort_num 展示。
        pinned_set = config.pinned_lib_guids()
        pinned = [d for d in inserts if d["guid"] in pinned_set]
        normal = [d for d in inserts if d["guid"] not in pinned_set]
        for i, d in enumerate(pinned):
            d["sort_num"] = 10_000_000 - i
        for d in normal:
            d["sort_num"] = 0
        dbs[0:0] = normal
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
    """App（Flutter）打开媒体库前会用 /mdb/<guid> 校验库是否存在。

    网页版不调此接口，所以此前仅网页能打开虚拟库；这里为 8 个虚拟库返回与
    官方同构的库元数据，否则官方后端返回 code=-6，App 提示「此媒体库不存在」。
    """
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






@app.post("/v/api/v1/item/list")
async def item_list(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        body = {}
    aguid = body.get("ancestor_guid")
    client = _client_of(request)

    # —— TVBox 加固源专区库 ——

    # —— 🛡加固影视：OK影视 式目录树（源→分类→无限海报墙），只读后台缓存，即时返回 ——

    # —— 小雅分类媒体库 ——

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
            rows.append(row)
        rows = clientmod.normalize_cards(rows, client)
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
    if q and _should_online(q):
        try:
            online = await asyncio.wait_for(online_search(q, ext_client(request)),
                                            timeout=6.0)
        except asyncio.TimeoutError:
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

@app.get("/v/api/v1/item/{guid}")
async def item_detail(guid: str, request: Request):
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
        if bound is None and not (time.time() - _CHART_FAILED_TS.get(guid, 0)
                                  < _CHART_FAILED_TTL):
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
        failed = time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL
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
        data = inj._base_item(
            guid=guid, type="Episode", title=ep["ep_title"],
            parent_guid=ep["season_guid"], parent_title="第 1 季",
            tv_title=ep["title"], season_number=1, episode_number=ep["index"] + 1,
            number_of_seasons=1, can_play=1,
            media_guid=ep_media_guid(guid), video_guid=ep_video_guid(guid))
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    from store import season_guid as _sg
    # 反查 season guid
    tv = None
    for g in store._titles:
        if _sg(g) == guid:
            tv = g
            break
    if tv:
        brief = store.get_title_by_guid(tv)
        data = inj._base_item(
            guid=guid, type="Season", title="第 1 季", parent_guid=tv,
            parent_title=brief["title"], tv_title=brief["title"],
            season_number=1, number_of_seasons=1,
            number_of_episodes=store.episode_count(tv), can_play=None,
            poster=inj._poster(brief.get("poster")))
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
    brief = store.get_title_by_guid(guid)
    if not brief or not inj.is_tv(brief):
        return await forward_upstream(request)
    client = _client_of(request)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(inj.season_list(brief), client)})


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_list_route(guid: str, request: Request):
    # guid 可能是伪造的 season guid，也可能是 tv guid
    tv = None
    from store import season_guid as _sg
    for g in store._titles:
        if _sg(g) == guid:
            tv = g
            break
    if not tv:
        brief = store.get_title_by_guid(guid)
        if brief and inj.is_tv(brief):
            tv = guid
    if not tv:
        return await forward_upstream(request)
    client = _client_of(request)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(
                             inj.episode_list(store.get_title_by_guid(tv)), client)})


@app.get("/v/api/v1/stream/list/{guid}")
async def stream_list(guid: str, request: Request):
    ep = store.get_episode(guid)
    target = None
    if ep:
        sl = inj.episode_stream_list(ep)
        target = store.get_episode_target(guid)
    else:
        brief = store.get_title_by_guid(guid)
        if not brief and chart_cache.is_chart_guid(guid):
            if time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL:
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
            egs = store.tv_episode_guids(guid)
            if egs:
                target = store.get_episode_target(egs[0])
        else:
            target = store.resolve_media_guid(media_guid(guid))
    quals = _qualities(target) if target else []
    if quals:
        sl["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
        sl["video_streams"][0]["bps"] = quals[0]["bitrate"]
        sl["qualities"] = quals
        sl["cloud_storage_info"] = None
    # 打开详情/线路菜单即后台预热默认线路播放列表（单飞，真正取 main 时并入）
    if target:
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


def _is_online_item_guid(guid: str) -> bool:
    if not guid:
        return False
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
    marks.add_favorite(guid, _mark_extra(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.delete("/v/api/v1/item/favorite")
async def item_favorite_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_favorite(guid)
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
    if want == 0:
        marks.remove_watched(guid)
    else:
        marks.set_watched(guid, _mark_extra(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.delete("/v/api/v1/item/watched")
async def item_watched_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = (body.get("guid") or body.get("item_guid") or "").strip()
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_watched(guid)
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
        ep_guids = store.tv_episode_guids(guid)
        if ep_guids:
            target_eg, resume_ts = history.resume_episode(ep_guids)
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


@app.post("/v/api/v1/play/info")
async def play_info(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    guid = body.get("item_guid", "")
    client = _client_of(request)


    def _prewarm_from_info(info: dict):
        """点击播放即后台预热默认线路的播放列表，缩短首帧等待。"""
        try:
            mg = info.get("media_guid")
            if not mg:
                return
            picked = _pick_episode(mg, request)
            if picked:
                _warm_playlist(mg, picked, request)
        except Exception:
            pass

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


def _line_choices(target: dict) -> list[dict]:
    """该播放目标可切换的线路：按资源站去重（同一站直链优先于 share 页），
    当前播放线路排第一。"""
    raw = store.target_lines(target)
    cur = target.get("line")
    # 当前线路置顶，其余直链在前、可解析页在后
    raw.sort(key=lambda t: (t["line"] != cur, not t["m3u8"]
                            and not re.search(r"\.mp4(\?|$)", t["url"], re.I),
                            t.get("resolve", False)))
    out, seen_provider, seen_name = [], set(), set()
    for t in raw:
        provider = t["line"].split("::", 1)[0]
        name = _PROVIDER_CN.get(provider, provider)
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
    # 保持当前线路第一
    out.sort(key=lambda t: t["line"] != cur)
    return out


def _line_bitrate(line: str, lines: list[str]) -> int:
    # 用 9000 万段的稳定整数编码线路，避免与真实码率（通常<3000万）冲突
    try:
        idx = lines.index(line)
    except ValueError:
        idx = 0
    return 90_000_000 + idx * 10001


def _line_of_bitrate(br, lines: list[str]) -> str | None:
    try:
        b = int(br or 0)
    except (TypeError, ValueError):
        return None
    if 90_000_000 <= b < 91_000_000:
        idx = (b - 90_000_000) // 10001
        if 0 <= idx < len(lines):
            return lines[idx]
    return None


def _qualities(target: dict) -> list[dict]:
    """把每条播放线路表达成播放器「清晰度」菜单里的一个可选项。"""
    lines = _line_choices(target)
    if len(lines) <= 1:
        return []
    out = []
    for i, t in enumerate(lines):
        out.append({
            "resolution": t.get("label") or _friendly_line(t["line"]),
            "bitrate": 90_000_000 + i * 10001,
            "progressive": True,
            "is_m3u8": False,  # 置 False 才不会被菜单项置灰
        })
    return out


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    target = store.resolve_media_guid(mg)
    if not target and chart_guid_by_media(mg):
        # 网页播放器会在进入播放页前连续请求 stream；这里必须瞬时返回，不能等待搜源，
        # 否则三个请求串起来仍会把用户留在海报墙。真实匹配推迟到播放器请求 preset。
        start_resolve_chart(chart_guid_by_media(mg))
        return _instant_stream_response(mg)
    if not target:
        return await forward_upstream(request)
    if target.get("kind") == "episode":
        sl = inj.episode_stream_list(target)
    else:
        brief = store.get_title_by_guid(target["online_guid"])
        sl = inj.stream_list(brief)
    f = sl["files"][0]
    v = sl["video_streams"][0]
    quals = _qualities(target)
    if quals:
        # 默认选中第一条线路，让播放器初始分辨率/码率与之一致
        v["resolution_type"] = quals[0]["resolution"]
        v["bps"] = quals[0]["bitrate"]
    return JSONResponse({"code": 0, "msg": "", "data": {
        "file_stream": f, "video_stream": v,
        "audio_streams": [], "subtitle_streams": [],
        "qualities": quals, "cloud_storage_info": None}})


@app.get("/v/api/v1/media/range/{mg}")
async def media_range(mg: str, request: Request):
    """App（Flutter 原生播放器）不走 /play/play，而是直接 GET /media/range/<mg>。

    官方后端对我们的虚拟 mg 无本地文件记录，会返回 403（App 提示“文件权限不足”）。
    这里识别在线 mg：按实际源类型把它引导到既有的 HLS 或直链代理（302），
    非在线 mg 原样透传官方。
    """
    target = store.resolve_media_guid(mg)
    chart_g = "" if target else chart_guid_by_media(mg)
    if not target and chart_g:
        if time.time() - _CHART_FAILED_TS.get(chart_g, 0) < _CHART_FAILED_TTL:
            return Response("暂无可用在线片源", status_code=503)
        # App 原生播放器也不等待搜索：立刻进入播放器，preset 内部再等待后台匹配
        start_resolve_chart(chart_g)
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index=0",
                                status_code=302)
    if not target:
        # 本地真实媒体字节（Flutter 原生播放器 Range 请求）：必须流式透传，
        # 不能整包缓冲几 GB 的影片，否则打满内存/swap 拖死整机。
        return await forward_upstream_streaming(request)
    # 同样【不在此同步解析】（会卡 App 起播、像点了没反应）：先瞬时 302 到对应代理，
    # 由 preset/main、direct 内部在播放加载阶段解析，并有双向 302 兜底。
    _wrapper, is_hls = _guess_wrapper(target)
    q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
    if is_hls:
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
    base_target = store.resolve_media_guid(mg)
    if not base_target and chart_guid_by_media(mg):
        # 未绑定榜单片：play/play 也立即返回占位 HLS，播放器先进入加载页；
        # preset/main 请求时再等待后台匹配，避免 play/info+stream+play 串行等待。
        start_resolve_chart(chart_guid_by_media(mg))
        return _instant_play_response(mg, body.get("video_guid", ""))
    if not base_target:
        return await forward_upstream(request)
    # 播放器「清晰度/线路」菜单切换时会带我们编码的 bitrate，据此选线路
    choices = _line_choices(base_target)
    chosen_line = None
    if len(choices) > 1:
        chosen_line = _line_of_bitrate(body.get("bitrate"), [t["line"] for t in choices])
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
    q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
    if is_hls:
        play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}"
        # 后台立刻预热该线路（含解析 + 播放列表），播放器取 main 时大概率已就绪
        try:
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
async def direct_proxy(mg: str, request: Request):
    """mp4 等非 HLS 直链代理：同源、带 Range 支持（播放器 seek）。"""
    failed_g = await _await_pending_chart(mg)
    target = store.resolve_media_guid(mg)
    if not target:
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)
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
                                    "index": target.get("index", 0)})
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
                    return Response(f"direct fail {type(exc2).__name__}",
                                    status_code=502)
            else:
                return Response(f"direct fail {type(exc).__name__}", status_code=502)
        else:
            return Response(f"direct fail {type(exc).__name__}", status_code=502)
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
        return Response(f"direct upstream {r.status_code}", status_code=502)
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
    ids = [g for g in ids if g and (store.get_title_by_guid(g) or store.get_episode(g))]
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
    for rec in history.all():
        ig = rec.get("item_guid", "")
        ep = store.get_episode(ig)
        brief = store.get_title_by_guid(ig)
        if ep:
            brief = store.get_title_by_guid(ep["online_guid"])
        if not brief:
            continue  # 缓存已淘汰的标题不再展示
        online_items.append(inj.history_item(brief, rec, ep))

    # 在线历史按最近播放排序，置顶；按 guid 与官方去重
    online_items.sort(key=lambda x: x.get("_updated_at", 0), reverse=True)
    online_guids = {x.get("guid") for x in online_items}
    local_only = [x for x in local_items if x.get("guid") not in online_guids]
    # 剥掉私有字段
    for o in online_items:
        o.pop("_online", None)
        o.pop("_updated_at", None)
        o.pop("_year", None)
    online_items = clientmod.normalize_cards(online_items, _client_of(request))
    return JSONResponse({"code": 0, "msg": "", "data": online_items + local_only},
        headers={"Content-Type": "application/json; charset=utf-8"})


# media/p 的 RPC（quit/checkPlayLink 等）对在线 playLink 直接回 succ
@app.post("/v/api/v1/media/p")
async def media_p(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    link = body.get("playLink", "")
    if isinstance(link, str) and link.startswith(ONLINE_PREFIX):
        # 在线播放的清晰度/线路切换（resetQuality）等无官方媒体任务，直接成功
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
    """零成本换线：当前线在坏线记忆中、且用户没有显式指定该线时，换到非坏线候选。
    让“退出重放”不再黏在上一次切坏的线路上（不发起任何探测网络请求）。"""
    if not picked or not _autofail_on() or not _line_is_bad(picked):
        return picked
    # 用户在播放器里明确选了这条坏线（query 带 line）则尊重其选择，交给探测逻辑
    if request.query_params.get("line"):
        return picked
    for c in _episode_candidates(picked):
        if not _line_is_bad(c):
            _log.info("跳过已知坏线 %s -> %s", picked.get("line"), c.get("line"))
            return c
    return picked


async def _await_pending_chart(mg: str) -> str:
    """若该 media 属于秒进播放页、尚未绑定完成的榜单片，等待其后台绑定结束。"""
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    if og and chart_cache.is_chart_guid(og) and not store.resolve_media_guid(mg):
        await resolve_chart(og)
        return og if not store.resolve_media_guid(mg) else ""
    return ""


def _seg_url(mg: str, absolute: str, qs: str = "") -> str:
    suffix = f"&{qs}" if qs else ""
    return f"{ONLINE_PREFIX}/{mg}/seg?u={b64e(absolute)}{suffix}"









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


async def _probe_latency(cand: dict, request: Request,
                         build_hls: bool = False) -> float | None:
    """探测一条线路该集是否可播，并返回“起播延迟”（秒，越小越快）。

    会就地解析（专有源/跳转/分享，与真实播放同一套逻辑）。失败/文件不存在返回 None。
    build_hls=True 时，HLS 线路顺手构建改写后的播放列表并填缓存（main 取流零额外抓取）；
    竞速阶段传 False，只做轻量验活（抓原始清单，不改写），选出胜者后再构建。"""
    t0 = time.time()
    try:
        await _resolve_if_needed(cand, request)
        raw_url = cand.get("url", "")
        if not raw_url:
            return None
        # 网盘转存后是本服务根相对地址（/v/api/ext/pan/play?fid=…），经回环自调用
        url = _self_url(raw_url)
        headers = cand.get("headers") or {"User-Agent": "Mozilla/5.0"}
        cli = ext_client(request)

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
_LINE_FAST_TTL = 600.0          # 最快线记忆 10 分钟
_LINE_FAST: dict[str, tuple[float, str]] = {}   # 集 key -> (ts, line)
_RACE_CONCURRENCY = 4


def _episode_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{int(picked.get("index", 0) or 0)}'


def _remember_fast(picked: dict, line: str) -> None:
    if not line:
        return
    _LINE_FAST[_episode_key(picked)] = (time.time(), line)
    if len(_LINE_FAST) > 1000:
        for k in list(_LINE_FAST)[:500]:
            _LINE_FAST.pop(k, None)


def remembered_fast_line(picked: dict) -> str | None:
    """该集上一次竞速选出的最快线（未过期、且当前未被标记为坏线）。"""
    rec = _LINE_FAST.get(_episode_key(picked))
    if not rec:
        return None
    ts, line = rec
    if time.time() - ts >= _LINE_FAST_TTL:
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    if _line_is_bad({**picked, "line": line}):
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    return line


def _fastest_on() -> bool:
    try:
        return bool(config.get().get("line_fastest", True))
    except Exception:
        return True


def _race_candidates(picked: dict) -> list[dict]:
    """竞速候选：默认线/上次最快线在前，坏线排除（除非全坏），限量。"""
    cands = _episode_candidates(picked)
    default = store.best_line(picked.get("online_guid", ""))
    fast = remembered_fast_line(picked)
    cands = [c for c in cands if not _line_is_bad(c)] or cands
    try:
        n = int(config.get().get("line_fastest_max", 6))
    except Exception:
        n = 6
    cands = cands[:max(2, n)]

    def rank(c: dict) -> tuple:
        return (c["line"] != fast if fast else True,
                c["line"] != default if default else True)

    cands.sort(key=rank)
    return cands


async def _race_fastest(picked: dict, request: Request) -> dict | None:
    """并行竞速探测候选线路，选首字节延迟最低者（默认高质量线给小幅领先优势）。

    返回胜出的已解析目标（HLS 胜者会再构建播放列表缓存）；全部失败返回 None。"""
    cands = _race_candidates(picked)
    if not cands:
        return None
    default = store.best_line(picked.get("online_guid", ""))
    try:
        timeout = float(config.get().get("line_fastest_timeout", 4.0))
        prefer = float(config.get().get("line_fastest_prefer", 0.25))
    except Exception:
        timeout, prefer = 4.0, 0.25
    sem = asyncio.Semaphore(_RACE_CONCURRENCY)

    async def run(cand: dict):
        async with sem:
            lat = await asyncio.wait_for(
                _probe_latency(cand, request, build_hls=False),
                timeout=timeout)
        return cand, lat

    results = await asyncio.gather(*[run(c) for c in cands],
                                   return_exceptions=True)
    ok = [(c, lat) for r in results
          if isinstance(r, tuple) for (c, lat) in [r] if lat is not None]
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
    _remember_fast(picked, winner["line"])
    # 胜者若是 HLS，构建改写后的播放列表供 main 直接取用
    if re.search(r"\.m3u8(\?|$)", winner.get("url", "").split("?", 1)[0], re.I):
        mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
        if mg:
            await _get_playlist_cached(mg, winner, request)
    _log.info("线路竞速: 《%s》%s 选中 %s (%.2fs, %d 候选)",
              picked.get("title"), picked.get("ep_title") or "",
              winner.get("line"), wlat, len(ok))
    return winner


async def _ensure_playable(picked: dict, request: Request,
                           auto: bool = False) -> dict:
    """取流前确保目标可播。

    - auto=True（播放器自动起播，非用户手动切线）且开启最快线：并行竞速选最快线。
    - 否则（用户手动选了某条线）：先探当前线，失败才记忆坏线并故障切换到首个可用线。
    返回应播放的 picked（可能已换成另一线路）。"""
    if not picked or not picked.get("online_guid"):
        return picked
    if auto and _autofail_on() and _fastest_on():
        winner = await _race_fastest(picked, request)
        if winner is not None:
            return winner
        # 竞速全失败：退回常规故障切换（探当前线/换线）
    if not _autofail_on():
        await _resolve_if_needed(picked, request)
        return picked
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
    return picked


async def _resolve_if_needed(picked: dict, request: Request) -> dict:
    """把播放页(/share/<id>)或 302 跳转地址解析成真实媒体地址（原地更新）。"""
    if picked and picked.get("redirect"):
        real = await providers.resolve_redirect(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["redirect"] = False
            picked["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            ext_client(request), picked["url"],
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
# 单飞：同一「集+线路」只允许一次在途抓取，预热与真正取 main 共用同一个 Future
_PLAYLIST_FUT: dict[str, asyncio.Future] = {}

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
    # 解析后若发现其实是 mp4 直链（wrapper 预判为 HLS 时的兜底），交由调用方 302
    if not re.search(r"\.m3u8(\?|$)", src_url, re.I) \
            and re.search(r"\.mp4(\?|$)", src_url, re.I):
        return _NOT_HLS, None
    text, cur = await _fetch_playlist(request, src_url, headers)
    # 网盘播放代理：原画直出 mp4 时回包不是清单（content-type video、二进制），
    # 判为非 HLS 交调用方 302 到 direct；转码档则返回正常清单。
    if text is not None and picked.get("pan") and not _is_media_playlist(text):
        return _NOT_HLS, None
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
                ext_client(request), jump, (headers or {}).get("Referer", ""))
            if fresh:
                text, cur = await _fetch_playlist(request, fresh, headers)
                if text is not None:
                    picked["url"] = fresh
    if text is None:
        return None, None

    # m3u8 流层去广告（TVBox/OK影视 Pro rules 引擎 + 通用启发式），在改写分片前执行
    try:
        import adfilter as _adfilter
        if _adfilter.enabled():
            text, _ad_removed = _adfilter.filter_hls(text, src_url)
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
                ext_client(request), text, cur, headers)
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

    def seg_decrypt_qs() -> str:
        iv = explicit_iv if explicit_iv is not None else (
            (media_seq + seg_index).to_bytes(16, "big"))
        return f"dec=1&k={b64e(decrypt_key.hex())}&iv={b64e(iv.hex())}"

    compact_i = 0
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


async def _get_playlist_cached(mg: str, picked: dict, request: Request):
    """取改写后的播放列表（内存缓存 + 单飞）。返回文本或 None。
    若该集/线路已有在途抓取（点击时预热启动），直接等同一个结果，不重复请求上游。"""
    key = _playlist_cache_key(picked)
    now = time.time()
    hit = _PLAYLIST_CACHE.get(key)
    if hit and now - hit[0] < _PLAYLIST_TTL:
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
                    if len(_PLAYLIST_CACHE) > 600:
                        for k in list(_PLAYLIST_CACHE)[:300]:
                            _PLAYLIST_CACHE.pop(k, None)
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


def _warm_playlist(mg: str, picked: dict, request: Request) -> None:
    """点击播放时后台预热默认线路播放列表（单飞，真正取 main 时并入同一任务）。"""
    key = _playlist_cache_key(picked)
    hit = _PLAYLIST_CACHE.get(key)
    if hit and time.time() - hit[0] < _PLAYLIST_TTL:
        return
    if key in _PLAYLIST_FUT:
        return  # 已在途
    asyncio.create_task(_get_playlist_cached(mg, picked, request))


@app.get(f"{ONLINE_PREFIX}/{{mg}}/preset.m3u8")
async def hls_preset(mg: str, request: Request):
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)
    # 起播即跳过已知坏线（用户显式选线除外）
    picked = _dodge_known_bad(picked, request)
    # 注意：此处不 await _resolve_if_needed —— preset 只输出线路变体菜单，
    # 播放页/跳转/专有解码延迟到该线路被选中取 main 时再做，避免拖慢首屏。
    # play/play 选定线路后会带 line/index；此时只输出该线路，避免与清晰度菜单重复
    explicit_line = request.query_params.get("line")
    if explicit_line:
        # _pick_episode 已按 line/index 解析出该线路完整 target（含
        # redirect/resolve/xsrc 标志）；这里原样使用，真正解析推迟到取 main 时。
        targets = [picked]
    else:
        targets = store.target_lines(picked)
    # 起播速度优先：preset 只枚举候选线路，不在关键路径解析播放页/302/专有解码，
    # 那些动作延迟到该线路真正被选中取 main 时才做。
    hls_targets = []
    # 任何可按需解析的专有直链（小雅网盘 / JS / 加固等）都要进菜单，死活在 main 探测
    _SPECIAL = ("catv" + "od", "dr" + "py", "gu" + "ard")

    def _need_resolve(t: dict) -> bool:
        return bool(t.get("xsrc")) or any(t.get(k) for k in _SPECIAL)

    for t in targets:
        if t.get("m3u8") or re.search(r"\.(m3u8|mp4)(\?|$)", t.get("url", ""), re.I) \
                or t.get("resolve") or t.get("redirect") or _need_resolve(t):
            hls_targets.append(t)
    if not hls_targets:
        return Response("no hls line", status_code=404)

    # 分辨率探测模块已搁置：线路菜单只显示线路名，不追加分辨率标签。
    idx = int(picked.get("index", 0) or 0)

    # 自动起播：把上一次竞速选出的最快线排到首位（稳定用线）；用户显式选线时不动顺序
    auto_mode = (not explicit_line) and _fastest_on()
    if auto_mode:
        fast = remembered_fast_line(picked)
        if fast:
            hls_targets.sort(key=lambda t: t["line"] != fast)
    else:
        # 手动/菜单模式：网盘线（高质量原画）排在在线线之前，便于直接选到网盘资源
        hls_targets.sort(key=lambda t: 0 if _pan_match(str(t.get("url") or ""))
                         else 1)

    def variant_uri(t: dict, is_auto_first: bool = False) -> str:
        q = {"index": idx, "line": t["line"]}
        # 仅播放器自动起播的首选变体带 auto=1（取 main 时触发竞速选最快线）；
        # 用户手动切到的其它变体不带，严格尊重其选择
        if is_auto_first:
            q["auto"] = "1"
        return "main.m3u8?" + urllib.parse.urlencode(q)

    if len(hls_targets) == 1:
        # 单线路：无 RESOLUTION 的单一变体（分辨率探测已搁置，不再写死占位分辨率）
        master = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
                  f"{variant_uri(hls_targets[0], auto_mode)}\n")
    else:
        # 多线路：无 RESOLUTION 的变体，飞牛 HLS 播放器呈现为可切换「线路」
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for i, t in enumerate(hls_targets):
            name = _line_display_name(t)
            # NAME 供支持的播放器显示线路名；BANDWIDTH 差异化避免变体被合并
            out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={1000000 + i * 100000},'
                       f'PROGRAM-ID=1,NAME="{name}"')
            # 仅首个（自动起播）变体带 auto=1
            out.append(variant_uri(t, auto_mode and i == 0))
        master = "\n".join(out) + "\n"
    return Response(master, media_type="application/vnd.apple.mpegurl")


_PROVIDER_CN = {
    "baofeng": "暴风", "ffzy": "非凡", "lzi": "量子", "wujin": "无尽",
    "youku": "优酷", "guangsu": "光速", "ruyi": "如意", "jisu": "极速",
    "xinlang": "新狼", "dytt": "电影天堂", "s360": "360",
}
_GROUP_CN = {"m3u8": "主线路", "yun": "云播", "ff": "非凡", "uk": "优酷",
             "gs": "光速", "bf": "暴风", "lz": "量子"}


def _friendly_line(key: str) -> str:
    """把 'provider::组名' 转成好认的线路名，如 baofeng::bfzym3u8 -> 暴风。"""
    provider, _, group = key.partition("::")
    pcn = _PROVIDER_CN.get(provider, provider)
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


@app.get(f"{ONLINE_PREFIX}/{{mg}}/main.m3u8")
async def hls_main(mg: str, request: Request):
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)

    # 线路健康探测 + 自动选最快线
    explicit_line = request.query_params.get("line")
    auto_play = request.query_params.get("auto") == "1" and not explicit_line
    request.state._play_mg = mg  # 供探测复用播放列表构建、填充缓存
    # 起播即跳过已知坏线（用户显式选线除外），避免无谓探测延迟
    picked = _dodge_known_bad(picked, request)
    if _autofail_on():
        picked = await _ensure_playable(picked, request, auto=auto_play)
        # 若自动换到了别的线路，后续 _pick_episode 仍按 query 的旧线解析；用换好的目标
        request.state._failover_picked = picked


    body = await _get_playlist_cached(mg, picked, request)
    if body is None:
        # 当前线抓取失败：记忆并自动换一条已探测可用的线路重试一次
        if _autofail_on():
            _mark_line_bad(picked)
            alt = await _find_playable(picked, request, skip_current=True)
            if alt is not None:
                _log.info("播放列表抓取失败，自动换线 %s -> %s",
                          picked.get("line"), alt.get("line"))
                picked = alt
                request.state._failover_picked = alt
                # 小雅等非 HLS 直链
                body = await _get_playlist_cached(mg, picked, request)
    if body is None:
        return Response("fetch fail upstream", status_code=502)
    if body is _NOT_HLS:
        # 预判为 HLS、解析后实为 mp4：302 到直链代理（direct 复用已解析结果）
        q = urllib.parse.urlencode({"line": picked["line"], "index": picked["index"]})
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)
    # 清单活、分片死（如某源 CDN 删流）：先在已解析清单上验首个分片，失败则
    # 标记坏线，并启动「按片名全网回源」后台任务。回源较慢，首个请求不阻塞到
    # 网关超时，而是让客户端（本就每 3 秒重试）在后台找到可播源后的下一次请求
    # 经 _MEDIA_REDIRECT 命中新记录。
    if _autofail_on() and not await _verify_rewritten_first_seg(body, request, picked):
        _mark_line_bad(picked)
        status = await _resurrect_start(picked, mg)
        if status == "ok":
            alt = _pick_episode(mg, request)
            if alt:
                body2 = await _get_playlist_cached(mg, alt, request)
                if body2 and body2 is not _NOT_HLS:
                    return Response(body2, media_type="application/vnd.apple.mpegurl",
                                    headers={"Cache-Control": "no-cache",
                                             "Access-Control-Allow-Origin": "*"})
        if status == "fail":
            return Response("暂无可播线路（该源分片已失效，且未找到其他可用源）",
                            status_code=502)
        # running：让客户端稍候重试，不返回错误清单
        return Response("正在为你寻找可用线路…", status_code=503,
                        headers={"Retry-After": "3",
                                 "Access-Control-Allow-Origin": "*"})
    _sk = sourcehealth.source_key_of(picked.get("line"), picked)
    if _sk:
        sourcehealth.record(_sk, True)
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache",
                             "Access-Control-Allow-Origin": "*"})


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


def _decode_poster_tok(tok: str) -> tuple[str, str, int | None]:
    """解码海报 token，返回 (图床URL, 网盘名, 健康等级)。

    兼容旧版纯 URL token 与新版 JSON{u,p,h} token。"""
    raw = _decode_tok(tok)
    pan_label, grade = "", None
    url = raw
    if raw[:1] in ("{", "["):
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                url = j.get("u", "")
                pan_label = str(j.get("p", "") or "")
                if j.get("h") is not None:
                    grade = int(j["h"])
        except Exception:
            url = raw
    return url, pan_label, grade


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


async def _serve_online_poster(tok_path: str, request: Request) -> Response:
    # 兼容 /online/<token>.jpg 与官方图片前缀 /v/api/v1/sys/img/online/<token>.jpg
    tok_path = tok_path.lstrip("/")
    marker = "online/"
    if marker in tok_path:
        tok_path = tok_path.split(marker, 1)[1]
    try:
        u, pan_label, health_grade = _decode_poster_tok(tok_path)
    except Exception:
        return _poster_fallback()
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
        r = await ext_client(request).get(u, headers=headers)
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
    except Exception:
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
    """供管理台展示的虚拟媒体库清单（热门四库 / 海量四库），含开关与置顶态。"""
    out = []
    for cat, title, _c in chartmod.CHART_CATEGORIES:
        out.append({"guid": CHART_LIB_GUIDS[cat], "title": title,
                    "group": "hot", "enabled": charts_enabled(),
                    "count": len(chart_cache.items(cat))})
    for cat, title, _c in catalogmod.CATALOG_CATEGORIES:
        out.append({"guid": CATALOG_LIB_GUIDS[cat], "title": title,
                    "group": "catalog", "enabled": catalog_enabled() and catalog.available(cat),
                    "count": catalog.total(cat) if catalog.available(cat) else 0})
    pinned = config.pinned_lib_guids()
    for d in out:
        d["pinned"] = d["guid"] in pinned
    return out


@app.get("/v/api/ext/libs")
async def ext_libs(request: Request):
    return {"code": 0, "msg": "", "data": {
        "hot_enabled": charts_enabled(),
        "catalog_enabled": catalog_enabled(),
        "libs": _virtual_lib_list()}}


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
        "catalog_library_enabled",
        "prefer_quality_source",
        "line_fastest", "line_fastest_max", "line_fastest_timeout",
        "line_fastest_prefer", "line_autofail",
        "update_proxy",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
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
    if "update_proxy" in patch:
        pu = str(patch["update_proxy"]).strip()[:300]
        patch["update_proxy"] = pu if re.match(r"^(https?|socks5h?)://", pu) else ""
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
                    cur[sid]["standalone"] = ch["standalone"]
                    sources_changed = True
                if isinstance(ch.get("loose_filter"), bool):
                    cur[sid]["loose_filter"] = ch["loose_filter"]
                if isinstance(ch.get("adult"), bool):
                    cur[sid]["adult"] = ch["adult"] and bool(cur[sid].get("standalone"))
        patch["sources"] = list(cur.values())
    config.save(patch)
    _search_cache.clear()
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


@app.post("/v/api/ext/junk-keywords")
async def ext_junk_keywords(request: Request):
    body = await request.json()
    if body.get("reset"):
        kws = config.reset_junk_keywords()
    else:
        kws = config.set_junk_keywords(body.get("keywords", []))
    _search_cache.clear()
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
    _search_cache.clear()
    if body.get("standalone"):
        _schedule_routes_refresh()
    return {"code": 0, "msg": "added", "data": {"id": info}}


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
    _search_cache.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "deleted"}


@app.post("/v/api/ext/source/restore")
async def ext_source_restore(request: Request):
    config.restore_builtin()
    _search_cache.clear()
    return {"code": 0, "msg": "restored"}


@app.post("/v/api/ext/source/test")
async def ext_source_test(request: Request):
    body = await request.json()
    api = (body.get("api") or "").strip()
    sid = body.get("id")
    if not api and sid:
        src = next((s for s in config.all_sources() if s["id"] == sid), None)
        if not src:
            return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
        api = src["api"]
    keyword = (body.get("keyword") or "庆余年").strip()
    result = await providers.probe_source(api, keyword=keyword)
    return {"code": 0, "msg": "", "data": result}


@app.post("/v/api/ext/source/test-all")
async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]。"""
    async def one(s):
        r = await providers.probe_source(s["api"])
        r.update(id=s["id"], name=s["name"], enabled=bool(s.get("enabled")),
                 builtin=bool(s.get("builtin")))
        return r
    results = await asyncio.gather(*[one(s) for s in config.all_sources()],
                                   return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    return {"code": 0, "msg": "", "data": out}


@app.post("/v/api/ext/source/import-tvbox")
async def ext_import_tvbox(request: Request):
    """导入 TVBox/影视仓/OK影视配置中的标准苹果CMS JSON/XML 源。

    入参：{url?} 或 {text?}，外加 standalone（可选）。对候选源做轻量探测，
    通过者加入自定义源；爬虫 jar / 网盘 / 直播源跳过。
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
    seen_api: set[str] = set()
    skipped: list[str] = []
    for s in sites:
        status, norm = tvboxmod.classify_site(s)
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
    if added_names:
        _search_cache.clear()
        if standalone:
            _schedule_routes_refresh()
        else:
            # 新源加入后也刷新一次聚合路由，便于海量库覆盖
            _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {
        "scanned": len(sites),
        "candidates": len(candidates),
        "added": len(added_names),
        "added_names": added_names,
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

    if cms_added or (guard_report and guard_report.get("enabled_count")) \
            or (csp_report and csp_report.get("added")) \
            or (drpy_report and drpy_report.get("added")):
        _search_cache.clear()
        _schedule_routes_refresh()

    return {"code": 0, "msg": "", "data": {
        "scanned": len(raw_sites),
        "subscriptions": _subs,
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
    # 附带各规则组的名称/域名，便于管理页展示
    st["groups"] = [{"name": g.get("name", ""),
                     "hosts": g.get("hosts", []),
                     "regex": len(g.get("regex", []))}
                    for g in _adf._load_rules()]
    return {"code": 0, "msg": "", "data": st}


@app.post("/v/api/ext/adfilter/toggle")
async def ext_adfilter_toggle(request: Request):
    """开关 m3u8 去广告。body: {"enabled":bool} 或 {"enabled":bool,"dynamic":bool}"""
    import adfilter as _adf
    body = await request.json()
    if "dynamic" in body:
        config.save({"ad_filter_dynamic": bool(body.get("dynamic"))})
    if "enabled" in body:
        _adf.set_enabled(bool(body.get("enabled")))
    return {"code": 0, "msg": "",
            "data": {"enabled": _adf.enabled(),
                     "dynamic": _adf._dynamic_enabled()}}


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




# ------------------------------------------------------------- 兜底透传

@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    # 本地视频/文件等大响应必须流式透传（不缓冲整文件），避免打满内存拖死系统；
    # 该函数内部对普通 JSON 自动回落到缓冲透传。
    return await forward_upstream_streaming(request)
