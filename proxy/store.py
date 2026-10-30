#!/usr/bin/env python3
"""在线资源会话存储（持久化）。

注入的 item guid 为 md5("on_<provider>_<vodid>")。标题/线路等播放所需数据
持久化到 state/titles.json，服务重启后历史搜索卡片仍可点击播放。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import providers

_GUID_PREFIX = "on"
_TTL = 24 * 3600
_MAX_TITLES = 4000
_STATE = Path(__file__).resolve().parent.parent / "state" / "titles.json"


def _h(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def make_guid(t: providers.OnlineTitle) -> str:
    return _h(f"{_GUID_PREFIX}_{t.provider}_{t.vod_id}")


def media_guid(online_guid: str) -> str:
    return _h("media:" + online_guid)


def video_guid(online_guid: str) -> str:
    return _h("video:" + online_guid)


def season_guid(online_guid: str, season: int = 1) -> str:
    return _h(f"season{season}:{online_guid}")


def episode_guid(online_guid: str, line: str = "", index: int = 0) -> str:
    """集 guid 不绑定线路（按集序号），以便多线路切换时同一集保持同一身份。"""
    return _h(f"ep:{online_guid}:{index}")


def ep_media_guid(ep_g: str, line: str = "") -> str:
    # 线路并入 media guid：同一集切换线路会得到不同 play 目标
    return _h("media:" + ep_g + (f":{line}" if line else ""))


def ep_video_guid(ep_g: str, line: str = "") -> str:
    return _h("video:" + ep_g + (f":{line}" if line else ""))


def is_online_guid(guid: str) -> bool:
    return store.get_title_by_guid(guid) is not None


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._titles: dict[str, dict] = {}
        self._orig: dict[str, str] = {}
        self._play: dict[str, dict] = {}
        self._detail_cache: dict[str, providers.OnlineTitle] = {}
        self._dirty = False
        self._ep_index: dict[str, dict] = {}   # episode guid -> 定位信息（含多线路）
        self._media_index: dict[str, str] = {} # media/video guid -> ("t:"|"e:") + JSON
        self._load()

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
            self._titles = j.get("titles", {})
            self._orig = j.get("orig", {})
        except Exception:
            self._titles, self._orig = {}, {}
        self._rebuild_index()

    def _direct_eps(self, brief: dict, line: str) -> list[dict]:
        # 可直接播放，或可按需解析的播放页（/share/<id>）
        return [e for e in brief.get("lines", {}).get(line, [])
                if e.get("direct") or e.get("resolve")]

    def _rebuild_index(self):
        """重建 ep / media 反查索引（集身份与线路解耦，media guid 携带线路）。"""
        self._ep_index = {}
        self._media_index = {}
        for g, brief in self._titles.items():
            # 标题（电影=整段；剧集默认线路第一集）
            self._media_index[media_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
            self._media_index[video_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
            default = self.best_line(g)
            if not default:
                continue
            line_names = list(brief.get("lines", {}).keys())
            # 以默认线路的集数为基准建立集身份，聚合各线路
            base = self._direct_eps(brief, default)
            for i, base_e in enumerate(base):
                eg = episode_guid(g, "", i)
                per_line = {}
                for ln in line_names:
                    eps = self._direct_eps(brief, ln)
                    if i < len(eps):
                        e = eps[i]
                        per_line[ln] = {"url": e["url"],
                                        "headers": e.get("headers", {}),
                                        "ep_title": e.get("title", ""),
                                        "resolve": bool(e.get("resolve")),
                                        "redirect": bool(e.get("redirect")),
                                        "xsrc": bool(e.get("xsrc") or e.get("dduo")),
                                        "play_from": e.get("play_from", "")}
                self._ep_index[eg] = {
                    "ep_guid": eg, "online_guid": g, "index": i, "season": 1,
                    "season_guid": season_guid(g), "title": brief.get("title", ""),
                    "default_line": default, "lines": list(per_line.keys()),
                    "_per": per_line,
                    "ep_title": base_e.get("title", ""), "line": default,
                }
                # 每个有该集的线路都映射一个 media/video guid
                for ln in per_line:
                    self._media_index[ep_media_guid(eg, ln)] = \
                        "e:" + json.dumps({"ep": eg, "line": ln})
                    self._media_index[ep_video_guid(eg, ln)] = \
                        "e:" + json.dumps({"ep": eg, "line": ln})
                # 空线路=默认线路（兼容）
                self._media_index[ep_media_guid(eg, "")] = \
                    "e:" + json.dumps({"ep": eg, "line": default})
                self._media_index[ep_video_guid(eg, "")] = \
                    "e:" + json.dumps({"ep": eg, "line": default})

    def resolve_media_guid(self, mg: str) -> Optional[dict]:
        """media/video guid -> 可播放目标（含 url/line/index）。"""
        ref = self._media_index.get(mg)
        if not ref:
            return None
        kind, payload = ref[:1], ref[2:]
        try:
            meta = json.loads(payload)
        except Exception:
            return None
        if kind == "e":
            return self._ep_resolve(meta["ep"], meta.get("line"))
        r = self.resolve_episode(meta["og"], meta.get("line") or None)
        if not r:
            return None
        return dict(r, online_guid=meta["og"], ep_guid="", kind="title")

    def get_episode_target(self, ep_g: str, line: str | None = None) -> Optional[dict]:
        """供播放层按指定线路解析某一集（line 非法时回退默认线路）。"""
        return self._ep_resolve(ep_g, line)

    def _ep_resolve(self, ep_g: str, line: str | None = None) -> Optional[dict]:
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        ln = line if line in ep["_per"] else ep["default_line"]
        one = ep["_per"][ln]
        return {
            "ep_guid": ep_g, "online_guid": ep["online_guid"], "line": ln,
            "index": ep["index"], "url": one["url"], "headers": one["headers"],
            "ep_title": one["ep_title"] or ep["ep_title"], "title": ep["title"],
            "resolve": one.get("resolve", False),
            "redirect": one.get("redirect", False),
            "xsrc": one.get("xsrc", False),
            "play_from": one.get("play_from", ""),
            "total": len(self._direct_eps(self._titles[ep["online_guid"]],
                                          ep["default_line"])),
            "season_guid": ep["season_guid"], "season": 1, "kind": "episode",
        }

    def get_episode(self, ep_g: str) -> Optional[dict]:
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        d = dict(ep)
        d.pop("_per", None)
        return d

    def tv_episode_guids(self, og: str) -> list[str]:
        """某 TV（默认线路）按顺序的全部集 guid（与 episode/list 一致）。"""
        line = self.best_line(og)
        if not line:
            return []
        n = len(self._direct_eps(self._titles[og], line))
        return [episode_guid(og, "", i) for i in range(n)]

    def episode_lines(self, ep_g: str) -> list[str]:
        ep = self._ep_index.get(ep_g)
        return list(ep["_per"].keys()) if ep else []

    def target_lines(self, target: dict) -> list[dict]:
        """给定 resolve 出的播放目标，枚举该集（或电影正片）在各线路下的地址。
        返回 [{line,url,m3u8}]，默认线路排最前。"""
        og = target.get("online_guid")
        brief = self._titles.get(og)
        if not brief:
            return []
        idx = int(target.get("index", 0) or 0)
        default = self.best_line(og)
        out, seen_url = [], set()
        ordered = ([default] if default else []) + \
                  [ln for ln in brief.get("lines", {}) if ln != default]
        for ln in ordered:
            eps = self._direct_eps(brief, ln)
            if idx >= len(eps):
                continue
            url = eps[idx]["url"]
            if url in seen_url:
                continue
            seen_url.add(url)
            out.append({"line": ln, "url": url,
                        "m3u8": bool(re.search(r"\.m3u8(\?|$)", url, re.I)),
                        "redirect": bool(eps[idx].get("redirect")),
                        "resolve": bool(eps[idx].get("resolve")),
                        "xsrc": bool(eps[idx].get("xsrc") or eps[idx].get("dduo")),
                        "play_from": eps[idx].get("play_from", ""),
                        "online_guid": og, "index": idx,
                        "headers": eps[idx].get("headers", {})})
        return out

    def title_lines(self, og: str) -> list[str]:
        """该标题有直链的线路名（按直链集数降序）。"""
        brief = self._titles.get(og)
        if not brief:
            return []
        out = [(ln, len(self._direct_eps(brief, ln)))
               for ln in brief.get("lines", {})]
        return [ln for ln, n in sorted(out, key=lambda x: -x[1]) if n > 0]

    def _flush_locked(self, force=False):
        now = time.time()
        if not self._dirty:
            return
        # 限频写盘（最多每 3 秒一次由调用方触发，这里直接写）
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE.with_suffix(".tmp")
            # 控制磁盘体积：超限时只裁剪本次写入的快照，不能从内存中删除。
            # 否则热门库 remember_merged 后可能误删用户此前搜索缓存的标题。
            titles = self._titles
            orig = self._orig
            if len(titles) > _MAX_TITLES:
                keys = list(titles.keys())[-_MAX_TITLES:]
                keep = set(keys)
                titles = {k: self._titles[k] for k in keys}
                orig = {k: v for k, v in self._orig.items() if k in keep}
            tmp.write_text(json.dumps(
                {"titles": titles, "orig": orig}, ensure_ascii=False))
            tmp.replace(_STATE)
            self._dirty = False
        except Exception:
            pass

    def remember_titles(self, titles: list[providers.OnlineTitle]) -> list[dict]:
        import inject
        now = time.time()
        briefs = []
        with self._lock:
            for t in titles:
                g = make_guid(t)
                b = inject.brief_of(t)
                self._titles[g] = b
                self._orig[g] = t.online_id
                self._detail_cache[t.online_id] = t
                briefs.append(b)
            self._play = {k: v for k, v in self._play.items() if now - v["ts"] < _TTL}
            self._dirty = True
            self._flush_locked()
            self._rebuild_index()
        return briefs

    def remember_merged(self, briefs: list[dict]) -> list[dict]:
        """保存跨源合并后的 brief（guid 已在 brief 内确定）。"""
        with self._lock:
            for b in briefs:
                self._titles[b["guid"]] = b
                self._orig.setdefault(b["guid"], b.get("online_id", ""))
            self._dirty = True
            self._flush_locked(force=True)
            self._rebuild_index()
        return briefs

    def get_title_by_guid(self, guid: str) -> Optional[dict]:
        with self._lock:
            return self._titles.get(guid)

    def find_title_with_line(self, title: str, line: str) -> Optional[str]:
        """同名标题常因不同搜索/聚合路径各存一条、且只挂部分线路。当播放页记录里
        没有用户所选线路时，按片名找到「包含该线路、线路最多」的另一条记录，返回其
        guid，避免静默退回第一条死链。"""
        if not title or not line:
            return None
        best, best_n = None, -1
        disp = line.split("::", 1)[1] if "::" in line else line
        with self._lock:
            for g, b in self._titles.items():
                if b.get("title") != title:
                    continue
                lines = b.get("lines") or {}
                hit = line in lines or any(
                    k == line or ("::" in k and k.split("::", 1)[1] == disp)
                    for k in lines)
                if hit and len(lines) > best_n:
                    best, best_n = g, len(lines)
        return best

    def provider_of(self, online_guid: str) -> str:
        """guid 对应标题所属资源源 id（来自 online_id "<provider>:<vod_id>"）。"""
        with self._lock:
            return (self._orig.get(online_guid, "") or "").split(":", 1)[0]

    def remember_detail(self, t: providers.OnlineTitle) -> str:
        import inject
        with self._lock:
            g = make_guid(t)
            self._titles[g] = inject.brief_of(t)
            self._orig[g] = t.online_id
            self._detail_cache[t.online_id] = t
            self._dirty = True
            self._flush_locked()
            self._rebuild_index()
        return g

    def get_detail(self, online_id: str) -> Optional[providers.OnlineTitle]:
        with self._lock:
            return self._detail_cache.get(online_id)

    def online_guid_by_media(self, mg: str) -> Optional[str]:
        with self._lock:
            for g in self._titles:
                if media_guid(g) == mg or video_guid(g) == mg:
                    return g
        return None

    # ---- 季 / 集（原生 TV 选集）----
    @staticmethod
    def _playable(eps: list[dict]) -> list[dict]:
        return [e for e in eps if e.get("direct") or e.get("resolve")]

    def best_line(self, guid: str) -> str | None:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        # 直链线路优先于仅可解析的线路
        best, bestn, best_direct = None, -1, False
        for ln, eps in brief.get("lines", {}).items():
            play = self._playable(eps)
            nd = sum(1 for e in play if e.get("direct"))
            if not play:
                continue
            direct = nd > 0
            if (direct != best_direct and direct) or \
               (direct == best_direct and len(play) > bestn):
                best, bestn, best_direct = ln, len(play), direct
        return best

    def episode_count(self, guid: str, line: str | None = None) -> int:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return 0
        ln = line if line in brief.get("lines", {}) else self.best_line(guid)
        if not ln:
            return 0
        return len(self._playable(brief["lines"][ln]))

    def season_episode(self, season_g: str, index: int) -> Optional[dict]:
        """由伪造的 season guid 与集序号返回在线标题 guid / 线路 / 集。"""
        for g in self._titles:
            if season_guid(g) == season_g:
                ln = self.best_line(g)
                return self.resolve_episode(g, ln, index)
        return None

    def resolve_episode_guid(self, ep_g: str) -> Optional[dict]:
        """由伪造的 episode guid 反查真实播放地址。"""
        for g, brief in self._titles.items():
            for ln, eps in brief.get("lines", {}).items():
                dirs = [e for e in eps if e.get("direct")]
                for i, e in enumerate(dirs):
                    if episode_guid(g, ln, i) == ep_g:
                        return {"online_guid": g, "url": e["url"],
                                "headers": e.get("headers", {}), "line": ln,
                                "index": i, "total": len(dirs),
                                "ep_title": e["title"], "title": brief.get("title", "")}
        return None

    def list_episodes(self, guid: str, line: str | None = None) -> list[dict]:
        """返回某线路（或第一条可播线路）下所有可直放剧集。"""
        brief = self.get_title_by_guid(guid)
        if not brief:
            return []
        lines = brief.get("lines", {})
        name = line if line in lines else next(iter(lines), None)
        if name is None:
            return []
        return [dict(e, _line=name) for e in lines[name] if e.get("direct")]

    def resolve_episode(self, guid: str, line: str | None = None,
                        ep_index: int = 0) -> Optional[dict]:
        """解析出某一集的真实地址。返回 {url,headers,line,index,total,ep_title}。"""
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        lines = brief.get("lines", {})
        # 选线路：指定 > 第一条可播（直链优先，其次可解析页）
        name = None
        if line and line in lines and self._playable(lines[line]):
            name = line
        else:
            # 直链优先
            for ln, eps in lines.items():
                if any(e.get("direct") for e in eps):
                    name = ln
                    break
            if name is None:
                for ln, eps in lines.items():
                    if self._playable(eps):
                        name = ln
                        break
        if name is None:
            return None
        eps = self._playable(lines[name])
        if not eps:
            return None
        ep_index = max(0, min(int(ep_index or 0), len(eps) - 1))
        ep = eps[ep_index]
        return {"url": ep["url"], "headers": ep.get("headers", {}),
                "line": name, "index": ep_index, "total": len(eps),
                "resolve": bool(ep.get("resolve")),
                "redirect": bool(ep.get("redirect")),
                "xsrc": bool(ep.get("xsrc") or ep.get("dduo")),
                "play_from": ep.get("play_from", ""),
                "online_guid": guid,
                "ep_title": ep["title"], "title": brief.get("title", "")}

    def first_playable(self, guid: str) -> Optional[dict]:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        for line, eps in brief["lines"].items():
            for ep in eps:
                if ep.get("direct"):
                    return {"line": line, "ep": ep}
        return None

    def create_play(self, guid: str, line: str | None = None, ep_index: int = 0):
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        lines = brief["lines"]
        name = line if line in lines else next(iter(lines), None)
        if name is None:
            return None
        eps = [e for e in lines[name] if e.get("direct")]
        if not eps:
            return None
        ep_index = max(0, min(ep_index, len(eps) - 1))
        ep = eps[ep_index]
        psid = uuid.uuid4().hex
        with self._lock:
            self._play[psid] = {
                "url": ep["url"], "headers": ep.get("headers", {}),
                "title": brief["title"], "ep": ep["title"], "ts": time.time()}
        return {"psid": psid, "line": name, "ep": ep, "index": ep_index,
                "total": len(eps)}

    def get_play(self, psid: str) -> Optional[dict]:
        with self._lock:
            return self._play.get(psid)


store = Store()
