#!/usr/bin/env python3
"""CMS 源批量探活：受控并发、实时进度、可取消、结果保留。

借鉴 LibreTV use-source-probe：默认并发 4、最多 100 个源；命中任一热门词即判
有效。结果回写 sourcehealth，与起播/竞速共用同一套健康画像，不另建标准。
"""
from __future__ import annotations

import asyncio
import time

import providers
import sourcehealth

_state: dict = {"running": False, "total": 0, "done": 0, "alive_count": 0,
                "results": [], "started_at": 0.0, "error": ""}
_task: asyncio.Task | None = None
_run_id = 0


def status() -> dict:
    return {**_state, "results": list(_state["results"])}


async def start(sources: list[dict], *, concurrency: int = 4,
                limit: int = 100) -> dict:
    global _task, _run_id
    if _state["running"]:
        return status()
    _run_id += 1
    rid = _run_id
    targets = list(sources)[:max(1, min(100, int(limit)))]
    _state.update(running=True, total=len(targets), done=0, alive_count=0,
                  results=[], started_at=time.time(), error="")
    cursor = 0
    lock = asyncio.Lock()

    async def one(source: dict):
        nonlocal cursor
        async with lock:
            cursor += 1
            index = cursor
        try:
            result = await providers.probe_source_alive(source)
        except Exception as exc:  # noqa: BLE001
            result = {"alive": False, "ok": False, "elapsed_ms": 0,
                      "count": 0, "error": type(exc).__name__,
                      "hit": "", "tried": []}
        if rid != _run_id:
            return
        key = sourcehealth.source_key_of(source.get("id"), source) or source.get("id", "")
        if key and result.get("alive") is not None:
            sourcehealth.record(key, bool(result.get("alive")),
                                (result.get("elapsed_ms") or 0) / 1000.0)
        row = {"id": source.get("id"), "name": source.get("name", ""),
               "alive": bool(result.get("alive")), "hit": result.get("hit", ""),
               "tried": result.get("tried", []), "count": result.get("count", 0),
               "elapsed_ms": result.get("elapsed_ms", 0),
               "status": result.get("status", 0), "error": result.get("error", ""),
               "dead": sourcehealth.is_dead(key) if key else False}
        async with lock:
            _state["results"].append(row)
            _state["done"] += 1
            _state["alive_count"] += int(row["alive"])

    async def worker():
        while rid == _run_id:
            async with lock:
                if cursor >= len(targets):
                    return
                source = targets[cursor]
            await one(source)

    async def job():
        try:
            await asyncio.gather(*[worker() for _ in range(
                max(1, min(int(concurrency), len(targets) or 1)))])
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _state["error"] = str(exc)[:200]
        finally:
            if rid == _run_id:
                _state["running"] = False

    _task = asyncio.create_task(job(), name=f"cms-source-probe-{rid}")
    return status()


def cancel() -> dict:
    global _run_id
    _run_id += 1
    if _task and not _task.done():
        _task.cancel()
    _state["running"] = False
    return status()


async def wait() -> dict:
    """等待本轮结束（测试/诊断用）；生产轮询 status 即可。"""
    task = _task
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass
    return status()
