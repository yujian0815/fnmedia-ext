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


HTTP_TIMEOUT = 12.0
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
    params = {"ac": "detail", "wd": keyword}
    try:
        r = await client.get(source["api"], params=params,
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
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
                       timeout: float = 8.0) -> dict:
    """探测一个苹果CMS资源站是否可用。
    返回 {ok,status,elapsed_ms,count,sample,error}。"""
    api = (api or "").strip()
    t0 = time.monotonic()
    if not api.startswith(("http://", "https://")):
        return {"ok": False, "status": 0, "elapsed_ms": 0, "count": 0,
                "sample": "", "error": "地址需以 http(s):// 开头"}
    try:
        async with httpx.AsyncClient(follow_redirects=True,
                                     trust_env=False) as client:
            r = await client.get(
                api, params={"ac": "detail", "wd": keyword},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code,
                        "elapsed_ms": ms, "count": 0, "sample": "",
                        "error": f"HTTP {r.status_code}"}
            j = r.json()
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
            async with httpx.AsyncClient(follow_redirects=True,
                                         trust_env=False) as client:
                r2 = await client.get(
                    api, params={"ac": "list", "pg": 1},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
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











async def search_all(keyword: str, sources=None) -> list[OnlineTitle]:
    sources = sources or DEFAULT_SOURCES
    enabled = [s for s in sources if s.get("enabled")]
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        tasks = []
        for s in enabled:
            tasks.append(search_source(client, s, keyword))
        results = await __import__("asyncio").gather(*tasks, return_exceptions=True)
    titles: list[OnlineTitle] = []
    for r in results:
        if isinstance(r, list):
            titles.extend(r)
    return titles


def detail_source(source: dict, vod_id: str) -> OnlineTitle | None:
    """同步取详情（带缓存由调用方做）。"""
    import requests  # 仅 PoC；主代理内用 httpx 异步版
    return None
