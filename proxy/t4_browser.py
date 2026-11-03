"""On-demand native T4 directories; no crawler or player execution here."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path

import inject
import t4

LIB_GUID = hashlib.md5(b"fnmedia-t4-browse").hexdigest()
LIB_TITLE = "T4 选片"
_STATE = Path(__file__).resolve().parent.parent / "state" / "t4_browse.json"
_log = logging.getLogger("fnmedia.t4.browse")


def _number(value, default=0):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return default


class T4Browser:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self._nodes = OrderedDict()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._search_sessions = OrderedDict()
        self._locks = {}
        self._slots = asyncio.Semaphore(4)
        self._dirty = False
        try:
            nodes = json.loads(_STATE.read_text(encoding="utf-8"))
            self._nodes.update((key, value) for key, value in nodes.items()
                               if isinstance(value, dict))
        except (OSError, ValueError, AttributeError):
            pass
        while len(self._nodes) > 2048:
            self._nodes.popitem(last=False)

    def sources(self):
        if not self.config.get().get("t4_library_enabled", True):
            return []
        return [s for s in self.config.enabled_sources() if s.get("kind") == "t4"]

    def source(self, sid):
        source = next((s for s in self.config.enabled_sources()
                       if s.get("kind") == "t4" and s["id"] == sid), None)
        if source is None:
            raise t4.T4Error("T4 来源已移除或停用")
        return source

    def node(self, guid):
        return self._nodes.get(guid)

    def _put(self, source, kind, name, parent=LIB_GUID, **values):
        identity = [source["id"], kind, values.get("tid", ""),
                    values.get("filters", {}), values.get("key", "")]
        guid = hashlib.md5(("t4-directory:" + json.dumps(
            identity, sort_keys=True, ensure_ascii=True)).encode()).hexdigest()
        if parent == guid:
            parent = self._nodes.get(guid, {}).get("parent", LIB_GUID)
        node = dict(source_id=source["id"], kind=kind, title=name,
                    parent=parent, **values)
        if self._nodes.get(guid) != node:
            self._nodes[guid] = node
            self._dirty = True
        self._nodes.move_to_end(guid)
        while len(self._nodes) > 2048:
            self._nodes.popitem(last=False)
        return guid

    def _save(self):
        if not self._dirty:
            return
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = _STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(self._nodes, ensure_ascii=False), encoding="utf-8")
        temp.replace(_STATE)
        self._dirty = False

    def _trail(self, guid):
        trail, seen = [], set()
        while guid != LIB_GUID and guid in self._nodes and guid not in seen:
            seen.add(guid)
            node = self._nodes[guid]
            trail.append({"fv_guid": guid, "base_name": node["title"]})
            guid = node["parent"]
        return [{"fv_guid": "", "base_name": LIB_TITLE}] + list(reversed(trail))

    def card(self, guid):
        node = self._nodes[guid]
        return inject._base_item(
            guid=guid, type="Directory", title=node["title"], can_play=0,
            path="/".join(row["base_name"] for row in self._trail(guid)),
            file_name=node["title"], runtime=0, video_guid="",
            parent_guid="" if node["parent"] == LIB_GUID else node["parent"],
            ancestor_guid=LIB_GUID, ancestor_name=LIB_TITLE, ancestor_category="Mix",
            play_item_guid="", play_error="", logic_type=0,
            posters="", backdrops="")

    def library(self, *, detail=False):
        row = {"guid": LIB_GUID, "title": LIB_TITLE, "category": "Mix",
               "poster": "", "posters": [], "view_type": 0,
               "poster_type": 0, "refresh_disabled": True}
        if detail:
            row.update(name=LIB_TITLE, dir_list=[], lan="zh-CN",
                       auto_scrap_subtitle=0, include_adult=False, subtitle_lan="zh-CN",
                       last_update_time=0, sort_num=0, auto_progress_thumb=0,
                       skip_filesize=0, prefer_local_nfo=0)
        return row

    def media_card(self, brief, parent=LIB_GUID):
        if brief.get("locator", {}).get("search_keyword"):
            source = self.source(brief["locator"]["source_id"])
            guid = self._put(source, "search", brief["title"], parent,
                             key=brief["guid"],
                             keyword=brief["locator"]["search_keyword"],
                             year=brief["locator"].get("search_year", ""))
            card = self.card(guid)
            card["poster"] = inject._poster(brief.get("poster") or "")
            self._save()
            return card
        old = self.store.get_title_by_guid(brief["guid"])
        display = dict(brief, lines=old["lines"], type_name=inject.media_type_name(
            brief.get("type_name", ""), old.get("type_name", ""))) \
            if old and old.get("lines") else brief
        if (brief.get("locator", {}).get("kind") == "t4" and not display.get("lines")
                and not any(word in (brief.get("type_name") or "")
                            for word in inject._MOVIE_TYPE_WORDS + inject._TV_TYPE_WORDS)):
            source = self.source(brief["locator"]["source_id"])
            guid = self._put(source, "title", brief["title"], parent,
                             key=brief["guid"], target_guid=brief["guid"])
            card = self.card(guid)
            card["poster"] = inject._poster(brief.get("poster") or "")
            self._save()
            return card
        return inject.search_item(display)

    async def _raw(self, client, source, tid=None, page=1, filters=None, *,
                   keyword=None, year="", navigation=None):
        key = hashlib.sha256(json.dumps(
            [source, tid, page, filters, keyword, year],
            sort_keys=True, ensure_ascii=True).encode()).hexdigest()

        def search_session():
            stored = self._search_sessions.get(key)
            if stored and time.monotonic() - stored[1] < 90:
                return stored[0]
            self._search_sessions.pop(key, None)
            return None

        def cache_key():
            session = (navigation or {}).get("nav_session") or search_session()
            if navigation is not None and session:
                return (key, session, navigation.get("nav_offset", 0),
                        navigation.get("nav_limit", 22))
            return key

        def cached():
            now = time.monotonic()
            # Also reclaim idle entries on the next browsing request.
            for old in list(self._cache):
                if now - self._cache[old][0] >= 60:
                    self._cache_bytes -= self._cache.pop(old)[2]
            # Legacy T4 servers ignore incremental options and return one snapshot.
            hit = self._cache.get(key) or self._cache.get(cache_key())
            if hit:
                self._cache.move_to_end(key if key in self._cache else cache_key())
                return hit[1]
            return None

        hit = cached()
        if hit is not None:
            return hit
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                hit = cached()
                if hit is not None:
                    return hit
                start = time.monotonic()
                async with self._slots:
                    if keyword is not None:
                        options = dict(navigation or {})
                        if session := search_session():
                            options.setdefault("nav_session", session)
                        result = await t4.navigation_search(
                            client, source, keyword, year, options) if options else \
                            await t4.navigation_search(client, source, keyword, year)
                    else:
                        result = await t4.home(client, source) if tid is None else \
                            await t4.category(client, source, tid, page, filters)
                self.source(source["id"])
                size = len(json.dumps(result, ensure_ascii=True).encode())
                status = result.get("t4_search")
                status = status if isinstance(status, dict) else {}
                # Paging must reuse one snapshot even when some plugins are slow.
                # Empty incomplete results remain immediately retryable.
                cacheable = bool(result.get("list")) or not (
                    status.get("partial") or status.get("failed"))
                if navigation is not None and status.get("incremental"):
                    self._search_sessions[key] = status["session"], time.monotonic()
                    self._search_sessions.move_to_end(key)
                    while len(self._search_sessions) > 32:
                        self._search_sessions.popitem(last=False)
                    cacheable = not status.get("pending") or len(result["list"]) >= \
                        navigation.get("nav_limit", 22)
                if size <= 1024 * 1024 and cacheable:
                    stored_key = cache_key() if status.get("incremental") else key
                    previous = self._cache.pop(stored_key, None)
                    if previous:
                        self._cache_bytes -= previous[2]
                    self._cache[stored_key] = time.monotonic(), result, size
                    self._cache_bytes += size
                    while len(self._cache) > 32 or self._cache_bytes > 4 * 1024 * 1024:
                        self._cache_bytes -= self._cache.popitem(last=False)[1][2]
                _log.info("source=%s operation=%s page=%d rows=%d elapsed_ms=%d",
                          source["id"], "search" if keyword is not None else
                          "home" if tid is None else "category",
                          page, len(result.get("list") or []),
                          int((time.monotonic() - start) * 1000))
                return result
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)

    def _category_type_name(self, parent):
        seen = set()
        while parent in self._nodes and parent not in seen:
            seen.add(parent)
            node = self._nodes[parent]
            if node["kind"] == "search":
                return ""
            if node["kind"] in ("category", "filter"):
                name = str(node.get("category_type_name") or
                           node.get("category_title") or node["title"])
                if any(word in name for word in
                       inject._MOVIE_TYPE_WORDS + inject._TV_TYPE_WORDS):
                    return name
            parent = node["parent"]
        return ""

    def _rows(self, rows, source, parent):
        cards, briefs = [], []
        type_hint = self._category_type_name(parent)
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = str(row.get("vod_id") if row.get("vod_id") is not None else "")
            if raw_id.startswith(("action:", "push://")):
                continue
            if str(row.get("vod_tag", "")).lower() == "folder":
                if raw_id and row.get("vod_name"):
                    guid = self._put(source, "category", str(row["vod_name"]),
                                     parent, tid=raw_id, filters={}, schema=[],
                                     category_type_name=type_hint)
                    card = self.card(guid)
                    card["poster"] = inject._poster(str(row.get("vod_pic") or ""))
                    cards.append(card)
                continue
            title = t4.map_title(row, source)
            if title is not None:
                brief = inject.brief_of(title)
                old = self.store.get_title_by_guid(brief["guid"])
                brief["type_name"] = inject.media_type_name(
                    brief["type_name"],
                    inject.media_type_name((old or {}).get("type_name", ""), type_hint))
                if old is None or any(old.get(key) != brief.get(key) for key in (
                    "title", "type_name", "poster", "year", "remarks", "overview"
                )) or any((old.get("locator") or {}).get(key) != brief["locator"].get(key)
                          for key in ("source_id", "vod_id", "content_key",
                                      "search_keyword", "search_year", "source_label")):
                    briefs.append(brief)
                # 列表不抹掉已加载详情的类型和集数，也不提前请求详情/取链。
                card = self.media_card(brief, parent)
                card.update(title=title.title, parent_guid=parent,
                            ancestor_guid=LIB_GUID, ancestor_name=LIB_TITLE,
                            ancestor_category="Mix")
                if label := brief.get("locator", {}).get("source_label"):
                    card["title"] = title.title + " · " + label
                cards.append(card)
        if briefs:
            self.store.remember_merged(briefs)
        return cards

    async def _window(self, client, source, node, offset, size):
        first = await self._raw(client, source, node["tid"], filters=node["filters"])
        rows = first["list"]
        limit = _number(first.get("limit")) or len(rows) or 20
        # Some spiders return a placeholder limit=999999 instead of their page size.
        if limit > 500:
            limit = len(rows) or 20
        declared = _number(first.get("total"))
        pages = _number(first.get("pagecount"))
        total = declared or (pages * limit if pages else len(rows))
        known = bool(declared or pages) or len(rows) < limit
        end = min(offset + size, total) if known else offset + size
        if end <= offset:
            return [], total
        pg_start, pg_end = offset // limit + 1, (end - 1) // limit + 1
        numbers = list(range(pg_start, pg_end + 1))
        tasks = [asyncio.create_task(
            self._raw(client, source, node["tid"], pg, node["filters"])) for pg in numbers]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        out = []
        for pg, result in zip(numbers, results):
            start = (pg - 1) * limit
            batch = result["list"]
            out.extend(batch[max(0, offset - start):min(len(batch), end - start)])
            if len(batch) < limit and (not known or start + len(batch) < total):
                total, known = start + len(batch), True
        if not known:
            total = offset + len(out) + (size if out else 0)
        return out, total

    async def listing(self, client, guid, body):
        size = max(1, min(_number(body.get("page_size"), 22) or 22, 100))
        page = max(1, _number(body.get("page") or body.get("page_index"), 1))
        offset = _number(body.get("offset"), (page - 1) * size)
        if guid == LIB_GUID:
            guids, names = [], {}
            for source in self.sources():
                name = source.get("name") or "T4"
                names[name] = names.get(name, 0) + 1
                label = name if names[name] == 1 else f"{name} ({names[name]})"
                guids.append(self._put(source, "home", label))
            cards, total = [self.card(g) for g in guids[offset:offset + size]], len(guids)
        else:
            node = self.node(guid)
            if node is None:
                raise t4.T4Error("T4 目录已过期，请从 T4 选片重新进入")
            source = self.source(node["source_id"])
            if node["kind"] == "home":
                result = await self._raw(client, source)
                filters = result.get("filters")
                filters = filters if isinstance(filters, dict) else {}
                guids = []
                for row in result.get("class") or []:
                    if not isinstance(row, dict) or row.get("type_id") is None:
                        continue
                    tid = str(row["type_id"])
                    schema = filters.get(tid) if t4.switch(source.get("filterable"), False) else []
                    guids.append(self._put(
                        source, "category", str(row.get("type_name") or tid),
                        guid, tid=tid, filters={},
                        schema=schema if isinstance(schema, list) else [],
                        category_type_name=str(row.get("type_name") or "")))
                prefix = [self.card(g) for g in guids]
                count = len(result.get("list") or [])
                total = len(prefix) + count
                cards = prefix[offset:offset + size]
                start = max(0, offset - len(prefix))
                cards += self._rows((result.get("list") or [])[start:start + size - len(cards)],
                                    source, guid)
            elif node["kind"] == "title":
                brief = self.store.get_title_by_guid(node["target_guid"])
                if not brief or not brief.get("lines"):
                    raise t4.T4Error("资源详情未加载，请重新打开资源目录")
                card = inject.search_item(brief)
                card.update(parent_guid=guid, ancestor_guid=LIB_GUID,
                            ancestor_name=LIB_TITLE, ancestor_category="Mix")
                cards, total = [card][offset:offset + size], 1
            elif node["kind"] == "search":
                base = _number(node.get("search_offset"))
                options = {"nav_incremental": "true", "nav_offset": base + offset,
                           "nav_limit": size}
                if node.get("search_session"):
                    options["nav_session"] = node["search_session"]
                result = await self._raw(client, source, keyword=node["keyword"],
                                         year=node.get("year", ""), navigation=options)
                rows = result["list"]
                status = result.get("t4_search") or {}
                if status.get("incremental"):
                    cards = self._rows(rows, source, guid)
                    total = max(0, _number(result.get("total")) - base)
                    if status.get("pending") and len(cards) < size:
                        resume = base + offset + len(rows)
                        more = self._put(
                            source, "search", "继续搜索剩余来源", guid,
                            key=f"next:{status['session']}:{resume}",
                            keyword=node["keyword"], year=node.get("year", ""),
                            search_offset=resume, search_session=status["session"])
                        cards.append(self.card(more))
                    elif status.get("pending"):
                        total = max(total, offset + size + 1)
                else:
                    cards = self._rows(rows[offset:offset + size], source, guid)
                    total = len(rows)
            elif node["kind"] == "filter":
                guids = []
                for value in node.get("values") or []:
                    if not isinstance(value, dict) or "v" not in value:
                        continue
                    selected = dict(node["filters"], **{node["key"]: str(value["v"])})
                    base = node["category_title"].split(" · ", 1)[0]
                    label = str(value.get("n") or value["v"] or "全部")
                    guids.append(self._put(
                        source, "category", base + " · " + label,
                        node["category_parent"], tid=node["tid"],
                        filters=selected, schema=node["schema"],
                        category_type_name=node.get("category_type_name", "")))
                cards, total = [self.card(g) for g in guids[offset:offset + size]], len(guids)
            else:
                guids = []
                for field in node.get("schema") or []:
                    if not isinstance(field, dict) or not field.get("key"):
                        continue
                    key = str(field["key"])
                    label = str(field.get("name") or key)
                    if node["filters"].get(key):
                        label += " · " + str(node["filters"][key])
                    guids.append(self._put(
                        source, "filter", label, guid, tid=node["tid"],
                        filters=node["filters"], key=key, schema=node["schema"],
                        values=field.get("value") or [], category_title=node["title"],
                        category_parent=node["parent"],
                        category_type_name=self._category_type_name(guid)))
                prefix = [self.card(g) for g in guids]
                cards = prefix[offset:offset + size]
                rows, total = await self._window(client, source, node,
                                                 max(0, offset - len(prefix)),
                                                 size - len(cards))
                cards += self._rows(rows, source, guid)
                total += len(prefix)
            self.source(node["source_id"])
        self._save()
        data = {"mdb_name": LIB_TITLE, "mdb_category": "Mix", "top_dir": "",
                "dir": "/".join(row["base_name"] for row in self._trail(guid)),
                "jump_list": self._trail(guid), "total": total, "list": cards}
        if guid != LIB_GUID and node["kind"] == "search" and isinstance(
                result.get("t4_search"), dict):
            data["t4_search"] = result["t4_search"]
        return data
