#!/usr/bin/env python3
"""在线资源会话存储（持久化）。

注入的 item guid 为 md5("on_<provider>_<vodid>")。标题/线路等播放所需数据
持久化到 state/titles.json，服务重启后历史搜索卡片仍可点击播放。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import providers
import t4

_GUID_PREFIX = "on"
_TTL = 24 * 3600
_MAX_TITLES = 4000
_STATE = Path(__file__).resolve().parent.parent / "state" / "titles.json"


def _cache_cfg() -> tuple[int, int, float]:
    """读取 titles.json 缓存裁剪/落盘策略（条数上限、字节上限、防抖秒）。
    config 在函数内导入，避免与 config->providers 形成模块加载环。"""
    try:
        import config
        c = config.get()
        max_n = int(c.get("titles_cache_max_entries", _MAX_TITLES))
        max_mb = float(c.get("titles_cache_max_mb", 4))
        debounce = float(c.get("titles_flush_debounce", 5))
    except Exception:
        max_n, max_mb, debounce = _MAX_TITLES, 4.0, 5.0
    return (max(100, max_n),
            max(0.0, max_mb) * 1024 * 1024,
            max(0.0, debounce))


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
        # —— 后台防抖落盘（issue #42：机械盘同步全量写 20+MB 会让搜索卡 10s）——
        self._dirty_at = 0.0
        self._writer_started = False
        self._closing = False
        self._state: Path = _STATE    # 缓存文件路径（实例级，便于测试/端点引用）
        self._ep_index: dict[str, dict] = {}   # episode guid -> 定位信息（含多线路）
        self._media_index: dict[str, str] = {} # media/video guid -> ("t:"|"e:") + JSON
        # 索引脏标记：写入 titles 后只标记、不立即全量重建（单次重建 100ms+，安卓并发
        # 打开多个冷片时反复重建会把 0.9s 搜索拖到 2s+）。读索引前经 _ensure_index 按需
        # 重建，把短时间内的多次写入合并为一次。
        self._index_dirty = True
        self._load()

    def _ensure_writer(self):
        """惰性启动后台写线程（daemon，仅一个）。"""
        if self._writer_started:
            return
        self._writer_started = True
        t = threading.Thread(target=self._flush_loop, name="titles-flush",
                             daemon=True)
        t.start()

    def _flush_loop(self):
        """防抖写：变脏后静默满 debounce 秒（期间新脏数据顺延、合并为一次写）
        再后台落盘；debounce=0 时尽快写。"""
        while not self._closing:
            time.sleep(0.25)
            try:
                with self._lock:
                    if not self._dirty:
                        continue
                    since = time.time() - self._dirty_at
                    _, _, debounce = _cache_cfg()
                if since < debounce:
                    continue
                self._flush_now()
            except Exception:
                pass

    def _schedule_flush(self):
        """标记脏并请求后台合并写；搜索热路径不做任何磁盘 IO。"""
        now = time.time()
        with self._lock:
            if not self._dirty:
                self._dirty_at = now
            self._dirty = True
        self._ensure_writer()

    def _load(self):
        try:
            j = json.loads(self._state.read_text(encoding="utf-8"))
            self._titles = j.get("titles", {})
            self._orig = j.get("orig", {})
        except Exception:
            self._titles, self._orig = {}, {}
        self._rebuild_index()
        self._index_dirty = False

    def _direct_eps(self, brief: dict, line: str) -> list[dict]:
        # 可直接播放，或可按需解析的播放页（/share/<id>）
        return [e for e in brief.get("lines", {}).get(line, [])
                if e.get("direct") or e.get("resolve") or e.get("t4")]

    def _t4_rows(self, brief: dict) -> list[dict]:
        rows = {}
        default = self.best_line(brief["guid"])
        lines = ([default] if default else []) + [
            ln for ln in brief.get("lines", {}) if ln != default]
        for line in lines:
            for key, ep in t4.playlist_entries(self._direct_eps(brief, line), line):
                row = rows.setdefault(key, {
                    "key": key, "title": t4.episode_menu_title(ep["title"], key), "per": {}})
                row["per"][line] = ep
        def order(row):
            key = row["key"]
            number = t4.episode_number(key)
            season = t4.episode_season(key)
            return (0 if number and season else 1, season, number, key)
        return sorted(rows.values(), key=order)

    def episode_guid_at(self, og: str, index: int) -> str:
        brief = self.get_title_by_guid(og) or {}
        if brief.get("locator", {}).get("kind") == "t4":
            guids = self._t4_episode_guids.get(og, [])
            if not 0 <= index < len(guids):
                return ""
            return guids[index]
        return episode_guid(og, "", index)

    @staticmethod
    def t4_episode_guid(og: str, key: str) -> str:
        return _h(f"t4ep:{og}:{key}")

    def _index_t4(self, g: str, brief: dict):
        rows = self._t4_rows(brief)
        self._t4_episode_guids[g] = []
        numbered = any(t4.episode_number(row["key"]) for row in rows)
        positions = {}
        for index, row in enumerate(rows):
            eg = _h(f"t4ep:{g}:{row['key']}")
            self._t4_episode_guids[g].append(eg)
            per = row["per"]
            default = next(iter(per))
            season = t4.episode_season(row["key"]) if (
                t4.episode_number(row["key"]) or not numbered) else 0
            positions[season] = positions.get(season, 0) + 1
            self._ep_index[eg] = {
                "ep_guid": eg, "online_guid": g, "index": index, "season": season,
                "episode_number": t4.episode_number(row["key"]) or positions[season],
                "season_guid": season_guid(g, season), "title": brief["title"],
                "default_line": default, "lines": list(per), "_per": {
                    ln: dict(e, ep_title=e["title"]) for ln, e in per.items()},
                "ep_title": row["title"], "line": default, "t4": True,
                "episode_key": row["key"],
            }
            for line in [""] + list(per):
                meta = "e:" + json.dumps({"ep": eg, "line": line or default})
                self._media_index[ep_media_guid(eg, line)] = meta
                self._media_index[ep_video_guid(eg, line)] = meta

    def _ensure_index(self):
        """读索引前调用：仅在脏时重建，合并短时间内的多次写入。

        RLock 可重入，已持锁的写路径调用也安全；重建本身是纯内存操作。"""
        if not self._index_dirty:
            return
        with self._lock:
            if self._index_dirty:
                self._rebuild_index()
                self._index_dirty = False

    def _rebuild_index(self):
        """重建 ep / media 反查索引（集身份与线路解耦，media guid 携带线路）。"""
        self._ep_index = {}
        self._media_index = {}
        self._t4_episode_guids = {}
        for g, brief in self._titles.items():
            # 标题（电影=整段；剧集默认线路第一集）
            self._media_index[media_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
            self._media_index[video_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
            if brief.get("locator", {}).get("kind") == "t4":
                self._index_t4(g, brief)
                continue
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
        self._season_index = {}
        self._t4_seasons = {}
        for g, guids in self._t4_episode_guids.items():
            for eg in guids:
                ep = self._ep_index[eg]
                sg = ep["season_guid"]
                if sg not in self._season_index:
                    self._t4_seasons.setdefault(g, []).append(sg)
                self._season_index.setdefault(sg, {
                    "online_guid": g, "season": ep["season"], "episode_guids": [],
                })["episode_guids"].append(eg)

    def resolve_media_guid(self, mg: str) -> Optional[dict]:
        """media/video guid -> 可播放目标（含 url/line/index）。"""
        self._ensure_index()
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
        self._ensure_index()
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        ln = line if line in ep["_per"] else ep["default_line"]
        if ep.get("t4") and line and line not in ep["_per"]:
            return None
        one = ep["_per"][ln]
        return {
            "ep_guid": ep_g, "online_guid": ep["online_guid"], "line": ln,
            "index": ep["index"], "url": one["url"], "headers": one["headers"],
            "ep_title": one["ep_title"] or ep["ep_title"], "title": ep["title"],
            "resolve": one.get("resolve", False),
            "redirect": one.get("redirect", False),
            "xsrc": one.get("xsrc", False),
            "play_from": one.get("play_from", ""),
            "t4": bool(one.get("t4")),
            "episode_key": one.get("episode_key", ""),
            "total": self.episode_count(ep["online_guid"]) if ep.get("t4") else
            len(self._direct_eps(self._titles[ep["online_guid"]], ep["default_line"])),
            "season_guid": ep["season_guid"], "season": ep.get("season", 1),
            "episode_number": ep.get("episode_number", ep["index"] + 1), "kind": "episode",
        }

    def get_episode(self, ep_g: str) -> Optional[dict]:
        self._ensure_index()
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        d = dict(ep)
        d.pop("_per", None)
        return d

    def tv_episode_guids(self, og: str, *, regular_only: bool = False) -> list[str]:
        """某 TV（默认线路）按顺序的全部集 guid（与 episode/list 一致）。"""
        self._ensure_index()
        line = self.best_line(og)
        if not line:
            return []
        if self._titles[og].get("locator", {}).get("kind") == "t4":
            guids = list(self._t4_episode_guids.get(og, []))
            regular = [eg for eg in guids if self._ep_index[eg]["season"] > 0]
            return regular if regular_only and regular else guids
        n = len(self._direct_eps(self._titles[og], line))
        return [episode_guid(og, "", i) for i in range(n)]

    def seasons(self, og: str) -> list[dict]:
        brief = self.get_title_by_guid(og) or {}
        if brief.get("locator", {}).get("kind") != "t4":
            return [{"guid": season_guid(og), "season": 1,
                     "episode_guids": self.tv_episode_guids(og)}]
        rows = [dict(self._season_index[sg], guid=sg)
                for sg in self._t4_seasons.get(og, [])]
        return sorted(rows, key=lambda row: (row["season"] == 0, row["season"]))

    def get_season(self, sg: str) -> Optional[dict]:
        self._ensure_index()
        meta = self._season_index.get(sg)
        if meta is not None:
            return dict(meta)
        for g, brief in self._titles.items():
            if brief.get("locator", {}).get("kind") != "t4" and season_guid(g) == sg:
                return {"online_guid": g, "season": 1,
                        "episode_guids": self.tv_episode_guids(g)}
        return None

    def episode_lines(self, ep_g: str) -> list[str]:
        self._ensure_index()
        ep = self._ep_index.get(ep_g)
        return list(ep["_per"].keys()) if ep else []

    def target_lines(self, target: dict) -> list[dict]:
        """给定 resolve 出的播放目标，枚举该集（或电影正片）在各线路下的地址。
        返回 [{line,url,m3u8}]，默认线路排最前。"""
        og = target.get("online_guid")
        brief = self._titles.get(og)
        if not brief:
            return []
        if brief.get("locator", {}).get("kind") == "t4":
            eg = target.get("ep_guid") or self.episode_guid_at(
                og, int(target.get("index") or 0))
            return [dict(self._ep_resolve(eg, line), m3u8=False)
                    for line in self.episode_lines(eg)]
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

    def _prune_memory_locked(self, keep_guards: set):
        """内存软上限：条数远超配置上限时，丢最旧的非保护条目。
        磁盘裁剪只影响落盘快照；这个防止长跑进程内存与索引无界增长（issue #42）。"""
        max_n, _, _ = _cache_cfg()
        ceiling = int(max_n * 1.5)
        if len(self._titles) <= ceiling:
            return False
        keys = [k for k in self._titles.keys() if k not in keep_guards]
        drop_n = len(self._titles) - max_n
        for k in keys[:max(0, drop_n)]:
            self._titles.pop(k, None)
            self._orig.pop(k, None)
        return True

    def _snapshot_capped(self) -> tuple[dict, dict]:
        """在锁内按「条数上限 + 字节上限」裁剪出待落盘快照。
        只裁剪落盘内容，不动内存；按插入顺序丢最旧（dict 保序，最近搜索在后）。"""
        max_n, max_bytes, _ = _cache_cfg()
        g_items = list(self._titles.items())
        o_items = list(self._orig.items())
        # 1) 条数上限
        if len(g_items) > max_n:
            g_items = g_items[-max_n:]
            keep = {k for k, _ in g_items}
            o_items = [(k, v) for k, v in o_items if k in keep]
        # 2) 字节上限（0=不限制）。逐条算【UTF-8 字节】长度（titles + 对应 orig；
        #    中文在磁盘上 3 字节/字，不能用字符 len 估算），从最旧丢到达标，留 2% 余量。
        if max_bytes > 0:
            def _bsize(v) -> int:
                return len(json.dumps(v, ensure_ascii=False).encode("utf-8"))
            o_map = dict(o_items)
            budget = max_bytes * 0.92
            sizes = [_bsize(b) + _bsize(o_map.get(k, "")) for k, b in g_items]
            total = sum(sizes) + 256
            drop = 0
            while total > budget and drop < len(g_items):
                total -= sizes[drop]
                drop += 1
            if drop:
                g_items = g_items[drop:]
                keep = {k for k, _ in g_items}
                o_items = [(k, v) for k, v in o_items if k in keep]
        return dict(g_items), dict(o_items)

    def _flush_now(self) -> bool:
        """实际落盘：锁内取裁剪快照，锁外序列化+原子替换（写盘不阻塞搜索/读）。"""
        if not self._dirty:
            return False
        with self._lock:
            if not self._dirty:
                return False
            titles, orig = self._snapshot_capped()
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"titles": titles, "orig": orig},
                              ensure_ascii=False)
            tmp = self._state.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self._state)
            with self._lock:
                self._dirty = False
            return True
        except Exception:
            return False

    def flush(self, wait: bool = True) -> bool:
        """对外的立即落盘（关闭进程时调用）。wait=True 时同步写完。"""
        with self._lock:
            if not self._dirty:
                return True
        if wait:
            return self._flush_now()
        self._ensure_writer()
        return True

    def close(self):
        """进程关闭：停掉防抖线程并尽力把最后一批脏数据写盘。"""
        self._closing = True
        try:
            self._flush_now()
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
            self._prune_memory_locked({make_guid(t) for t in titles})
            self._dirty = True
            self._schedule_flush()
            # T4 索引依赖写入时的内存数据，必须立即重建；普通 CMS 走惰性合并。
            if any(b.get("locator", {}).get("kind") == "t4" for b in briefs):
                self._rebuild_index()
                self._index_dirty = False
            else:
                self._index_dirty = True
        return briefs

    def remember_merged(self, briefs: list[dict]) -> list[dict]:
        """保存跨源合并后的 brief（guid 已在 brief 内确定）。"""
        with self._lock:
            for b in briefs:
                old = self._titles.get(b["guid"])
                if b.get("locator", {}).get("kind") == "t4" and old and old.get("lines"):
                    # A search card must not erase loaded detail or historical episode IDs.
                    locator = dict(b["locator"], loaded_at=0)
                    b = dict(b, lines=old["lines"], locator=locator)
                self._titles[b["guid"]] = b
                self._orig.setdefault(b["guid"], b.get("online_id", ""))
            self._prune_memory_locked({b["guid"] for b in briefs})
            self._dirty = True
            self._schedule_flush()
            # T4 索引依赖写入时的内存数据，必须立即重建；普通 CMS 走惰性合并，
            # 把并发冷片绑定的多次全量重建（单次 100ms+）合并为读取时一次。
            if any(b.get("locator", {}).get("kind") == "t4" for b in briefs):
                self._rebuild_index()
                self._index_dirty = False
            else:
                self._index_dirty = True
        return briefs

    def replace_t4_detail(self, guid: str, title: providers.OnlineTitle) -> dict:
        import inject
        with self._lock:
            brief = dict(inject.brief_of(title), guid=guid)
            self._titles[guid] = brief
            self._orig[guid] = title.online_id
            self._dirty = True
            self._schedule_flush()
            # T4 索引依赖写入时的内存数据，必须立即重建。
            self._rebuild_index()
            self._index_dirty = False
            return brief

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
            self._prune_memory_locked({g})
            self._dirty = True
            self._schedule_flush()
            # T4 索引依赖写入时的内存数据，必须立即重建（后续不允许重扫文件）。
            self._rebuild_index()
            self._index_dirty = False
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
        return [e for e in eps if e.get("direct") or e.get("resolve") or e.get("t4")]

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
        if brief.get("locator", {}).get("kind") == "t4" and not line:
            self._ensure_index()
            return len(self._t4_episode_guids.get(guid, []))
        ln = line if line in brief.get("lines", {}) else self.best_line(guid)
        if not ln:
            return 0
        return len(self._playable(brief["lines"][ln]))

    def season_episode(self, season_g: str, index: int) -> Optional[dict]:
        """由伪造的 season guid 与集序号返回在线标题 guid / 线路 / 集。"""
        meta = self.get_season(season_g)
        if meta:
            guids = meta["episode_guids"]
            return self.get_episode_target(guids[index]) if 0 <= index < len(guids) else None
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
        if brief.get("locator", {}).get("kind") == "t4":
            eg = self.episode_guid_at(guid, int(ep_index or 0))
            return self._ep_resolve(eg, line) if eg else None
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
                # 补 m3u8 标记：_episode_candidates 复用本结果，缺标记会让 .m3u8 直链
                # 在探测时被当成非 HLS 走 Range 分支、拿到清单文本误判死线（秒播退化根因）。
                "m3u8": bool(re.search(r"\.m3u8(\?|$)", ep.get("url", ""), re.I)),
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
