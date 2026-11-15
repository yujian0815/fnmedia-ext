#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TMDB 元数据客户端：按片名+年份匹配，取详情/剧照/演员表，磁盘缓存。

只读 TMDB 官方 API（api.themoviedb.org）与官方图床（image.tmdb.org），不接受
第三方地址。API key 存本机 config（tmdb_api_key），不入包、不上传。富化走后台
异步 + 缓存，绝不阻塞搜索/起播/海报（守住 v0.2.42 播放基线）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.parse

import httpcore
from httpcore._backends.sync import SyncBackend

_API = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "tmdb_cache.json")
_LOCK = threading.RLock()
_CACHE: dict = {}
_TTL = 7 * 24 * 3600        # 命中缓存保留 7 天
_NEG_TTL = 15 * 60          # 未命中/瞬时失败仅负缓存 15 分钟，避免锁死后续恢复
_INFLIGHT: set = set()
# 最近一次可用通道（短时记忆）：避免每次详情/演员表都先等直连超时。
_CHANNEL_GOOD: dict = {"proxy": "", "until": 0.0}


def _key() -> str:
    try:
        import config
        return str(config.config.get().get("tmdb_api_key", "") or "").strip()
    except Exception:
        return ""


def enabled() -> bool:
    try:
        import config
        c = config.config.get()
        if not c.get("tmdb_enabled", True):
            return False
        # 豆瓣免 Key 可作为主源；TMDB 无 Key 时仅跳过 TMDB 自身请求。
        try:
            import douban
            if douban.enabled():
                return True
        except Exception:
            pass
        return bool(_key())
    except Exception:
        return False


# —— 识别范围（按媒体库）——
# 默认只识别「热门四库(chart)」与「海量四库(catalog)」；单源独立库/自定义库、
# T4、加固、网盘与本地库一律先排除。config.tmdb_libs 为白名单，元素可以是
# 库类型（"chart"/"catalog"/"standalone"…）或具体库键（"chart:movie"/
# "standalone:custom_xxx"），供后期管理页按库开关直接复用；空数组=全不识别。
_DEFAULT_LIBS = ("chart", "catalog")
# 与 catalog.AGG_SCOPE 保持一致（跨源聚合海量四库的保留作用域 id）。
_AGG_SCOPE = "__agg__"


def _lib_whitelist() -> list:
    try:
        import config
        v = config.config.get().get("tmdb_libs", None)
    except Exception:
        v = None
    if not isinstance(v, list):
        return list(_DEFAULT_LIBS)
    return [str(x) for x in v if isinstance(x, str) and x]


def lib_key_of_brief(brief: dict) -> str | None:
    """反查 brief 所属虚拟库的稳定库键（纯本地 O(1)，零网络、不扫目录缓存）。

    依据入库时写入的静态身份：chart 绑定标记 / catalog_scope / t4 locator；
    兜底用榜单缓存索引认 chart guid（重启恢复的旧绑定）。认不出（搜索结果、
    本地/自定义库等）返回 None，调用方按「不在识别范围」处理。
    """
    if not isinstance(brief, dict):
        return None
    ch = brief.get("chart")
    if isinstance(ch, dict) and ch.get("cat"):
        return f"chart:{ch['cat']}"
    scope = brief.get("catalog_scope") or ""
    if scope == _AGG_SCOPE:
        cat = brief.get("catalog_cat") or ""
        return f"catalog:{cat}" if cat else "catalog"
    if scope:
        return f"standalone:{scope}"
    if (brief.get("locator") or {}).get("kind") == "t4":
        return "t4"
    guid = brief.get("guid") or ""
    if guid:
        try:
            from charts import chart_cache
            if chart_cache.is_chart_guid(guid):
                cat = chart_cache.cat_of(guid) or ""
                return f"chart:{cat}" if cat else "chart"
        except Exception:
            pass
    return None


def lib_allowed(brief: dict) -> bool:
    """该 brief 所属媒体库是否在 TMDB 识别白名单内（库键或库类型命中即可）。"""
    key = lib_key_of_brief(brief)
    if not key:
        return False
    wl = _lib_whitelist()
    return key in wl or key.split(":", 1)[0] in wl


def img(path: str, size: str = "w500") -> str:
    """TMDB 图片相对路径 → 官方图床绝对地址。size: w500/original 等。"""
    if not path:
        return ""
    return f"{IMG_BASE}/{size}{path}"


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


def _norm(s: str) -> str:
    return re.sub(r"[\s　:：·\-—_|,，.。!！?？'\"()（）\[\]【】]", "",
                  str(s or "")).lower()


def _ck(title: str, year: str, is_tv: bool) -> str:
    return f"{'tv' if is_tv else 'movie'}:{_norm(title)}:{year or ''}"


def _clean_title(title: str) -> str:
    """去除资源站展示后缀，保留 TMDB 检索所需的真实片名。"""
    t = re.sub(r"【[^】]*】", "", str(title or "")).strip()
    t = re.split(r"\s*[·|｜]\s*(?:正片|已完结|HD.*|BD.*|高清.*|中字.*)$",
                 t, maxsplit=1)[0].strip()
    t = re.sub(r"(?:普通话版|国语版|粤语版|英语版|原声版|中字版|高清版|完整版)$",
               "", t).strip()
    t = re.sub(r"[（(](?:19|20)\d{2}[)）]\s*$", "", t).strip()
    return t or str(title or "").strip()


class _IPv4Backend(SyncBackend):
    """仅 TMDB 客户端使用的 IPv4 后端。

    本机到 api.themoviedb.org 的 IPv6 路由会连接超时，而同一官方域名的 IPv4
    正常。只在 TCP 连接阶段选 A 记录；HTTP Origin/TLS SNI 仍是官方域名，
    不改目标地址、不走代理，也不影响进程内其它网络请求。
    """

    def connect_tcp(self, host: str, port: int, **kwargs):
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except OSError:
            return super().connect_tcp(host, port, **kwargs)
        last_exc = None
        for _family, _type, _proto, _canon, sockaddr in infos:
            try:
                return super().connect_tcp(sockaddr[0], port, **kwargs)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        return super().connect_tcp(host, port, **kwargs)


def _fallback_proxy() -> str:
    """直连失败时复用用户已配置的持久代理（目标仍是 TMDB 官方地址）。"""
    try:
        import config
        c = config.config.get()
        return str(c.get("source_proxy") or c.get("update_proxy") or "").strip()
    except Exception:
        return ""


def _get(path: str, params: dict) -> dict | None:
    key = _key()
    if not key:
        return None
    p = dict(params)
    p["api_key"] = key
    p.setdefault("language", "zh-CN")
    url = _API + path + "?" + urllib.parse.urlencode(p)
    # 先直连；DNS/路由失败再复用已配置代理。429/5xx 退避重试，富化在后台线程。
    proxy = _fallback_proxy()
    channels = [None, proxy, None]
    if proxy and _CHANNEL_GOOD["proxy"] == proxy \
            and time.time() < float(_CHANNEL_GOOD["until"]):
        channels = [proxy, None, proxy]
    for attempt, proxy in enumerate(channels):
        try:
            if proxy:
                _pu = urllib.parse.urlsplit(proxy)
                _auth = None
                if _pu.username is not None:
                    _auth = (urllib.parse.unquote(_pu.username),
                             urllib.parse.unquote(_pu.password or ""))
                pool = httpcore.HTTPProxy(
                    proxy_url=proxy, proxy_auth=_auth,
                    network_backend=_IPv4Backend())
            else:
                pool = httpcore.ConnectionPool(network_backend=_IPv4Backend())
            with pool:
                r = pool.request("GET", url, extensions={
                    "timeout": {"connect": 6.0, "read": 12.0,
                                "write": 12.0, "pool": 12.0}})
                if r.status == 200:
                    if proxy:
                        _CHANNEL_GOOD.update(proxy=proxy,
                                             until=time.time() + 300)
                    return json.loads(r.content)
                if r.status in (429, 500, 502, 503, 504):
                    time.sleep(0.8 + attempt)
                    continue
                return None
        except Exception:
            if attempt < len(channels) - 1:
                time.sleep(0.5 + attempt * 0.5)
                continue
            return None
    return None


def _year_of(d: dict, is_tv: bool) -> str:
    date = d.get("first_air_date" if is_tv else "release_date") or ""
    return date[:4]


def _pick(results: list, title: str, year: str, is_tv: bool) -> dict | None:
    """在搜索结果里挑最佳：优先 标题归一化相等 + 年份相符，其次 popularity。"""
    if not results:
        return None
    nt = _norm(title)

    def score(r):
        name = r.get("name" if is_tv else "title") or ""
        orig = r.get("original_name" if is_tv else "original_title") or ""
        ry = _year_of(r, is_tv)
        s = 0
        if _norm(name) == nt or _norm(orig) == nt:
            s += 100
        if year and ry == str(year):
            s += 50
        elif year and ry and abs(int(ry) - int(year)) <= 1:
            s += 20
        s += min(float(r.get("popularity") or 0), 50) / 10
        return s

    best = max(results, key=score)
    # 至少要标题沾边或年份相符，避免完全无关的高热度片误配
    name = best.get("name" if is_tv else "title") or ""
    orig = best.get("original_name" if is_tv else "original_title") or ""
    nn, no = _norm(name), _norm(orig)
    title_match = bool(nt and (nt in nn or nn in nt
                               or (no and (nt in no or no in nt))))
    year_match = bool(year and _year_of(best, is_tv) == str(year))
    if not title_match and not year_match:
        return None
    return best


def _fetch_meta(title: str, year: str, is_tv: bool) -> dict | None:
    kind = "tv" if is_tv else "movie"
    d = _get(f"/search/{kind}", {"query": title})
    if not d:
        return None
    best = _pick(d.get("results") or [], title, year, is_tv)
    if not best:
        return None
    tid = best.get("id")
    # 详情（含 genres/runtime/评分）+ 演员表；图片用搜索/详情自带的 path
    det = _get(f"/{kind}/{tid}", {}) or {}
    credits = _get(f"/{kind}/{tid}/credits", {}) or {}
    cast = []
    for c in (credits.get("cast") or [])[:12]:
        cast.append({"id": c.get("id"),
                     "name": c.get("name", ""),
                     "original_name": c.get("original_name") or c.get("name", ""),
                     "character": c.get("character", ""),
                     "profile": img(c.get("profile_path"), "w185")})
    crew = credits.get("crew") or []
    director = next((c.get("name", "") for c in crew
                     if c.get("job") == "Director"), "")
    genres = [g.get("name", "") for g in (det.get("genres") or [])]
    backdrop = det.get("backdrop_path") or best.get("backdrop_path") or ""
    poster = det.get("poster_path") or best.get("poster_path") or ""
    return {
        "tmdb_id": tid,
        "tmdb_type": kind,
        "title": det.get("name" if is_tv else "title") or title,
        "original_title": det.get("original_name" if is_tv else "original_title") or "",
        "overview": det.get("overview") or best.get("overview") or "",
        "vote_average": round(float(det.get("vote_average")
                                    or best.get("vote_average") or 0), 1),
        "genres": genres,
        "runtime": det.get("runtime") or 0,
        "release_date": det.get("release_date")
                        or det.get("first_air_date") or "",
        "poster": img(poster, "w500"),
        "backdrop": img(backdrop, "w1280"),
        "cast": cast,
        "director": director,
        "number_of_seasons": det.get("number_of_seasons") or 0,
        "number_of_episodes": det.get("number_of_episodes") or 0,
    }


def lookup(title: str, year: str = "", is_tv: bool = False) -> dict | None:
    """同步读缓存（绝不发网络请求）；未命中返回 None。"""
    if not title:
        return None
    k = _ck(title, year, is_tv)
    with _LOCK:
        e = _CACHE.get(k)
    if not isinstance(e, dict):
        return None
    age = time.time() - float(e.get("ts") or 0)
    data = e.get("data")
    if data is None:
        return None if age < _NEG_TTL else None
    if age > _TTL:
        return None
    return data


def prefetch(title: str, year: str = "", is_tv: bool = False) -> None:
    """后台富化入口：命中缓存即跳过；否则丢线程池拉取并落盘（不阻塞调用方）。"""
    if not _key() or not title:
        return
    k = _ck(title, year, is_tv)
    with _LOCK:
        e = _CACHE.get(k)
        if isinstance(e, dict):
            age = time.time() - float(e.get("ts") or 0)
            if e.get("data") is not None and age < _TTL:
                return
            if e.get("data") is None and age < _NEG_TTL:
                return
        if k in _INFLIGHT:
            return
        _INFLIGHT.add(k)

    def _work():
        try:
            data = _fetch_meta(title, str(year or ""), is_tv)
            with _LOCK:
                _CACHE[k] = {"ts": time.time(), "data": data}
                # 控制体积：超出则清最旧
                if len(_CACHE) > 2000:
                    for old in sorted(_CACHE,
                                      key=lambda x: _CACHE[x].get("ts", 0))[:500]:
                        _CACHE.pop(old, None)
                _save()
        except Exception:
            with _LOCK:
                _CACHE[k] = {"ts": time.time(), "data": None}
        finally:
            with _LOCK:
                _INFLIGHT.discard(k)

    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _work)
        except RuntimeError:
            threading.Thread(target=_work, daemon=True).start()
    except Exception:
        threading.Thread(target=_work, daemon=True).start()


def _is_tv_brief(brief: dict) -> bool:
    """元数据搜索类型（区别于播放卡片的 is_tv，不受历史精简 lines 影响）。

    播放历史快照只保留当前一集，inject.is_tv 会因此把综艺/剧集误判成电影；
    TMDB 搜索应优先看库分类和类型名，确保综艺/动漫/剧集走 /search/tv。
    """
    ch = brief.get("chart") or {}
    cat = ch.get("cat") or brief.get("catalog_cat") or ""
    if cat in ("tv", "anime", "variety"):
        return True
    if cat == "movie":
        return False
    typ = str(brief.get("type_name") or "")
    if "电影" in typ or "剧情片" in typ:
        return False
    tv_words = ("电视剧", "连续剧", "剧集", "综艺", "真人秀", "动漫", "番剧",
                "动画剧", "日韩剧", "欧美剧", "国产剧", "大陆剧", "港台剧",
                "韩剧", "日剧", "美剧", "英剧", "泰剧")
    if any(w in typ for w in tv_words) or typ.endswith("剧"):
        return True
    from inject import is_tv as _is_tv
    return _is_tv(brief)


def _merge_metadata(db: dict | None, tm: dict | None) -> dict | None:
    """豆瓣主数据 + TMDB 横幅/缺字段兜底；TMDB backdrop 优先保持历史观感。"""
    if not db:
        return tm
    if not tm:
        return db
    out = dict(db)
    out["tmdb_id"] = tm.get("tmdb_id")
    for k in ("overview", "vote_average", "genres", "runtime", "release_date",
              "poster", "cast", "director", "original_title",
              "number_of_episodes"):
        if not out.get(k) and tm.get(k):
            out[k] = tm[k]
    if tm.get("backdrop"):
        out["backdrop"] = tm["backdrop"]
    return out


def enrich(brief: dict) -> dict | None:
    """豆瓣主用、TMDB 补图与兜底；仍只识别热门四库+海量四库。

    两个源都仅同步读缓存并各自后台补齐，不在请求线程发网络请求。范围外直接
    返回 None，不发请求也不读缓存，详情/历史回退资源站原始数据。
    """
    if not brief or not lib_allowed(brief):
        return None
    db = None
    try:
        import douban
        if douban.enabled():
            db = douban.enrich(brief)
    except Exception:
        db = None
    title = _clean_title(brief.get("title", ""))
    year = str(brief.get("year", "") or "")
    tv = _is_tv_brief(brief)
    prefetch(title, year, tv)
    tm = lookup(title, year, tv)
    return _merge_metadata(db, tm)
