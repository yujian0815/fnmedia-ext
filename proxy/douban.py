#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆瓣移动 API 元数据客户端（详情/演员/剧照，免 API Key）。

只访问豆瓣官方 movie.douban.com / m.douban.com 与图床 doubanio.com。
热门榜单已有 douban_id 时直接取详情；其它条目用 subject_suggest 匹配。
磁盘缓存 + 后台异步补齐 + 进程内限频，绝不阻塞搜索/详情/播放。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.parse

import httpx

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "douban_cache.json")
_SUGGEST = "https://movie.douban.com/j/subject_suggest"
_REXXAR = "https://m.douban.com/rexxar/api/v2"
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_FETCH_SEM = threading.Semaphore(2)
_CACHE: dict = {}
_INFLIGHT: set = set()
_TTL = 7 * 24 * 3600
_NEG_TTL = 15 * 60
_LAST_REQ = [0.0]


def enabled() -> bool:
    try:
        import config
        c = config.config.get()
        return bool(c.get("tmdb_enabled", True)) and bool(
            c.get("douban_enabled", True))
    except Exception:
        return False


def _load() -> None:
    global _CACHE
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            _CACHE = json.load(f)
    except Exception:
        _CACHE = {}


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


_load()


def _proxy() -> str:
    try:
        import config
        c = config.config.get()
        return str(c.get("source_proxy") or c.get("update_proxy") or "").strip()
    except Exception:
        return ""


def _throttle() -> None:
    # 约 2 请求/秒；只在后台请求线程内生效，不阻塞事件循环。
    with _RATE_LOCK:
        wait = 0.45 - (time.monotonic() - _LAST_REQ[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.monotonic()


def _new_client(proxy: str = "") -> httpx.Client:
    kw = {"timeout": httpx.Timeout(10, connect=5), "follow_redirects": True,
          "trust_env": False,
          "headers": {"User-Agent": _UA, "Accept": "application/json"}}
    if proxy:
        kw["proxy"] = proxy
    return httpx.Client(**kw)


def _get(client: httpx.Client, url: str, referer: str) -> dict | list | None:
    try:
        _throttle()
        r = client.get(url, headers={"Referer": referer})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    # 仅直连失败时复用用户已配置的持久代理；目标仍是豆瓣官方 URL。
    px = _proxy()
    if px:
        try:
            with _new_client(px) as fallback:
                _throttle()
                r = fallback.get(url, headers={"Referer": referer})
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", str(s or "").lower())


def _douban_id(brief: dict) -> str:
    for v in (brief.get("douban_id"), (brief.get("chart") or {}).get("id")):
        if v:
            return str(v)
    # 老绑定 chart 标记不含 id：用本地榜单索引补齐，零网络。
    guid = brief.get("guid") or ""
    if guid:
        try:
            from charts import chart_cache
            meta = chart_cache.get_guid(guid) or {}
            return str(meta.get("id") or "")
        except Exception:
            pass
    return ""


def _cache_key(brief: dict) -> str:
    did = _douban_id(brief)
    if did:
        return f"id:{did}"
    from tmdb import _clean_title, _is_tv_brief
    return f"title:{'tv' if _is_tv_brief(brief) else 'movie'}:" \
           f"{_norm(_clean_title(brief.get('title', '')))}:{brief.get('year') or ''}"


def _pick_suggest(rows: list, title: str, year: str) -> dict | None:
    nt = _norm(title)
    best = None
    best_score = -1
    for r in rows:
        name = r.get("title") or ""
        nn = _norm(name)
        ry = str(r.get("year") or "")
        score = 0
        if nt and (nt in nn or nn in nt):
            score += 100
        if year and ry == str(year):
            score += 50
        elif year and ry and abs(int(ry) - int(year)) <= 1:
            score += 20
        if score > best_score:
            best, best_score = r, score
    if not best:
        return None
    nn = _norm(best.get("title") or "")
    title_match = bool(nt and (nt in nn or nn in nt))
    year_match = bool(year and str(best.get("year") or "") == str(year))
    return best if title_match or year_match else None


def _avatar(person: dict) -> str:
    av = person.get("avatar") or {}
    return (av.get("large") or av.get("normal") or "").replace("http://", "https://")


def _normalize_detail(detail: dict, celebrities: dict, photos: dict,
                      douban_id: str) -> dict:
    cast = []
    for a in (celebrities.get("actors") or [])[:12]:
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        cast.append({
            "source": "douban",
            "id": a.get("id"),
            "name": name,
            "original_name": a.get("latin_name") or name,
            "character": re.sub(r"^饰\s*", "", str(a.get("character") or "")),
            "profile": _avatar(a),
        })
    landscape = []
    for p in (photos.get("photos") or []):
        im = p.get("image") or {}
        large = im.get("large") or im.get("normal") or {}
        try:
            w, h = int(large.get("width") or 0), int(large.get("height") or 0)
        except (TypeError, ValueError):
            continue
        url = (large.get("url") or "").replace("http://", "https://")
        if url and w >= 700 and h > 0 and w / h >= 1.45:
            landscape.append((abs(w / h - 16 / 9), -w, url))
    landscape.sort()
    rating = detail.get("rating") or {}
    pic = detail.get("pic") or {}
    directors = [str(x.get("name") or "") for x in detail.get("directors") or []]
    durations = detail.get("durations") or []
    runtime = 0
    if durations:
        m = re.search(r"\d+", str(durations[0]))
        runtime = int(m.group(0)) if m else 0
    pub = (detail.get("pubdate") or [""])[0]
    date_match = re.search(r"\d{4}-\d{1,2}-\d{1,2}", str(pub))
    return {
        "douban_id": douban_id,
        "title": detail.get("title") or "",
        "original_title": detail.get("original_title") or "",
        "overview": detail.get("intro") or "",
        "vote_average": rating.get("value") or 0,
        "genres": detail.get("genres") or [],
        "runtime": runtime,
        "release_date": date_match.group(0) if date_match else "",
        "poster": (pic.get("large") or pic.get("normal")
                   or detail.get("cover_url") or ""),
        "backdrop": landscape[0][2] if landscape else "",
        "cast": cast,
        "director": " / ".join(x for x in directors if x)[:120],
        "number_of_episodes": detail.get("episodes_count") or 0,
    }


def _fetch_by_id(client: httpx.Client, did: str) -> dict | None:
    base = f"{_REXXAR}/movie/{did}"
    ref = f"https://m.douban.com/movie/subject/{did}"
    detail = _get(client, base + "?ck=&for_mobile=1", ref)
    if not isinstance(detail, dict) or not detail.get("id"):
        return None
    celebrities = _get(client, base + "/celebrities?ck=&for_mobile=1", ref) or {}
    photos = _get(client, base + "/photos?start=0&count=24&ck=&for_mobile=1",
                  ref) or {}
    return _normalize_detail(detail, celebrities, photos, did)


def _fetch(brief: dict) -> dict | None:
    did = _douban_id(brief)
    with _new_client() as client:
        if did:
            return _fetch_by_id(client, did)
        from tmdb import _clean_title
        title = _clean_title(brief.get("title", ""))
        year = str(brief.get("year") or "")
        rows = _get(client, _SUGGEST + "?" + urllib.parse.urlencode(
            {"q": title}), "https://movie.douban.com/")
        if not isinstance(rows, list):
            return None
        picked = _pick_suggest(rows, title, year)
        if not picked or not picked.get("id"):
            return None
        return _fetch_by_id(client, str(picked["id"]))


def lookup(brief: dict) -> dict | None:
    key = _cache_key(brief)
    with _LOCK:
        e = _CACHE.get(key)
    if not isinstance(e, dict) or not isinstance(e.get("data"), dict):
        return None
    if time.time() - float(e.get("ts") or 0) > _TTL:
        return None
    return e["data"]


def prefetch(brief: dict) -> None:
    if not enabled() or not brief:
        return
    key = _cache_key(brief)
    with _LOCK:
        e = _CACHE.get(key)
        if isinstance(e, dict):
            age = time.time() - float(e.get("ts") or 0)
            if isinstance(e.get("data"), dict) and age < _TTL:
                return
            if e.get("data") is None and age < _NEG_TTL:
                return
        if key in _INFLIGHT:
            return
        _INFLIGHT.add(key)

    def _work():
        try:
            with _FETCH_SEM:
                data = _fetch(brief)
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "data": data}
                if len(_CACHE) > 2000:
                    for old in sorted(_CACHE,
                                      key=lambda x: _CACHE[x].get("ts", 0))[:500]:
                        _CACHE.pop(old, None)
                _save()
        except Exception:
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "data": None}
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    try:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _work)
        except RuntimeError:
            threading.Thread(target=_work, daemon=True).start()
    except Exception:
        threading.Thread(target=_work, daemon=True).start()


def enrich(brief: dict) -> dict | None:
    """读取豆瓣缓存并后台补齐；库范围由调用方 tmdb.lib_allowed 统一约束。"""
    if not enabled() or not brief:
        return None
    prefetch(brief)
    return lookup(brief)
