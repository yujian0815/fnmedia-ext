#!/usr/bin/env python3
"""在线资源 provider：苹果 CMS V10 标准 JSON 接口（ac=detail）。

资源站返回的播放地址有两种：
  - 直链：http(s)://....m3u8 / .mp4
  - 中转页（/share/<id> 等）：需要解析，PoC 阶段标记为需解析、先跳过
provider 只负责"搜索→条目→播放 URL 列表"，不做转码。
"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

import features
import netutil

# 2026-10-17 国内直连实测（搜索+m3u8 直链可达性）。403/404 多为走 Clash 代理所致。
DEFAULT_SOURCES: list[dict] = [
    {"id": "baofeng", "name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "ffzy", "name": "非凡资源", "api": "https://cj.ffzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "lzi", "name": "量子资源", "api": "https://cj.lziapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "wujin", "name": "无尽资源", "api": "https://api.wujinapi.me/api.php/provide/vod/at/json", "enabled": False},
    {"id": "youku", "name": "优酷资源", "api": "https://api.ukuapi88.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "guangsu", "name": "光速资源", "api": "https://api.guangsuapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "ruyi", "name": "如意资源", "api": "https://cj.rycjapi.com/api.php/provide/vod/at/json", "enabled": False},
    {"id": "jisu", "name": "极速资源", "api": "https://jszyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "xinlang", "name": "新狼资源", "api": "https://api.xinlangapi.com/xinlangapi.php/provide/vod/at/json", "enabled": True},
    {"id": "dytt", "name": "电影天堂", "api": "https://caiji.dyttzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "s360", "name": "360资源", "api": "https://360zy.com/api.php/provide/vod/at/json", "enabled": False},
]


# 源质量默认权重（越高越优先；未列出取 0）。基于国内直连可达性、直链率与片单质量
# 的经验初值，用户可在管理页「片源质量」整体覆盖（存 config.source_priority）。
# 标准 CMS 站按口碑分档；新增自定义源默认 0。
DEFAULT_PRIORITY: dict[str, int] = {
    "baofeng": 40, "ffzy": 45, "guangsu": 40, "jisu": 38, "xinlang": 36,
    "lzi": 30, "youku": 28, "dytt": 30, "wujin": 20, "ruyi": 20, "s360": 20,
    "xiaoya": 55,
}


def default_source_priority(source: dict) -> int:
    sid = source.get("id", "")
    return DEFAULT_PRIORITY.get(sid, 0)


def source_priority(source: dict, overrides: dict | None = None) -> int:
    """源的有效质量权重：用户覆盖优先，否则内省默认。公开/私有构建通用，
    不能放进 PRIVATE 剥离块（管理页与播放排序在 lite 包也会调用）。"""
    sid = source.get("id", "")
    if overrides and sid in overrides:
        try:
            return int(overrides[sid])
        except (TypeError, ValueError):
            pass
    return default_source_priority(source)



HTTP_TIMEOUT = 12.0
# 加固（真机）搜索单源硬超时：聚合搜索要 gather 全部源，真机卡顿不能拖垮整体。
# 取较短值让慢加固源快速失败，纯 CMS 源通常 5s 内返回，不影响热榜片起播。
GUARD_SEARCH_TIMEOUT = 8.0
# 普通 csp spider jar / drpy 真机源同样走 redroid，to_thread 本身无超时；
# 几十源抢 3 台设备时单源可能长时间不返回，必须给硬超时，否则 gather 永久挂住。
SPIDER_SEARCH_TIMEOUT = 8.0
DRPY_SEARCH_TIMEOUT = 8.0
_DIRECT_RE = re.compile(r"^https?://", re.I)
_MEDIA_RE = re.compile(r"\.(m3u8|mp4|flv|mkv|ts)(\?|$)", re.I)
# 可由自有账号转存原画播放的网盘分享链接（夸克 / UC）。
_PAN_SHARE_RE = re.compile(
    r"https?://(?:pan\.quark\.cn/s/|(?:pan|drive)\.uc\.cn/s/)[A-Za-z0-9]+", re.I)


@dataclass
class Episode:
    title: str
    url: str
    direct: bool          # True=可直接播放的媒体地址
    resolve: bool = False  # True=播放页，需 resolve_share 解析
    headers: dict = field(default_factory=dict)
    # True=该地址是一次 302 跳转，Location 才是真实媒体（如苹果CMS ac=videodiag）
    redirect: bool = False
    # 非标准源标记：url 为加密 token，播放前需在专属适配层解码；
    # play_from 是该集所属线路标识，解码必须一并上送（公开构建下恒为 False）
    xsrc: bool = False
    play_from: str = ""
    # 远程 T4 订阅源：云端按需解析播放；episode_key 为云端集标识
    t4: bool = False
    episode_key: str = ""


@dataclass
class OnlineTitle:
    provider: str
    vod_id: str
    title: str
    type_name: str
    year: str
    area: str
    updated: str
    remarks: str
    score: str
    poster: str
    overview: str
    actor: str
    director: str
    sources: dict          # {播放线路名: [Episode,...]}
    locator: dict = field(default_factory=dict)

    @property
    def online_id(self) -> str:
        return f"{self.provider}:{self.vod_id}"


def _parse_play(vod_play_from: str, vod_play_note: str, vod_play_url: str,
                referer: str) -> dict:
    """把苹果 CMS 的 from/note/url 解析成 {线路: [Episode]}。"""
    froms = (vod_play_from or "").split("$$$")
    notes = (vod_play_note or "").split("$$$")
    groups = (vod_play_url or "").split("$$$")
    out: dict[str, list[Episode]] = {}
    for i, raw_group in enumerate(groups):
        line = froms[i] if i < len(froms) and froms[i] else f"线路{i+1}"
        eps = []
        for item in raw_group.split("#"):
            item = item.strip()
            if not item or "$" not in item:
                continue
            name, url = item.split("$", 1)
            url = url.strip()
            if not _DIRECT_RE.match(url):
                continue
            ep_name = name.strip() or f"第{len(eps)+1}集"
            # 选集层去广告：跳过“关注公众号/加微信/加群/下载APP”等引流假选集
            try:
                import adfilter as _adf
                if _adf.is_ad_episode(ep_name):
                    continue
            except Exception:
                pass
            direct = bool(_MEDIA_RE.search(url))
            # /share/<id> 等播放页：非直链但可按需解析出媒体地址
            resolvable = (not direct) and bool(
                re.search(r"/(share|play|player)/[A-Za-z0-9]+", url))
            # ac=videodiag 等：请求该地址会 302 到真实 m3u8/mp4
            redirect_ep = bool(re.search(r"[?&]ac=videodiag\b", url, re.I))
            eps.append(Episode(
                title=ep_name,
                url=url,
                direct=direct or redirect_ep,  # 视为可播，播放时跟随 302
                resolve=resolvable,
                redirect=redirect_ep,
                headers={"Referer": referer, "User-Agent": "Mozilla/5.0"},
            ))
        if eps:
            out[line] = eps
    return out


def _xml_text(elem) -> str:
    """取 XML 元素文本（自动解 CDATA），无则空串。"""
    if elem is None:
        return ""
    return (elem.text or "").strip()


def parse_cms_xml(text: str) -> dict | None:
    """把苹果 CMS 的 at/xml 响应（RSS）转成与 at/json 等价的 dict：
    {code:1, list:[ {vod_id, vod_name, vod_play_from, vod_play_url, ...} ]}。

    XML 用 <video> 元素、子标签名（id/name/pic/year/...、<dl><dd flag>），
    JSON 用 vod_ 前缀字段；这里统一映射，后续 _map_vod 无需区分格式。"""
    if not text or not text.lstrip().startswith("<"):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
    except Exception:
        return None
    # 根可能是 <rss>，列表在其下 <list>；也兼容直接 <list>
    list_node = root.find(".//list")
    if list_node is None:
        list_node = root if root.tag.lower() == "list" else None
    if list_node is None:
        return {"code": 1, "list": []}

    def child(video, name):
        el = video.find(name)
        return _xml_text(el) if el is not None else ""

    out = []
    for video in list_node.findall("video"):
        # 播放源：<dl><dd flag="m3u8"><![CDATA[ 第1集$url#第2集$url ]]></dd>
        froms, groups = [], []
        for dd in video.findall(".//dl/dd"):
            flag = (dd.get("flag") or "").strip() or f"line{len(froms)+1}"
            play = _xml_text(dd)
            if not play:
                continue
            froms.append(flag)
            groups.append(play)
        vod = {
            "vod_id": child(video, "id"),
            "vod_name": child(video, "name"),
            "type_name": child(video, "type"),
            "vod_year": child(video, "year"),
            "vod_area": child(video, "lang") or child(video, "area"),
            "vod_time": child(video, "last"),
            "vod_remarks": child(video, "note"),
            "vod_score": child(video, "score"),
            "vod_pic": child(video, "pic"),
            "vod_content": child(video, "des") or child(video, "content")
                           or child(video, "blurb"),
            "vod_actor": child(video, "actor"),
            "vod_director": child(video, "director"),
            "vod_play_from": "$$$".join(froms),
            "vod_play_note": "",
            "vod_play_url": "$$$".join(groups),
        }
        out.append(vod)
    return {"code": 1, "list": out}


def _is_xml_api(api: str) -> bool:
    return "/at/xml" in (api or "").lower()


def _map_vod(v: dict, source: dict) -> OnlineTitle | None:
    sources = _parse_play(v.get("vod_play_from", ""), v.get("vod_play_note", ""),
                          v.get("vod_play_url", ""), _site_root(source["api"]))
    return OnlineTitle(
        provider=source["id"],
        vod_id=str(v.get("vod_id")),
        title=v.get("vod_name", "").strip(),
        type_name=v.get("type_name", ""),
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated=str(v.get("vod_time", "") or v.get("vod_time_add", "") or ""),
        remarks=v.get("vod_remarks", "") or "",
        score=str(v.get("vod_score", "") or ""),
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or v.get("vod_blurb") or "").strip(),
        actor=v.get("vod_actor", "") or "",
        director=v.get("vod_director", "") or "",
        sources=sources,
    )


def _site_root(api: str) -> str:
    p = urllib.parse.urlparse(api)
    return f"{p.scheme}://{p.netloc}/"


async def search_source(client: httpx.AsyncClient, source: dict,
                        keyword: str) -> list[OnlineTitle]:
    if source.get("kind") == "t4":
        import logging
        import t4
        try:
            return await t4.search(client, source, keyword)
        except t4.T4Error as exc:
            logging.getLogger("fnmedia").warning("T4 search %s: %s", source["id"], exc)
            return []
    params = {"ac": "detail", "wd": keyword}
    try:
        r = await client.get(source["api"], params=params,
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        if _is_xml_api(source["api"]):
            # 苹果 CMS at/xml：RSS XML 响应，先转成与 at/json 等价的 dict
            j = parse_cms_xml(r.text)
        else:
            j = r.json()
    except Exception:
        return []
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return []
    out = []
    for v in j.get("list", []) or []:
        try:
            t = _map_vod(v, source)
        except Exception:
            continue
        if t and t.title:
            out.append(t)
    return out


# 非直链页面里内嵌媒体地址的解析规则
_EMBED_URL_RES = [
    re.compile(r"""(?:const|let|var)\s+url\s*=\s*["']([^"']+)["']""", re.I),
    re.compile(r"""["']url["']\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""(https?://[^"'\s<>]+?\.m3u8[^"'\s<>]*)""", re.I),
]
_resolve_cache: dict[str, tuple[float, str]] = {}
_RESOLVE_TTL = 30 * 60


async def resolve_redirect(client: httpx.AsyncClient, jump_url: str,
                           referer: str = "") -> str | None:
    """跟随一次 302 跳转（ac=videodiag 等），返回 Location 指向的真实媒体地址。

    部分私有源该接口极慢且间歇抖动（同一地址有时 1s、有时 16~19s 超时），
    只试一次会在播放中途偶发解析失败导致整个 m3u8 502。这里放宽超时并做
    退避重试，成功后缓存较长时间，保证一次会话内地址稳定。
    """
    import asyncio as _aio
    import time as _t
    jump_url = jump_url.strip()
    hit = _resolve_cache.get(jump_url)
    if hit and _t.time() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    found = None
    for attempt, tmo in enumerate((20.0, 16.0, 12.0)):
        try:
            r = await client.get(jump_url, headers=headers, timeout=tmo,
                                 follow_redirects=False)
            loc = r.headers.get("location") or ""
            if loc and _MEDIA_RE.search(loc):
                found = urllib.parse.urljoin(jump_url, loc)
                break
        except Exception:
            found = None
        if attempt < 2:
            await _aio.sleep(0.3 * (2 ** attempt))
    if found:
        _resolve_cache[jump_url] = (_t.time(), found)
        if len(_resolve_cache) > 2000:
            for k in list(_resolve_cache)[:1000]:
                _resolve_cache.pop(k, None)
    return found


async def resolve_share(client: httpx.AsyncClient, page_url: str,
                        referer: str = "") -> str | None:
    """把 /share/<id> 等播放页解析成真实媒体地址（按需、带缓存）。"""
    import time as _t
    page_url = page_url.strip()
    hit = _resolve_cache.get(page_url)
    if hit and _t.time() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    # 优先用强化嗅探引擎（player_aaaa / iframe 递归 / 转义解码 / base64），
    # 它是纯 HTTP 解析，可替代部分原本要靠加固爬虫或 WebView 的网页源。
    try:
        import sniff as _sniff
        found = await _sniff.sniff(client, page_url, referer)
        if found:
            return found
    except Exception:
        pass
    try:
        r = await client.get(page_url, headers=headers, timeout=10,
                             follow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None
    found = None
    for rx in _EMBED_URL_RES:
        m = rx.search(html)
        if m:
            cand = m.group(1)
            # 必须像媒体地址（.m3u8/.mp4 或相对媒体路径）
            if _MEDIA_RE.search(cand) or re.search(r"/\d{6,}/[^\"']+\.(m3u8|mp4)", cand):
                found = urllib.parse.urljoin(str(r.url), cand)
                break
    if found:
        _resolve_cache[page_url] = (_t.time(), found)
        if len(_resolve_cache) > 2000:
            for k in list(_resolve_cache)[:1000]:
                _resolve_cache.pop(k, None)
    return found


async def probe_source(api: str, keyword: str = "庆余年",
                       timeout: float = 8.0, source: dict | None = None) -> dict:
    """探测一个苹果CMS资源站是否可用。
    返回 {ok,status,elapsed_ms,count,sample,error}。"""
    api = (api or "").strip()
    if source and source.get("kind") == "t4":
        import t4
        async with httpx.AsyncClient(trust_env=False) as client:
            return await t4.probe(client, source, timeout)
    t0 = time.monotonic()
    if not api.startswith(("http://", "https://")):
        return {"ok": False, "status": 0, "elapsed_ms": 0, "count": 0,
                "sample": "", "error": "地址需以 http(s):// 开头"}
    try:
        async with netutil.async_client(timeout=timeout) as client:
            r = await client.get(
                api, params={"ac": "detail", "wd": keyword},
                headers={"User-Agent": "Mozilla/5.0"})
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code,
                        "elapsed_ms": ms, "count": 0, "sample": "",
                        "error": f"HTTP {r.status_code}"}
            # 同时支持 at/json 与 at/xml（XML 先转等价 dict）
            j = parse_cms_xml(r.text) if _is_xml_api(api) else r.json()
    except Exception as exc:
        return {"ok": False, "status": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "count": 0, "sample": "", "error": type(exc).__name__}
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return {"ok": False, "status": 200, "elapsed_ms": ms, "count": 0,
                "sample": "", "error": "非苹果CMS JSON（code≠1）"}
    lst = j.get("list", []) or []
    sample = ""
    direct = 0
    for v in lst:
        name = v.get("vod_name", "")
        play = v.get("vod_play_url", "") or ""
        if not sample and name:
            sample = name
        if _MEDIA_RE.search(play):
            direct += 1
    if not lst:
        # 接口合法但该词无结果，再试分类列表确认源活着
        try:
            async with netutil.async_client(timeout=timeout) as client:
                r2 = await client.get(
                    api, params={"ac": "list", "pg": 1},
                    headers={"User-Agent": "Mozilla/5.0"})
                j2 = r2.json()
            if isinstance(j2, dict) and j2.get("code") in (1, "1"):
                return {"ok": True, "status": 200, "elapsed_ms": ms,
                        "count": 0, "sample": "",
                        "error": "接口正常（该词无结果）"}
        except Exception:
            pass
        return {"ok": False, "status": 200, "elapsed_ms": ms, "count": 0,
                "sample": "", "error": "无返回结果"}
    return {"ok": True, "status": 200, "elapsed_ms": ms,
            "count": len(lst), "direct": direct, "sample": sample, "error": ""}


# 资源站有效性检测专用的【常见热门】关键词。只选各大主流 CMS 基本都收录的长红
# 影视剧，避免用冷门片导致“源其实正常、只是没这部”的误判。任一关键词搜到即视为有效。
PROBE_HOT_KEYWORDS = ("庆余年", "狂飙", "流浪地球", "斗罗大陆", "琅琊榜")


async def probe_source_alive(source: dict, timeout: float = 8.0,
                             keywords=PROBE_HOT_KEYWORDS) -> dict:
    """用多个常见热门关键词检测资源站是否有效。

    依次尝试关键词，任一返回非空结果即判定有效（ok=True）并立即返回；全部关键词
    都搜不到才判定失效。这样不会因单个片名缺失而误伤正常源。返回与 probe_source
    兼容的结构，并额外带 tried（尝试过的词）与 hits（命中的词）。"""
    api = (source.get("api") or "").strip()
    t0 = time.monotonic()
    tried, hit = [], ""
    last = {"ok": False, "status": 0, "elapsed_ms": 0, "count": 0,
            "sample": "", "error": ""}
    for kw in keywords:
        tried.append(kw)
        r = await probe_source(api, keyword=kw, timeout=timeout, source=source)
        last = r
        if r.get("ok") and (r.get("count") or 0) > 0:
            hit = kw
            return {**r, "alive": True, "hit": hit, "tried": tried,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    # 全部关键词都无结果：判定失效（保留最后一次的错误/状态供展示）
    return {**last, "alive": False, "hit": "", "tried": tried,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}











def _is_heavy_search_source(s: dict) -> bool:
    """是否为需要并发保护/专有调度的源：远程 T4 或加固/爬虫/drpy/小雅等私有专有源。

    普通苹果 CMS 直链源（纯 HTTP、无 jar/无真机）很轻，不该被并发上限拖累。"""
    if s.get("kind") == "t4":
        return True
    return bool(s.get("catvod") or s.get("drpy") or s.get("guard")
                or s.get("spider_src") or s.get("xsrc"))


async def _search_cms(keyword: str, sources,
                            soft_timeout: float | None = None,
                            *, early_min_wait: float | None = None,
                            early_min_results: int | None = None) -> list[OnlineTitle]:
    """纯普通 CMS 源：不限并发扇出（保留上游软超时），最大化搜索速度。

    与含 T4/真机源时的 Semaphore(8) 交错调度分开，避免几个慢 CMS 源占满槽位拖死其它源。

    够快即返回：快源先完成、累计结果达到阈值且过了最小等待后，立即取消仍在途的慢
    源，不必干等满 soft_timeout（实测个别源 7s+，会把整个搜索拖到 5s 多）。软超时仍
    作为兜底，保证慢词/冷门词能拿全结果。early_min_wait/results 可由交互入口（二级
    目录绑定）传入更激进的值以压进客户端加载窗口；不传则用全局搜索默认。"""
    import asyncio
    enabled = [s for s in sources if s.get("enabled")]
    # 提前返回阈值：在线搜索最终去重后只取 max_per_keyword（默认12）组，原始结果
    # 达到该量级即足以铺满首屏，无需等满软超时被慢源拖尾。
    early_min_results = early_min_results if early_min_results is not None else 25
    # 至少等待这么久，避免快源结果太少时过早收手漏掉更多源
    early_min_wait = early_min_wait if early_min_wait is not None else 0.8
    async with netutil.async_client() as client:
        tasks = [asyncio.create_task(search_source(client, s, keyword))
                 for s in enabled]
        if not tasks:
            return []
        # 预初始化：搜索任务被取消（CancelledError）时 results 仍已定义，
        # 避免 UnboundLocalError 让后台搜索 Future 异常未回收、刷错误日志。
        results: list = []
        if soft_timeout is None:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            per = max(0.5, soft_timeout - 0.3)
            collected: list = []
            pending = set(tasks)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + per
            early_gate = loop.time() + min(early_min_wait, per)
            try:
                while pending and loop.time() < deadline:
                    wait_for = min(0.25, deadline - loop.time())
                    done, pending = await asyncio.wait(
                        pending, timeout=wait_for,
                        return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        try:
                            r = t.result()
                            if isinstance(r, list):
                                collected.append(r)
                        except Exception:
                            pass
                    # 过了最小等待且结果已够多：提前收手，取消在途慢源
                    total = sum(len(x) for x in collected)
                    if loop.time() >= early_gate and total >= early_min_results:
                        break
            finally:
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            # 被取消/超时的任务若已产出也尽量回收
            for t in tasks:
                if t.done() and not t.cancelled() and t.exception() is None:
                    r = t.result()
                    if isinstance(r, list) and r not in collected:
                        collected.append(r)
            results = collected
    return [t for r in results if isinstance(r, list) for t in r]


async def search_all(keyword: str, sources=None,
                     soft_timeout: float | None = None,
                     *, budget: float | None = None,
                     early_min_wait: float | None = None,
                     early_min_results: int | None = None) -> list[OnlineTitle]:
    """搜索扇出：纯普通 CMS 不限并发；含远程 T4/真机私有源时限并发交错调度。"""
    import asyncio
    from itertools import zip_longest
    sources = DEFAULT_SOURCES if sources is None else sources
    enabled = [s for s in sources if s.get("enabled")]
    # 纯普通 CMS（无 T4、无加固/爬虫/drpy/小雅等需要真机保护的源）：不限并发扇出。
    if enabled and not any(_is_heavy_search_source(s) for s in enabled):
        _early = {}
        if early_min_wait is not None:
            _early["early_min_wait"] = early_min_wait
        if early_min_results is not None:
            _early["early_min_results"] = early_min_results
        return await _search_cms(
            keyword, sources, soft_timeout if budget is None else budget,
            **_early)
    # Imported T4 sources must not wait behind every built-in CMS slow request.
    remote = [s for s in enabled if s.get("kind") == "t4"]
    legacy = [s for s in enabled if s.get("kind") != "t4"]
    scheduled = [s for pair in zip_longest(remote, legacy) for s in pair if s is not None]
    sem = asyncio.Semaphore(8)
    if budget is None:
        budget = soft_timeout
    async with netutil.async_client() as client:
        # 按 T4 交错顺序（远程 T4 不排在所有内置慢 CMS 之后）构造每源搜索协程，
        # 私有专有源走各自搜索分支；PanSou 不进全局搜索（仅播放菜单手动线）。
        def _coro_for(s):
            return search_source(client, s, keyword)

        async def one(source):
            async with sem:
                return await _coro_for(source)

        tasks = [asyncio.create_task(one(s)) for s in scheduled]
        # 预初始化：finally 与取消路径都会引用 results，避免 UnboundLocalError
        results: list = []
        try:
            if budget is None and soft_timeout is None:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            else:
                # 有预算：到点即用已完成源结果，未完成任务取消（单慢源不拖垮全局）。
                wait_budget = budget if budget is not None else soft_timeout
                if tasks:
                    # 够快即返回：快源结果够多且过了最小等待就提前收手，避免个别
                    # 慢源（实测有 12s）把整个搜索拖满预算，与 _search_cms 同策略。
                    early_min_results, early_min_wait = 25, 0.8
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + float(wait_budget)
                    early_gate = loop.time() + min(early_min_wait, float(wait_budget))
                    pending = set(tasks)
                    results = []
                    while pending and loop.time() < deadline:
                        done, pending = await asyncio.wait(
                            pending, timeout=min(0.25, deadline - loop.time()),
                            return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            if not t.cancelled() and t.exception() is None:
                                results.append(t.result())
                        total = sum(len(x) for x in results if isinstance(x, list))
                        if loop.time() >= early_gate and total >= early_min_results:
                            break
                else:
                    results = []
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 取消后回收已产出的结果，避免丢失快源数据
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception() is None:
                    r = task.result()
                    if isinstance(r, list) and r not in results:
                        results.append(r)
    titles: list[OnlineTitle] = []
    for r in results:
        if isinstance(r, list):
            titles.extend(r)
    return titles


def detail_source(source: dict, vod_id: str) -> OnlineTitle | None:
    """同步取详情（带缓存由调用方做）。"""
    import requests  # 仅 PoC；主代理内用 httpx 异步版
    return None
