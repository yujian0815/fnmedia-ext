"""Remote T4 protocol adapter. Plugins and media decoding stay outside fnmedia."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT = 30.0


class T4Error(Exception):
    """A diagnostic safe to return without disclosing upstream URLs or tokens."""

    def __init__(self, message: str, *, refresh_detail: bool = False):
        super().__init__(message)
        self.refresh_detail = refresh_detail


def switch(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def source_id(api: str, ext=None) -> str:
    identity = json.dumps([api.strip(), ext], sort_keys=True,
                          ensure_ascii=True, separators=(",", ":"))
    return "t4_" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def is_folder(row: dict) -> bool:
    return str(row.get("vod_tag", "")).lower() == "folder" or \
        str(row.get("vod_id", "")).startswith(("action:", "push://"))


def search_intent(row: dict) -> str:
    action = row.get("t4_action")
    if isinstance(action, dict) and action.get("type") == "search":
        keyword = action.get("keyword")
        if isinstance(keyword, str):
            return keyword.strip()[:200]
    return ""


async def request(client: httpx.AsyncClient, source: dict,
                  params: dict, timeout: float = TIMEOUT) -> dict:
    query = dict(params)
    if source.get("ext") not in (None, ""):
        ext = source["ext"]
        query["ext"] = ext if isinstance(ext, str) else json.dumps(
            ext, ensure_ascii=False, separators=(",", ":"))
    # httpx.get(params=...) replaces the API query, including its authentication.
    url = httpx.URL(source["api"])
    headers = {"User-Agent": "okhttp/4.12.0"}
    try:
        if source.get("method", "GET").upper() == "POST":
            response = await client.post(url, json=query, headers=headers,
                                         timeout=timeout, follow_redirects=True)
        else:
            response = await client.get(url.copy_merge_params(query), headers=headers,
                                        timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPStatusError as exc:
        raise T4Error(f"T4 服务返回 HTTP {exc.response.status_code}",
                      refresh_detail=exc.response.status_code in (400, 404)) from None
    except (httpx.HTTPError, ValueError):
        raise T4Error("T4 服务连接失败或响应不是 JSON") from None
    if not isinstance(result, dict) or result.get("error") or result.get("detail"):
        raise T4Error("T4 服务返回错误响应")
    if result.get("code") not in (None, 0, "0", 1, "1", 200, "200"):
        raise T4Error("T4 服务报告请求失败")
    return result


def map_title(row: dict, source: dict, *, detail: bool = False,
              locator: dict | None = None):
    import providers

    if not isinstance(row, dict) or is_folder(row):
        return None
    vod_id = str(row.get("vod_id") or "")
    name = str(row.get("vod_name") or "").strip()
    if not vod_id or not name:
        return None
    original = dict(locator or {})
    original.update(kind="t4", source_id=source["id"],
                    source_name=source.get("name", ""),
                    vod_id=original.get("vod_id") or vod_id,
                    content_key=original.get("content_key") or
                    str(row.get("content_key") or vod_id))
    if keyword := search_intent(row):
        original["search_keyword"] = keyword
        year = row["t4_action"].get("year")
        if isinstance(year, str) and re.fullmatch(r"(?:19|20)\d{2}", year):
            original["search_year"] = year
    if isinstance(row.get("t4_source"), str):
        original["source_label"] = row["t4_source"]
    sources = {}
    if detail:
        flags = str(row.get("vod_play_from") or "").split("$$$")
        for i, group in enumerate(str(row.get("vod_play_url") or "").split("$$$")):
            flag = flags[i] if i < len(flags) and flags[i] else f"线路{i + 1}"
            # Display labels must be unique; the actual flag is sent unchanged.
            label = flag if flag not in sources else f"{flag} ({i + 1})"
            episodes = []
            for part in group.split("#"):
                title, sep, play_id = part.partition("$")
                if not sep or not play_id.strip():
                    continue
                title = title.strip() or f"第{len(episodes) + 1}集"
                if play_id.startswith(("action:", "push://")):
                    continue
                episodes.append(providers.Episode(
                    title=title, url=play_id.strip(), direct=False, t4=True,
                    play_from=flag, episode_key=episode_key(title, flag, play_id.strip())))
            if episodes:
                sources[label] = episodes
    original["loaded_at"] = time.time() if detail else 0
    return providers.OnlineTitle(
        provider=source["id"], vod_id=original["content_key"], title=name,
        type_name=str(row.get("type_name") or ""),
        year=str(row.get("vod_year") or ""), area=str(row.get("vod_area") or ""),
        updated=str(row.get("vod_time") or ""), remarks=str(row.get("vod_remarks") or ""),
        score=str(row.get("vod_score") or ""), poster=str(row.get("vod_pic") or ""),
        overview=str(row.get("vod_content") or row.get("vod_blurb") or ""),
        actor=str(row.get("vod_actor") or ""), director=str(row.get("vod_director") or ""),
        sources=sources, locator=original)


_ACTION_LABELS = {
    "互动状态", "点赞", "投币", "收藏", "稍后再看", "媒体信息", "全局搜索",
    "选集播放", "订阅信息", "检查更新", "立即巡检", "取消追剧", "操作",
}
_ACTION_IDS = (
    "action:", "push://", "bilistat-", "bililike-", "bilicoin-", "bilifav-",
    "biliwatch-", "msubstat-", "msubcheck-", "msubinspect-", "msubunsub-",
    "msubadd-", "msubdel-", "msubinfo-", "watchadd-", "watchdel-",
)


def _season_from_flag(flag: str) -> int:
    normalized = re.sub(r"\s+", "", flag).casefold()
    match = re.search(r"(?<![a-z0-9])(?:season|s)0*(\d+)", normalized)
    if not match:
        match = re.search(r"第0*(\d+)季", normalized)
    return int(match[1]) if match else 1


def _episode_coordinates(title: str, default_season: int = 1) -> tuple[int, int] | None:
    text = re.sub(r"\s+", "", title).casefold()
    suffix = re.split(r"s\d+e\d+|第\d+(?:集|话|期)|(?:episode|ep)\d+",
                      text, maxsplit=1)[-1]
    if re.search(r"预告|花絮|彩蛋|特辑|幕后|片花|trailer|preview|sample", suffix) or \
            re.search(r"^\d{1,3}[-~至]\d|e\d+(?:e|[-~至]e?)\d", text):
        return None
    patterns = (
        r"s0*(\d+)e0*(\d+)",
        r"season0*(\d+)episode0*(\d+)",
        r"第0*(\d+)季第0*(\d+)(?:集|话|期)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            values = {(int(season), int(number)) for season, number in matches}
            if len(values) == 1 and not re.search(r"(?:e\d+|集|话)[-~至]\d+", text):
                season, number = next(iter(values))
                labels = {int(n) for n in re.findall(r"第0*(\d+)(?:集|话|期)", text)}
                return (season, number) if not labels or labels == {number} else None
            return None

    patterns = (
        r"第0*(\d+)(?:集|话|期)",
        r"^0*(\d+)(?:集|话|期)(?=$|[._(\-·])",
        r"(?:episode|ep)0*(\d+)(?=$|[._(\-])",
        r"^0*(\d{1,3})(?=$|[._(\-])",
    )
    for pattern in patterns:
        numbers = {int(n) for n in re.findall(pattern, text)}
        if numbers:
            return (default_season, next(iter(numbers))) if len(numbers) == 1 else None
    return None


def episode_key(title: str, flag: str = "", play_id: str = "") -> str:
    # Preserve opaque playlist rows, but do not expose control/action entries as episodes.
    normalized = re.sub(r"\s+", "", title).casefold()
    compact_label = re.sub(r"[^\w]", "", normalized)
    if play_id.casefold().startswith(_ACTION_IDS) or compact_label in _ACTION_LABELS:
        return ""
    if re.fullmatch(r"追剧第\d+季", compact_label):
        return ""
    group = flag.rsplit(" · ", 1)[-1].strip().casefold()
    if group == "操作":
        return ""
    if group in ("相关视频", "up主视频"):
        line_id = hashlib.sha256(flag.encode()).hexdigest()[:16]
        return f"season:0:related:{line_id}:{normalized}"

    coordinates = _episode_coordinates(title, _season_from_flag(flag))
    if coordinates:
        season, number = coordinates
        return (f"episode:{number}" if season == 1
                else f"season:{season}:episode:{number}")
    season = _season_from_flag(flag)
    label = "label:" + normalized
    return label if season == 1 else f"season:{season}:{label}"


def episode_season(key: str) -> int:
    match = re.match(r"season:(\d+):", key)
    return int(match[1]) if match else 1


def episode_number(key: str) -> int:
    match = re.fullmatch(r"(?:season:\d+:)?episode:(\d+)", key)
    return int(match[1]) if match else 0


def episode_menu_title(title: str, key: str) -> str:
    number = episode_number(key)
    if number and not re.fullmatch(r"(?:第)?\s*\d+\s*(?:集|话|期)?", title.strip()):
        # Strip release/size suffixes from common cloud-drive file names.
        if _episode_coordinates(title) or re.match(r"^\s*\d{1,3}(?:[._(\-\s]|$)", title):
            return f"第{number}集"
    return title.strip()


def playlist_entries(episodes: list[dict], flag: str):
    """Yield unique coordinates; retain distinct versions without aligning them."""
    rows = [(episode_key(e["title"], e.get("play_from") or flag, e.get("url", "")), e)
            for e in episodes]
    counts, labels = {}, {}
    for key, ep in rows:
        counts[key] = counts.get(key, 0) + 1
        label = re.sub(r"\s+", "", ep["title"]).casefold()
        labels[label] = labels.get(label, 0) + 1
    for key, ep in rows:
        if not key:
            continue
        if counts[key] > 1:
            label = re.sub(r"\s+", "", ep["title"]).casefold()
            if labels[label] > 1:
                continue
            # Two encodes of E01 are selectable, not two interchangeable E01 lines.
            line_id = hashlib.sha256(flag.encode()).hexdigest()[:16]
            key = f"season:{episode_season(key)}:variant:{line_id}:{label}"
        yield key, dict(ep, episode_key=key)


async def search(client: httpx.AsyncClient, source: dict, keyword: str):
    if not switch(source.get("searchable")):
        return []
    result = await request(client, source, {"wd": keyword, "quick": "false", "pg": 1})
    rows = result.get("list")
    if not isinstance(rows, list):
        raise T4Error("T4 搜索缺少 list 列表")
    return [title for row in rows if (title := map_title(row, source)) is not None]


async def navigation_search(client: httpx.AsyncClient, source: dict, keyword: str,
                            year: str = "", options: dict | None = None):
    try:
        result = await request(client, source, {
            "wd": keyword, "quick": "false", "pg": 1, "nav_search": "true", "year": year,
            **(options or {}),
        })
    except T4Error as exc:
        if (options or {}).get("nav_session") and exc.refresh_detail:
            raise T4Error("片单搜索会话已失效，请返回片单重新选片") from None
        raise
    if not isinstance(result.get("list"), list):
        raise T4Error("T4 导航搜索缺少 list 列表")
    return result


async def home(client: httpx.AsyncClient, source: dict) -> dict:
    result = await request(client, source, {"filter": "true"})
    if not isinstance(result.get("class"), list) and not isinstance(result.get("list"), list):
        raise T4Error("T4 首页缺少 class/list 列表")
    if any(result.get(key) is not None and not isinstance(result[key], list)
           for key in ("class", "list")):
        raise T4Error("T4 首页 class/list 格式错误")
    return result


async def category(client: httpx.AsyncClient, source: dict, tid: str,
                   page: int = 1, filters: dict | None = None) -> dict:
    # Plugin initialization ext and category extend are independent contracts.
    result = await request(client, source, {
        "ac": "detail", "t": tid, "pg": page, "filter": "true",
        "extend": json.dumps(filters or {}, ensure_ascii=False, separators=(",", ":")),
    })
    if not isinstance(result.get("list"), list):
        raise T4Error("T4 分类缺少 list 列表")
    return result


async def detail(client: httpx.AsyncClient, source: dict, locator: dict,
                 fallback: dict | None = None):
    import inject

    vod_id = locator["vod_id"]
    result = await request(client, source, {"ac": "detail", "ids": vod_id})
    rows = result.get("list")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise T4Error("T4 详情未返回唯一影片，请重新搜索")
    row = dict(rows[0])
    for field, brief_key in (("vod_name", "title"), ("vod_pic", "poster"),
                             ("type_name", "type_name"), ("vod_year", "year"),
                             ("vod_content", "overview"), ("vod_actor", "actor"),
                             ("vod_director", "director"), ("vod_area", "area"),
                             ("vod_score", "score"), ("vod_remarks", "remarks")):
        if field == "vod_content" and row.get("vod_blurb"):
            continue
        if not row.get(field) and fallback:
            row[field] = fallback.get(brief_key, "")
    row["type_name"] = inject.media_type_name(
        str(row.get("type_name") or ""), (fallback or {}).get("type_name", ""))
    if is_folder(row):
        raise T4Error("该条目是目录，请从支持目录的入口展开")
    # Sidecar may rewrap the ID. Standard, stable IDs must still match exactly.
    returned_id = str(row.get("vod_id") or "")
    stable_key = str(row.get("content_key") or "")
    if not str(vod_id).startswith("t4_") and returned_id != str(vod_id):
        raise T4Error("T4 详情影片标识不一致")
    if stable_key and stable_key != locator["content_key"]:
        raise T4Error("T4 详情稳定标识不一致")
    title = map_title(row, source, detail=True, locator=locator)
    if not title or not title.sources:
        raise T4Error("T4 详情没有可播放的分集")
    return title


@dataclass
class Playback:
    url: str
    headers: dict
    wrapper: str
    parse: int = 0
    jx: int = 0
    subtitles: list = field(default_factory=list)
    identified: bool = False
    size: int = 0

    @property
    def needs_proxy(self) -> bool:
        if self.headers:
            return True
        host = urlsplit(self.url).hostname or ""
        if host == "localhost" or host.endswith((".local", ".lan")):
            return True
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            return False


def playback(result: dict, api: str) -> Playback:
    try:
        parse, jx = int(result.get("parse") or 0), int(result.get("jx") or 0)
    except (ValueError, TypeError):
        raise T4Error("T4 播放解析状态格式错误") from None
    if parse or jx:
        raise T4Error("T4 仍要求客户端嗅探或解析，请在 T4 服务端完成取链")
    url = result.get("url")
    if not isinstance(url, str) or not url.strip():
        raise T4Error("T4 未返回单一媒体地址")
    if url.lstrip().startswith(("<", "#")):
        raise T4Error("T4 返回内联清单，当前适配尚不支持")
    url = urljoin(api, url.strip())
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        raise T4Error("T4 返回的媒体地址不是 HTTP(S)")
    headers = result.get("header") or result.get("headers") or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except ValueError:
            raise T4Error("T4 媒体请求头不是有效 JSON") from None
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or
        "\r" in k + v or "\n" in k + v for k, v in headers.items()
    ):
        raise T4Error("T4 媒体请求头格式错误")
    kind = str(result.get("type") or result.get("contentType") or "").lower()
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8") or "mpegurl" in kind or kind in ("hls", "m3u8"):
        wrapper = "HLS"
    elif path.endswith(".mpd") or "dash" in kind:
        wrapper = "DASH"
    elif path.endswith(".mkv") or "matroska" in kind or kind == "mkv":
        wrapper = "MKV"
    elif path.endswith(".mp4") or kind in ("mp4", "video/mp4"):
        wrapper = "MP4"
    else:
        wrapper = ""
    return Playback(url=url, headers=headers, wrapper=wrapper,
                    subtitles=result.get("subs") or result.get("subtitles") or [])


async def player(client: httpx.AsyncClient, source: dict,
                 play_id: str, flag: str) -> Playback:
    result = await request(client, source, {"play": play_id, "flag": flag})
    return playback(result, source["api"])


async def probe(client: httpx.AsyncClient, source: dict, timeout: float = 8.0):
    start = time.monotonic()
    try:
        result = await request(client, source, {}, timeout=timeout)
        ok = isinstance(result.get("list"), list) or isinstance(result.get("class"), list)
        return {"ok": ok, "status": 200, "elapsed_ms": int(
            (time.monotonic() - start) * 1000), "count": len(result.get("list") or []),
            "sample": "", "error": "" if ok else "T4 首页缺少 class/list"}
    except T4Error as exc:
        return {"ok": False, "status": 0, "elapsed_ms": int(
            (time.monotonic() - start) * 1000), "count": 0, "sample": "", "error": str(exc)}
