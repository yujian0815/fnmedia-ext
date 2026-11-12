#!/usr/bin/env python3
"""订阅注册与有界自动刷新（LibreTV 语义，独立实现）。

- 周期按订阅各自 refresh_days；0 = 纯手动。
- 单次最多 5 个、并发 2、连续失败 3 次后暂停自动刷新。
- 拉取/解析失败只记录错误，不删除旧源；手动成功一次清除失败计数。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "subscriptions.json"
_REFRESH_TASK: asyncio.Task | None = None


def _load() -> dict:
    try:
        if _STATE.exists():
            data = json.loads(_STATE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("subscriptions") or {}
    except Exception:
        pass
    return {}


def _save(items: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"subscriptions": items}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_STATE)


def _days(value) -> int:
    try:
        return max(0, min(365, int(value))
        )
    except (TypeError, ValueError):
        return 1


def register(url: str, *, name: str = "", refresh_days=1) -> dict:
    url = str(url or "").strip()
    if not url:
        return {}
    items = _load()
    old = items.get(url) or {}
    rec = {"url": url, "name": name or old.get("name", ""),
           "refresh_days": _days(refresh_days if refresh_days is not None
                                  else old.get("refresh_days", 1)),
           "last_sync": int(time.time()), "fail_streak": 0, "last_error": ""}
    items[url] = rec
    _save(items)
    return rec


def set_refresh_days(url: str, days) -> dict:
    items = _load()
    rec = items.get(url) or {"url": url, "last_sync": 0, "fail_streak": 0,
                             "last_error": ""}
    rec["refresh_days"] = _days(days)
    rec["fail_streak"] = 0
    rec["last_error"] = ""
    items[url] = rec
    _save(items)
    return rec


def list_subscriptions() -> list[dict]:
    return sorted(_load().values(), key=lambda x: x.get("url", ""))


def mark_success(url: str) -> None:
    items = _load()
    if url not in items:
        return
    items[url]["last_sync"] = int(time.time())
    items[url]["fail_streak"] = 0
    items[url]["last_error"] = ""
    _save(items)


def mark_failure(url: str, message: str) -> None:
    items = _load()
    if url not in items:
        return
    items[url]["fail_streak"] = int(items[url].get("fail_streak", 0)) + 1
    items[url]["last_error"] = str(message)[:300]
    _save(items)


def due(now: float | None = None, *, limit: int = 5) -> list[dict]:
    now = now or time.time()
    out = []
    for rec in list_subscriptions():
        days = int(rec.get("refresh_days") or 0)
        if days <= 0 or int(rec.get("fail_streak", 0)) >= 3:
            continue
        last = float(rec.get("last_sync") or 0)
        if not last or now - last >= days * 86400:
            out.append(rec)
    return out[:max(1, min(5, int(limit)))]


async def refresh_due(refresh_func, now: float | None = None) -> dict:
    targets = due(now)
    results = []
    cursor = 0

    async def worker():
        nonlocal cursor
        while cursor < len(targets):
            rec = targets[cursor]
            cursor += 1
            try:
                await refresh_func(rec["url"])
            except Exception as exc:  # noqa: BLE001
                mark_failure(rec["url"], str(exc))
                results.append({"url": rec["url"], "ok": False,
                                "error": str(exc)[:200]})
            else:
                mark_success(rec["url"])
                results.append({"url": rec["url"], "ok": True})

    await asyncio.gather(*[worker() for _ in range(min(2, len(targets) or 1))])
    return {"total": len(targets), "ok": sum(1 for r in results if r["ok"]),
            "results": results}


def start_scheduler(refresh_func) -> None:
    """服务启动时注册一次；后台标签页/离线语义由 fnOS 服务自身进程约束。"""
    global _REFRESH_TASK
    if _REFRESH_TASK and not _REFRESH_TASK.done():
        return

    async def loop():
        # 首启延迟 30 秒，避免与库预热/搜索争抢；周期检查每 15 分钟。
        await asyncio.sleep(30)
        while True:
            try:
                await refresh_due(refresh_func)
            except Exception:
                pass
            await asyncio.sleep(900)

    _REFRESH_TASK = asyncio.create_task(loop(), name="subscription-auto-refresh")
    return _REFRESH_TASK
