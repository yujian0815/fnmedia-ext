#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源测速与全局最快线排名（通用运维能力，公开构建保留）。

与 sourcehealth（被动统计每次真实播放/搜索的成败延迟）不同，本模块【主动】按
配置间隔周期性实测每个启用的苹果 CMS 资源站：
  1) 搜索接口响应（ac=detail&wd=关键词）：可用性 + 搜索延迟；
  2) 命中的样例直链做一次 Range 首字节探测：真实出流延迟（更贴近播放体验）。
综合得分排序并持久化到 state/speedbench.json，重启不丢。播放自动选线 / preset
菜单置顶据此直接选全局最快源，免去每次播放都做一遍多线竞速；该源本次取流失败
仍由既有的“故障切换”兜底，不会因为排名过期而卡住播放。

只用标准库 + httpx，不含任何凭据/私有源信息。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse

import httpx

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "speedbench.json")

PROBE_KEYWORD = "庆余年"
_MEDIA_RE = re.compile(r"\.(m3u8|mp4)(\?|$)", re.I)
_RANGE = "bytes=0-2047"
_RESULT_TTL = 6 * 3600          # 测速结果超过 6h 视为过期，不用于自动选线
_MAX_PLAY_PROBE = 2             # 每源最多实测几条样例直链
_CONCURRENCY = 4
_BENCH_LOCK_NS = "_speedbench_running"

# 内存态：{sid: {name, ok, search_ms, play_ms, score, count, direct, ts, error}}
_RESULTS: dict[str, dict] = {}
_ORDER: list[str] = []
_UPDATED_AT = 0.0
_running = False


def _load() -> None:
    global _RESULTS, _ORDER, _UPDATED_AT
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            res = d.get("results")
            if isinstance(res, dict):
                _RESULTS = res
            ordv = d.get("order")
            if isinstance(ordv, list):
                _ORDER = [str(x) for x in ordv]
            _UPDATED_AT = float(d.get("updated_at") or 0.0)
    except Exception:
        pass


_load()


def _save_locked() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated_at": _UPDATED_AT, "order": _ORDER,
                       "results": _RESULTS}, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def _score(ok: bool, search_ms: float, play_ms: float | None) -> float:
    """越小越优。不可用源给大值沉底；可搜索但直链未测成也给较大值。"""
    if not ok:
        return 1_000_000.0
    s = float(search_ms)
    if play_ms is not None and play_ms >= 0:
        # 出流延迟更能代表体验：权重 0.65，搜索延迟 0.35
        s = 0.35 * float(search_ms) + 0.65 * float(play_ms)
    return round(s, 1)


def _extract_play_urls(list_items: list[dict], limit: int) -> list[str]:
    out: list[str] = []
    for v in list_items:
        play = v.get("vod_play_url") or ""
        for grp in play.split("$$$"):
            for ep in grp.split("#"):
                if "$" not in ep:
                    continue
                _, u = ep.split("$", 1)
                u = u.strip()
                if _MEDIA_RE.search(u):
                    out.append(u)
                    if len(out) >= limit:
                        return out
    return out


async def _probe_one(client: httpx.AsyncClient, sid: str, name: str,
                     api: str, timeout: float) -> dict:
    t0 = time.monotonic()
    row = {"id": sid, "name": name, "ok": False, "search_ms": 0,
           "play_ms": None, "score": _score(False, 0, None),
           "count": 0, "direct": 0, "ts": time.time(), "error": ""}
    try:
        r = await client.get(api, params={"ac": "detail", "wd": PROBE_KEYWORD},
                             headers={"User-Agent": "Mozilla/5.0"})
        s_ms = int((time.monotonic() - t0) * 1000)
        row["search_ms"] = s_ms
        if r.status_code != 200:
            row["error"] = f"HTTP {r.status_code}"
            return row
        j = r.json()
        if not isinstance(j, dict) or j.get("code") not in (1, "1"):
            row["error"] = "非苹果CMS JSON"
            return row
        lst = j.get("list") or []
        row["count"] = len(lst)
        urls = _extract_play_urls(lst, _MAX_PLAY_PROBE)
        row["direct"] = len(urls)
        if not urls:
            # 接口合法但无直链样例：至少搜索可用
            row["ok"] = True
            row["score"] = _score(True, s_ms, None)
            return row
        root = urllib.parse.urlparse(api)
        referer = f"{root.scheme}://{root.netloc}/"
        # 实测第一条直链首字节（Range 2KB），更贴近起播延迟
        play_ms = None
        for u in urls:
            tp = time.monotonic()
            try:
                rr = await client.get(
                    u, headers={"User-Agent": "Mozilla/5.0",
                                "Referer": referer, "Range": _RANGE},
                    follow_redirects=True)
                if rr.status_code in (200, 206):
                    play_ms = int((time.monotonic() - tp) * 1000)
                    break
            except Exception:
                continue
        row["play_ms"] = play_ms
        row["ok"] = True
        row["score"] = _score(True, s_ms, play_ms)
        return row
    except Exception as exc:
        row["error"] = type(exc).__name__
        row["search_ms"] = int((time.monotonic() - t0) * 1000)
        return row


async def benchmark(sources: list[dict], *, timeout: float = 8.0,
                    persist: bool = True) -> dict:
    """对一批源（config sources 形态：id/name/api/enabled）并发测速并更新排名。

    返回 {running, updated_at, order, results}。重复调用会被合并（同一时刻只跑一次）。
    """
    global _running, _ORDER, _UPDATED_AT
    if _running:
        return status()
    _running = True
    try:
        targets = [s for s in sources
                   if str(s.get("api", "")).startswith(("http://", "https://"))]
        sem = asyncio.Semaphore(_CONCURRENCY)
        # 直连国内源站测速（不走 Clash），与真实国内播放链路一致
        async with httpx.AsyncClient(trust_env=False, verify=False,
                                     timeout=timeout) as client:
            async def guarded(s):
                async with sem:
                    return await _probe_one(
                        client, str(s["id"]), str(s.get("name") or s["id"]),
                        str(s["api"]), timeout)
            rows = await asyncio.gather(
                *[guarded(s) for s in targets], return_exceptions=True)
        now = time.time()
        fresh = {}
        for r in rows:
            if isinstance(r, dict) and r.get("id"):
                r["ts"] = now
                fresh[r["id"]] = {k: r[k] for k in (
                    "name", "ok", "search_ms", "play_ms", "score",
                    "count", "direct", "ts", "error")}
        # 保留未参与本轮（已禁用/被删）之外的历史，仅合并本轮源；删除的源清掉
        _RESULTS.clear()
        _RESULTS.update(fresh)
        _ORDER = sorted(fresh.keys(), key=lambda k: fresh[k]["score"])
        _UPDATED_AT = now
        if persist:
            _save_locked()
        return status()
    finally:
        _running = False


def _fresh(sid: str, now: float | None = None) -> dict | None:
    now = now if now is not None else time.time()
    r = _RESULTS.get(sid)
    if not r:
        return None
    if not r.get("ok"):
        return None
    if now - float(r.get("ts") or 0) > _RESULT_TTL:
        return None
    return r


def rank_value(sid: str) -> tuple:
    """排序键，越小越优先。有测速成绩按 score；无成绩给中性档（不压过已知好源、
    也不沉底）；不可用/过期沉底。"""
    now = time.time()
    r = _fresh(sid, now)
    if r is not None:
        return (0, float(r.get("score") or 0))
    if sid in _RESULTS:
        return (2, 0)          # 测过但不可用/过期：沉底
    return (1, 0)              # 从未测速：中性，给一次机会


def is_running() -> bool:
    return _running


def status() -> dict:
    return {"running": _running, "updated_at": _UPDATED_AT,
            "order": list(_ORDER),
            "results": json.loads(json.dumps(_RESULTS, ensure_ascii=False))}


def reset() -> None:
    global _RESULTS, _ORDER, _UPDATED_AT
    _RESULTS, _ORDER, _UPDATED_AT = {}, [], 0.0
    try:
        if os.path.exists(_STATE_PATH):
            os.remove(_STATE_PATH)
    except Exception:
        pass
