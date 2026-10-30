#!/usr/bin/env python3
"""把在线资源（OnlineTitle 或持久化 brief dict）伪造成飞牛影视 item 结构。"""
from __future__ import annotations

import base64 as _b64

import providers
from store import (make_guid, media_guid, video_guid,
                   season_guid, episode_guid, ep_media_guid, ep_video_guid)


def _poster(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        tok = _b64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"/online/{tok}.jpg"
    return url


def _poster_badged(url: str, pan_label: str = "",
                   health_grade: int | None = None) -> str:
    """带角标的海报：token 编码 JSON{u:图床, p:网盘名, h:健康等级}。

    无任何角标时退化为普通 _poster（保持与旧缓存/旧链接兼容）。"""
    if not url:
        return ""
    if not url.startswith("http"):
        return url
    if not pan_label and health_grade is None:
        return _poster(url)
    import json as _json
    payload = {"u": url}
    if pan_label:
        payload["p"] = pan_label
    if health_grade is not None:
        payload["h"] = int(health_grade)
    tok = _b64.urlsafe_b64encode(
        _json.dumps(payload, ensure_ascii=False).encode()
    ).decode().rstrip("=")
    return f"/online/{tok}.jpg"


# 单集作为 Movie 点开即播；多集作为 TV，配合 season/list、episode/list
# 伪造出原生选集 UI。苹果 CMS 的 type_name 用于修正“单集连载剧”等边界情况。
_TV_TYPE_WORDS = ("电视剧", "连续剧", "日剧", "韩剧", "美剧", "英剧", "泰剧",
                  "国剧", "日本剧", "韩国剧", "欧美剧", "海外剧", "港台剧", "港台",
                  "动漫", "番剧", "动画剧", "综艺")
_MOVIE_TYPE_WORDS = ("电影", "影片", "剧情片", "动作片", "喜剧片", "爱情片", "科幻片",
                     "恐怖片", "纪录片")


def is_tv(b: dict) -> bool:
    typ = (b.get("type_name") or "").strip()
    # 明确的电影类型（含动画电影、纪录片等）一律作为单部电影点开即播。
    # 苹果 CMS 一部电影在每条线路里也是 1 个“正片”条目，不能因此误判成 TV。
    if any(w in typ for w in _MOVIE_TYPE_WORDS):
        return False
    # 动漫/综艺：多集才算连载剧集；单集番外/剧场版按电影处理。
    if "综艺" in typ and _ep_count(b) <= 1:
        return False
    if any(w in typ for w in _TV_TYPE_WORDS):
        return _ep_count(b) > 0
    return _ep_count(b) > 1


def fnos_type(b: dict) -> str:
    return "TV" if is_tv(b) else "Movie"


# 在线热门细分类：movie / tv / anime / variety（按优先级判定）
_ANIME_WORDS = ("动漫", "番剧", "动画剧", "动画", "日本动漫", "日韩动漫",
                "国产动漫", "欧美动漫", "动漫片")
_VARIETY_WORDS = ("综艺", "真人秀")
_MOVIE_CAT_WORDS = ("电影", "影片", "剧情片", "动作片", "喜剧片", "爱情片",
                    "科幻片", "恐怖片", "纪录片", "动画电影")


def hot_category(b: dict) -> str:
    """把在线条目归入 movie/tv/anime/variety 之一。"""
    typ = (b.get("type_name") or "").strip()
    if any(w in typ for w in _ANIME_WORDS):
        return "anime"
    if any(w in typ for w in _VARIETY_WORDS):
        return "variety"
    if any(w in typ for w in _MOVIE_CAT_WORDS):
        return "movie"
    # 其余（日剧/韩剧/海外剧/连续剧等，以及类型缺失）统一归入剧集库；
    # 不用“单集=电影”兜底，避免连载剧刚出第一集被错放进电影库。
    return "tv"


def brief_of(t: providers.OnlineTitle) -> dict:
    """OnlineTitle -> 可持久化的自包含 brief。"""
    return {
        "guid": make_guid(t),
        "online_id": t.online_id,
        "title": t.title,
        "type_name": t.type_name,
        "year": t.year,
        "poster": t.poster,
        "overview": t.overview,
        "remarks": t.remarks,
        "score": t.score,
        "actor": t.actor,
        "director": t.director,
        "area": t.area,
        "lines": {name: [e.__dict__ for e in eps] for name, eps in t.sources.items()},
    }


def _f(b: dict, k, d=""):
    return b.get(k, d) or d


def _ep_count(b: dict) -> int:
    return sum(len(e) for e in b.get("lines", {}).values())


ONLINE_TAG = "【在线】"

# app 层注入的角标提供者：brief -> (网盘名, 健康等级或None)。
# 避免 inject 反向依赖 app（循环导入）；未注入时不显示角标。
_badge_provider = None


def set_badge_provider(fn) -> None:
    global _badge_provider
    _badge_provider = fn


def _poster_for_brief(b: dict) -> str:
    """海报路径：按 brief 线路生成带角标海报（网盘资源 ☁ / 源健康点）。

    优先用 brief 上预算好的 _pan/_health；否则实时调用注入的角标提供者。"""
    cover = _f(b, "poster")
    if not cover.startswith("http"):
        return _poster(cover)
    pan, hg = _f(b, "_pan"), b.get("_health")
    if (not pan) and hg is None and _badge_provider is not None:
        try:
            pan, hg = _badge_provider(b)
        except Exception:  # noqa: BLE001
            pan, hg = "", None
    if pan or hg is not None:
        return _poster_badged(cover, pan, hg if isinstance(hg, int) else None)
    return _poster(cover)


def search_item(b: dict) -> dict:
    guid = b["guid"]
    title = _f(b, "title")
    remarks = _f(b, "remarks")
    year = _f(b, "year")
    area = _f(b, "area")
    typ = fnos_type(b)
    nep = _ep_count(b)
    return {
        "guid": guid,
        # TV/盒子端列表模型要求 video_guid/path/runtime，缺失会整库“加载失败”
        "video_guid": video_guid(guid),
        "path": "",
        "runtime": 0,
        "lan": "zh-CN",
        "imdb_id": "",
        "trim_id": "",
        "tv_title": "",
        "parent_guid": "",
        "parent_title": "",
        # 标题前置【在线】标签，搜索结果里与本地资源直观区分
        "title": ONLINE_TAG + title + (f" ·{remarks}" if remarks else ""),
        "type": typ,
        "genres": [],
        "poster": _poster_for_brief(b),
        "poster_width": 0,
        "poster_height": 0,
        "is_favorite": 0,
        "watched": 0,
        "watched_ts": 0,
        "vote_average": _f(b, "score", "0") or "0",
        "media_stream": {"resolutions": None, "audio_type": None, "color_range_type": None},
        "release_date": year,
        "season_number": 0,
        "episode_number": 0,
        "air_date": year,
        "number_of_seasons": 1 if typ == "TV" else 0,
        "number_of_episodes": nep,
        "local_number_of_seasons": 0,
        "local_number_of_episodes": 0,
        "status": "在线",
        "overview": _f(b, "overview")[:500],
        "ancestor_guid": "",
        "ancestor_name": f"🌐在线·{year}·{area}" if area else "🌐在线资源",
        "ancestor_category": typ,
        "ts": 0,
        "duration": 0,
        "single_child_guid": "",
        "file_name": "",
    }


def detail_item(b: dict) -> dict:
    guid = b["guid"]
    title = _f(b, "title")
    year = _f(b, "year")
    overview = _f(b, "overview")
    actor = _f(b, "actor")
    director = _f(b, "director")
    typ = fnos_type(b)
    nep = _ep_count(b)
    ov = overview + (f"\n\n主演：{actor}" if actor else "") + (f"\n导演：{director}" if director else "")
    return {
        "guid": guid,
        "imdb_id": "",
        "trim_id": "",
        "tv_title": "",
        "parent_title": "",
        "title": title,
        "logos": "",
        "original_title": "",
        "backdrops": _poster(_f(b, "poster")),
        "posters": _poster(_f(b, "poster")),
        "poster_width": 0,
        "poster_height": 0,
        "vote_average": _f(b, "score", "0") or "0",
        "genres": [],
        "content_ratings": "",
        "release_date": year,
        "runtime": 0,
        "production_countries": [_f(b, "area")] if _f(b, "area") else [],
        "overview": ov,
        "is_favorite": 0,
        "is_watched": 0,
        "watched_ts": 0,
        "air_date": year,
        "season_number": 0,
        "number_of_seasons": 1 if typ == "TV" else 0,
        "number_of_episodes": nep,
        "local_number_of_episodes": 0,
        "local_number_of_seasons": 0,
        # TV 也允许点播放键（play/info 会智能续播到该播的那集）
        "can_play": 1,
        "type": typ,
        "play_error": "",
        "parent_guid": "",
        "ancestor_guid": "",
        "ancestor_name": "🌐在线资源",
        "ancestor_category": typ,
        "play_item_guid": guid,
        "duration": 0,
        "logic_type": 0,
        "status": "在线",
        "media_stream": {"resolutions": None, "audio_type": None, "color_range_type": None},
    }


def _base_item(**kw):
    d = {
        "guid": "", "lan": "zh-CN", "imdb_id": "", "trim_id": "", "tv_title": "",
        "parent_guid": "", "parent_title": "", "title": "", "type": "Episode",
        "poster": "", "poster_width": 0, "poster_height": 0, "is_favorite": 0,
        "watched": 0, "watched_ts": 0, "vote_average": "0",
        "media_stream": {"resolutions": None, "audio_type": None, "color_range_type": None},
        "release_date": "", "season_number": 1, "episode_number": 0, "air_date": "",
        "number_of_seasons": 0, "number_of_episodes": 0,
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "1", "ancestor_guid": "", "ancestor_name": "🌐在线资源",
        "ancestor_category": "TV", "ts": 0, "duration": 0,
        "single_child_guid": "", "file_name": "", "overview": "", "genres": [],
    }
    d.update(kw)
    return d


def season_list(b: dict) -> list[dict]:
    """伪造 GET /season/list/:tvGuid -> 一个 Season。"""
    guid = b["guid"]
    nep = _ep_count(b)
    sg = season_guid(guid)
    return [_base_item(
        guid=sg, type="Season", title="第 1 季", parent_guid=guid,
        parent_title=_f(b, "title"), season_number=1, episode_number=0,
        number_of_seasons=1, number_of_episodes=0, poster=_poster(_f(b, "poster")),
        tv_title=_f(b, "title"), can_play=None, vote_average=_f(b, "score", "0") or "0",
        release_date=_f(b, "year"), air_date=_f(b, "year"))]


def episode_list(b: dict) -> list[dict]:
    """伪造 GET /episode/list/:seasonGuid -> 每一集。"""
    import store as _st
    guid = b["guid"]
    line = _st.store.best_line(guid)
    # 必须与 store._direct_eps 的过滤完全一致（direct 或可按需解析的 resolve 页），
    # 否则 episode/list 的第 i 张卡与 store 用同一 episode_guid(guid,"",i) 建立的
    # 集索引错位：点第 k 集播另一集，甚至 404。
    eps = [e for e in b["lines"][line] if e.get("direct") or e.get("resolve")]
    out = []
    for i, e in enumerate(eps):
        eg = episode_guid(guid, "", i)  # 集身份不绑定线路
        out.append(_base_item(
            guid=eg, type="Episode",
            title=e.get("title") or f"第{i+1}集",
            parent_guid=season_guid(guid), parent_title="第 1 季",
            tv_title=_f(b, "title"), season_number=1, episode_number=i + 1,
            number_of_seasons=1, can_play=1, poster=_poster(_f(b, "poster")),
            media_guid=ep_media_guid(eg), video_guid=ep_video_guid(eg),
            audio_guid="", subtitle_guid=""))
    return out


def play_info(b: dict) -> dict:
    guid = b["guid"]
    typ = fnos_type(b)
    return {
        "grand_guid": "",
        "guid": guid,
        "parent_guid": "",
        "play_config": None,
        "ts": 0,
        "type": typ,
        "video_guid": video_guid(guid),
        "audio_guid": "",
        "subtitle_guid": "",
        "media_guid": media_guid(guid),
        "item": detail_item(b),
        "direct_link_audio_index": -1,
    }


def episode_play_info(ep: dict) -> dict:
    """对伪造 Episode 的 play/info：media/video guid 与该集绑定。"""
    eg = ep["ep_guid"]
    return {
        "grand_guid": "",
        "guid": eg,
        "parent_guid": ep.get("season_guid", ""),
        "play_config": None,
        "ts": 0,
        "type": "Episode",
        "video_guid": ep_video_guid(eg),
        "audio_guid": "",
        "subtitle_guid": "",
        "media_guid": ep_media_guid(eg),
        "item": {
            "guid": eg, "type": "Episode", "title": ep.get("ep_title", ""),
            "can_play": 1, "season_number": ep.get("season", 1),
            "episode_number": ep.get("index", 0) + 1,
            "parent_guid": ep.get("season_guid", ""),
        },
        "direct_link_audio_index": -1,
    }


def episode_stream_list(ep: dict) -> dict:
    eg = ep["ep_guid"]
    mg = ep_media_guid(eg)
    vg = ep_video_guid(eg)
    title = ep.get("ep_title", "episode")
    # 与单集相同的 file/video 结构，但 guid 绑定到该集
    return {
        "files": [{
            "guid": mg, "path": f"在线/{title}.m3u8", "file_name": title + ".m3u8",
            "size": 0, "timestamp": 0, "type": 1, "sort_num": 0, "can_play": 1,
            "play_error": "", "create_time": 0, "update_time": 0,
            "parent_fv_guid": "", "file_birth_time": 0, "progress_thumb_hash_dir": "",
        }],
        "video_streams": [{
            "media_guid": mg, "title": "", "guid": vg, "resolution_type": "1080p",
            "color_range_type": "SDR", "codec_name": "h264", "codec_type": "video",
            "color_range": "tv", "profile": "High", "index": 0, "width": 1920,
            "height": 1080, "coded_width": 0, "coded_height": 0,
            "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p", "level": "40",
            "color_space": "", "color_transfer": "", "color_primaries": "",
            "duration": 0, "dv_profile": 0, "refs": 1, "r_frame_rate": "25 fps",
            "avg_frame_rate": "25 fps", "bits_per_raw_sample": "8", "bps": 0,
            "progressive": 1, "bit_depth": 8, "wrapper": "HLS", "create_time": 0,
            "update_time": 0, "rotation": 0, "ext1": 0, "is_bluray": False,
        }],
        "audio_streams": [],
        "subtitle_streams": [],
    }


def stream_list(b: dict) -> dict:
    guid = b["guid"]
    mg = media_guid(guid)
    vg = video_guid(guid)
    title = _f(b, "title")
    return {
        "files": [{
            "guid": mg, "path": f"在线/{title}.m3u8", "file_name": title + ".m3u8",
            "size": 0, "timestamp": 0, "type": 1, "sort_num": 0, "can_play": 1,
            "play_error": "", "create_time": 0, "update_time": 0,
            "parent_fv_guid": "", "file_birth_time": 0, "progress_thumb_hash_dir": "",
        }],
        "video_streams": [{
            "media_guid": mg, "title": "", "guid": vg, "resolution_type": "1080p",
            "color_range_type": "SDR", "codec_name": "h264", "codec_type": "video",
            "color_range": "tv", "profile": "High", "index": 0, "width": 1920,
            "height": 1080, "coded_width": 0, "coded_height": 0,
            "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p", "level": "40",
            "color_space": "", "color_transfer": "", "color_primaries": "",
            "duration": 0, "dv_profile": 0, "refs": 1, "r_frame_rate": "25 fps",
            "avg_frame_rate": "25 fps", "bits_per_raw_sample": "8", "bps": 0,
            "progressive": 1, "bit_depth": 8, "wrapper": "HLS", "create_time": 0,
            "update_time": 0, "rotation": 0, "ext1": 0, "is_bluray": False,
        }],
        "audio_streams": [],
        "subtitle_streams": [],
    }


def history_item(brief: dict, rec: dict, ep: dict | None = None) -> dict:
    """构造 GET /play/list 中的一条在线播放历史（含进度 ts/duration）。

    brief: 在线标题；ep: 若为剧集的某一集（store.get_episode 结果），
    rec: history.record（ts/duration/media_guid...）。
    """
    is_ep = bool(ep)
    title = _f(brief, "title")
    year = _f(brief, "year")
    if is_ep:
        item_guid = ep["ep_guid"]
        typ = "Episode"
        tv_title = title
        parent_title = "第 1 季"
        season_number = 1
        episode_number = ep.get("index", 0) + 1
        display = ep.get("ep_title") or f"第{episode_number}集"
    else:
        item_guid = brief["guid"]
        typ = "Movie"
        tv_title = parent_title = ""
        season_number = episode_number = 0
        display = title
    ts = int(rec.get("ts", 0) or 0)
    dur = int(rec.get("duration", 0) or 0)
    # 已看完（进度≥95%）则从0开始，不占继续观看
    if dur and ts >= dur * 0.95:
        ts = 0
    return {
        "guid": item_guid,
        "lan": "zh-CN", "imdb_id": "",
        "tv_title": tv_title,
        "parent_guid": season_guid(brief["guid"]) if is_ep else "",
        "parent_title": parent_title,
        "title": display,
        "type": typ,
        "poster": _poster(_f(brief, "poster")),
        "poster_width": 0, "poster_height": 0,
        "runtime": int(dur / 60) if dur else 0,
        "is_favorite": 0, "watched": 0, "watched_ts": 0,
        "vote_average": _f(brief, "score", "0") or "0",
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
        "season_number": season_number,
        "episode_number": episode_number,
        "number_of_seasons": 1 if is_tv(brief) else 0,
        "number_of_episodes": _ep_count(brief),
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "在线",
        "ancestor_guid": "",
        "ancestor_name": "🌐在线资源",
        "ancestor_category": "TV" if is_tv(brief) else "Movie",
        "ts": ts,
        "duration": dur,
        "single_child_guid": "",
        "media_guid": rec.get("media_guid", "") or (
            ep_media_guid(item_guid) if is_ep else media_guid(item_guid)),
        "audio_guid": rec.get("audio_guid", "") or "",
        "video_guid": rec.get("video_guid", "") or (
            ep_video_guid(item_guid) if is_ep else video_guid(item_guid)),
        "subtitle_guid": rec.get("subtitle_guid", "") or "_no_display_",
        "file_name": "",
        # TV(Flutter) 列表模型要求 path，缺失会让整页“加载失败”（继续观看也是列表）
        "path": "",
        "_online": True,
        "_updated_at": rec.get("updated_at", 0),
        "_year": year,
    }
