#!/usr/bin/env python3
"""fnmedia-ext 运行配置（可热加载）。state/config.json。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import features
from providers import DEFAULT_SOURCES

_STATE = Path(__file__).resolve().parent.parent / "state" / "config.json"
_LOCK = threading.RLock()

# 噪音关键词：标题或备注命中则不注入（电影解说、预告、花絮、AI 漫剧等）
JUNK_KEYWORDS = [
    "电影解说", "影视解说", "解说", "预告片", "预告", "花絮", "抢先", "枪版",
    "AI漫剧", "漫剧", "动态漫画", "彩蛋", "删减", "片场", "开机", "特辑",
]

DEFAULTS = {
    "sources": [dict(s) for s in DEFAULT_SOURCES],
    # 被删除的内置源 id（自定义源删除时直接从 sources 移除）
    "removed_ids": [],
    "max_per_keyword": 12,
    "filter_junk": True,
    "junk_keywords": JUNK_KEYWORDS,
    # 在线资源前排展示（在线片库更齐全，默认前排）
    "online_first": True,
    # 播放线路健康探测与自动换线：当前线路解析失败/文件不存在时，自动探测同集其他
    # 线路并切到首个可用线路；坏线短时记忆，起播自动跳过（关闭则保持纯手动选线）
    "line_autofail": True,
    # 完全自动选最快线：起播时并行竞速探测前若干条候选线路，选首字节延迟最低的线；
    # 最快线短时记忆，同一集后续/重放稳定用它（手动在播放器切线仍被尊重）
    "line_fastest": True,
    # 自动选最快线时最多参与竞速的候选线路数（控制起播开销）
    "line_fastest_max": 6,
    # 竞速超时预算（秒）：单条候选线探活上限。固化 0.2.42 最优基准=4.0s，
    # gather 等全部候选、各自 4s 上限，轻量验活正常几百毫秒返回即胜出。
    "line_fastest_timeout": 4.0,
    # 默认线（高质量源）领先优势（秒）：它在该优势内返回就算赢，避免无谓抖动
    "line_fastest_prefer": 0.25,
    # —— 本片线路预测（linepredict）：浏览海报时对首屏影片的少数候选线做轻量探活，
    # 预测出本片最快线并记忆，点播放零竞速秒播；取代旧的全局源测速 speedbench ——
    # 是否在海报/搜索列表阶段对可见影片做轻量预热
    "linepredict_enabled": True,
    # 浏览海量/独立库首屏时是否后台按片名全网补线。默认关闭：首屏一次对多达 18 部片
    # 排队跨源搜索（信号量仅 2、单任务最长 12s），会持续抢占事件循环与连接池，把同时
    # 进入的二级线路墙 item/list 拖到数秒。关闭后只在用户真正起播时补当前这一部，
    # 线路菜单完整性不变（起播路径仍会触发 start_fill_lines），浏览/二级菜单更快。
    "browse_fill_lines_enabled": False,
    # 每张海报最多探测的候选线数（只探最可能起播的前几条，避免压源站）
    "linepredict_max_lines": 3,
    # 预热结果记忆时长（分钟）
    "linepredict_ttl_min": 30,
    # 启动预热方式：realtime=重启后后台逐部搜源绑定（方案A，默认）；
    # persist=热门片绑定结果落盘，重启直接恢复线路、少重新搜源（方案B）
    "prewarm_mode": "realtime",
    # 网盘转存缓存自动清理：开启后按“最后一次播放时间”清理插件转存目录中超时的
    # 视频，防止网盘容量被不断转存占满。默认保留 6 小时；后台每小时检查一次。
    "pan_cache_cleanup_enabled": True,
    "pan_cache_ttl_hours": 6,
    # 虚拟“热门榜单”四库媒体库（豆瓣真实周榜/标签榜）
    "hot_library_enabled": True,
    # 虚拟“海量分类”四库媒体库（苹果 CMS 分类深翻）。与热门榜单相互独立开关。
    "catalog_library_enabled": True,
    # T4 远程订阅源的「T4 选片」聚合库（云端插件按需浏览/播放，非 CMS 深翻）
    "t4_library_enabled": True,
    # 是否把每个启用的 TVBox 加固源（csp_*Guard，如比特/糯米/花花）生成为一张
    # 独立“专区媒体库”卡片。默认关闭：加固源只作为播放时可切换的线路/搜索源，
    # 不在媒体库列表里产生空库。需要专区墙时可在管理页打开。
    "guard_zone_enabled": False,
    # 是否在飞牛原生媒体库注入单张「🛡加固影视」聚合库（源→分类片单，后台预取
    # 缓存、请求即时返回）。与 guard_zone_enabled（每源一张专区墙）相互独立。
    "guard_native_enabled": True,
    # 加固源并入整个在线播放体系：任意在线影片的普通(CMS)线路全部不可播时，
    # 用影片片名在可搜索加固源里元搜索聚合（复用 msearch 扇出竞速），把加固源
    # 作为“兜底播放线路”救活。仅在普通线路全失败时才打 redroid，不拖慢正常播放。
    # 仅完整版 GUARD 特性、且加固运行时就绪时生效。
    "guard_playback_fallback": True,
    # 置顶的虚拟媒体库 guid 集合（打开=在飞牛影视媒体库列表置顶，关闭=普通顺序）。
    "pinned_libs": [],
    # —— 虚拟媒体库显示开关 / 排序 / 子账号授权（稳定“库键”，见 vlibs.py）——
    # 管理员手动隐藏、不在飞牛影视显示的库键集合
    "lib_hidden": [],
    # 虚拟库自定义相对顺序（库键数组；未列出的库按默认顺序追加）
    "lib_order": [],
    # 虚拟库整体相对本地库的位置：front=本地库之前（默认）/ back=本地库之后 /
    # after=插到 anchor 指定的某个本地库之后
    "lib_placement": "front",
    # placement=after 时锚定的本地媒体库 guid
    "lib_anchor": "",
    # 子账号库授权：user_guid -> 允许观看的库键列表。键不存在=该账号未单独配置
    # （按 acl_default 处理）；空列表=一个虚拟库都看不到。
    "lib_acl": {},
    # 未单独配置的账号默认是否可见虚拟库：True=全部可见（默认，不影响老用户）
    "acl_default_visible": True,
    # 未单独配置的账号是否可用“在线搜索/在线片源”：True=可用（默认）
    "acl_default_online": True,
    "hot_library_title": "🌐在线热门",
    "hot_limit": 300,          # 四库合计保留条数上限
    "hot_per_source": 120,    # 每个资源站最多抓取的可播条目
    "hot_pages_per_source": 6,  # 每个资源站翻“最近更新”的页数（每页约20条）
    "hot_refresh_interval": 6 * 3600,
    # —— 海报预热（按需，不做全量后台预热）——
    # 仅在用户实际浏览海报/搜索列表时由 schedule_prewarm_title 触发轻量探活，
    # 不做无界后台拉取，避免抢占事件循环与连接池拖慢海报墙和媒体库。
    # —— 在线资源会话缓存 state/titles.json（issue #42：搜索越用越慢）——
    # 该文件每次合并搜索结果都会落盘；机械盘/ARM 盒子写几十 MB 要数秒，直接拖慢搜索。
    # 故同时给出「条数上限」和「字节上限」双重裁剪，并把落盘改为后台防抖异步写。
    "titles_cache_max_entries": 4000,   # 最多保留多少条标题（超出丢最旧）
    "titles_cache_max_mb": 4,          # 落盘文件 MB 硬上限（超出按最旧裁剪；0=不按字节限）
    "titles_flush_debounce": 5,        # 落盘防抖秒数：脏后最多等这么久合并写一次（0=立即）
    # 源质量优先级：source_id -> 权重（越高越优先）。未列出的源取内省默认（见
    # providers.source_priority）。热门榜单点击搜源时按 (优先级, 直链数) 排序。
    "source_priority": {},
    # 热门资源解析是否优先高质量源（关闭则只按可播直链数）
    "prefer_quality_source": True,
    # 自动优选（issue #4）：在源质量权重基础上，叠加实测健康度（死亡/高失败率/
    # 慢的源排后），让能连通、延迟低的源默认排到更前。默认开启；关掉则只看质量权重。
    "auto_best_source": True,
    "admin_token": "",
    # 仅用于「拉取订阅资源」的可选 HTTP(S) 代理（订阅配置 / spider jar / DRPY 脚本）。
    # 播放、聚合搜索、CMS 探测等一律不走此代理（产品客户端保持 trust_env=False 直连）。
    # 形如 http://user:pass@host:port ；留空表示直连。
    "subscription_proxy": "",
    # 仅用于「访问资源站取源」（聚合搜索 / 分类深翻 / 测速探测 / CMS 详情）的可选
    # HTTP(S) 代理。部分运营商直连资源站超时/被墙，挂代理可显著改善搜索结果与速度
    # （issue #30）。播放、m3u8/分片等媒体流默认仍直连，不增加代理带宽压力。
    # 形如 http://user:pass@host:port ；留空=直连。
    "source_proxy": "",
    # 资源站代理模式："off"=一律直连（忽略已配置代理）；"on"=所有取源都走代理；
    # "auto"=默认直连，仅当某个资源站直连发生连接级失败（重置/超时/DNS等）时，
    # 自动对该站切换到已配置代理重试，其余站点保持直连（推荐）。
    "source_proxy_mode": "auto",
    # 检查/下载插件更新安装包时可选的 HTTP(S) 代理（仅更新用途）。
    # 留空则回退 subscription_proxy，再不行直连。仅存于本机 state，不随包分发。
    "update_proxy": "",
    # m3u8 流层去广告总开关（TVBox/OK影视 Pro rules 引擎 + 通用启发式）。
    "ad_filter_enabled": True,
    # 播放时动态检测（无订阅 rules 时，按 DISCONTINUITY 分段+时长/CDN/位置识别广告段）。
    "ad_filter_dynamic": True,
    # 分片编码参数(SPS)指纹去广告：识别"重新编码拼接、无任何结构特征"的广告。
    # 只读分片头 4~32KB，按块时长加权投票 + 多重守卫，判定不确定时绝不删。
    "ad_filter_sps": True,
    # —— 混合直连（播放端直连源站，视频分片不经过 NAS 中转）——
    # 总开关，默认关闭：关闭时完全保持 v0.2.42 全量中转行为。开启后仅对【探测确认
    # 可裸连】的普通 HLS 直放线，把 m3u8 内分片地址改写为源站绝对 URL 让播放器直连；
    # AES 加密、去广告命中、解析(share)线、网盘/加固/T4 等特殊线一律自动回退中转。
    "direct_play_enabled": False,
    # 仅当线路首个分片在不带 Referer/UA 时仍返回 2xx/206 才允许直连（探测结果按源
    # 域名+线路短期记忆，避免每次起播都探测）。
    "direct_play_probe": True,
    # 直连资格探测结果的内存记忆时长（秒）；失败域名在此时长内直接回退中转。
    "direct_play_probe_ttl": 30 * 60,
}

# 已废弃、需从旧配置里清除的键（含曾引入后又移除的海报/冠军磁盘缓存）
_DEPRECATED = {
    "public_base", "strm_root",
    # 磁盘缓存已按 v0.2.42 机制移除（海报纯透传+浏览器强缓存，播放清单仅内存）
    "poster_disk_cache_enabled", "poster_cache_dir", "poster_disk_cache_max_mb",
    "poster_mem_cache_max", "poster_cache_ttl",
    "champion_cache_max_mb", "champion_cache_ttl",
    # 用户已明确撤回的试验功能：按需/短窗竞速、可见海报测速绑定
    "on_demand_race", "short_window_race", "poster_prewarm",
}
_BUILTIN_IDS = {s["id"] for s in DEFAULT_SOURCES}


class Config:
    def __init__(self):
        self._cfg: dict = {}
        self._mtime = 0
        self.load()


    def load(self):
        with _LOCK:
            try:
                j = json.loads(_STATE.read_text())
            except Exception:
                j = {}
            cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
            cfg.update(j)
            for k in list(cfg.keys()):
                if k in _DEPRECATED:
                    cfg.pop(k, None)
            removed = set(cfg.get("removed_ids", []))

            # 源列表：以内置为底（跳过被删除的），保留用户的启用/改名/改地址，
            # 再追加用户自定义源。
            saved = j.get("sources") if isinstance(j.get("sources"), list) else []
            saved_by_id = {s.get("id"): s for s in saved if isinstance(s, dict)}
            merged = []
            for s in DEFAULT_SOURCES:
                if s["id"] in removed:
                    continue
                cur = saved_by_id.get(s["id"], {})
                m = dict(s)
                m.update({k: cur[k] for k in ("enabled", "name", "api") if k in cur})
                m["builtin"] = True
                m["standalone"] = bool(cur.get("standalone", False))
                m["loose_filter"] = bool(cur.get("loose_filter", False))
                merged.append(m)
            for sid, s in saved_by_id.items():
                if sid in _BUILTIN_IDS or sid in removed:
                    continue
                m = {k: s.get(k) for k in ("id", "name", "api", "enabled")}
                m["enabled"] = bool(s.get("enabled", True))
                m["standalone"] = bool(s.get("standalone", False))
                m["loose_filter"] = bool(s.get("loose_filter", False))
                m["builtin"] = bool(s.get("builtin", False))
                # T4 远程订阅源的透传字段
                for key in ("kind", "site_key", "ext", "searchable",
                            "filterable", "indexs", "method"):
                    if key in s:
                        m[key] = s[key]
                merged.append(m)
            cfg["sources"] = merged
            cfg["removed_ids"] = sorted(removed)
            # 旧版本（单库/小数量）保存过的热门参数自动升级到深度爬取规模；
            # 仅在检测到旧小值时迁移一次，用户之后自定义的大值不会被覆盖。
            migrated = False
            try:
                if int(cfg.get("hot_limit", 0)) <= 120 and int(DEFAULTS["hot_limit"]) > 120:
                    cfg["hot_limit"] = DEFAULTS["hot_limit"]; migrated = True
                if int(cfg.get("hot_per_source", 0)) <= 40:
                    cfg["hot_per_source"] = DEFAULTS["hot_per_source"]; migrated = True
                if not cfg.get("hot_pages_per_source"):
                    cfg["hot_pages_per_source"] = DEFAULTS["hot_pages_per_source"]; migrated = True
                # 竞速单条超时：实验期曾改为 2.5s，现固化回 0.2.42 最优基准 4.0s。
                # 仅把实验值 2.5 迁回，用户手动设过的其它值保留。
                try:
                    if abs(float(cfg.get("line_fastest_timeout", 0)) - 2.5) < 1e-9:
                        cfg["line_fastest_timeout"] = DEFAULTS["line_fastest_timeout"]; migrated = True
                except (TypeError, ValueError):
                    pass
            except (TypeError, ValueError):
                pass
            if migrated:
                j.update({k: cfg[k] for k in
                          ("hot_limit", "hot_per_source", "hot_pages_per_source")})
                try:
                    _STATE.write_text(json.dumps(j, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            if not cfg.get("admin_token"):
                import secrets
                cfg["admin_token"] = secrets.token_hex(8)
            self._cfg = cfg
            try:
                self._mtime = _STATE.stat().st_mtime
            except FileNotFoundError:
                self._mtime = 0
                # 首次安装（state/config.json 尚不存在）：把合并后的默认配置落盘一次。
                # 否则干净安装后 status/管理页等依赖“文件存在”的路径会异常（见 issue #39）。
                try:
                    self._persist(cfg)
                    self._mtime = _STATE.stat().st_mtime
                except Exception:
                    pass

    def maybe_reload(self):
        try:
            mt = _STATE.stat().st_mtime
        except FileNotFoundError:
            return
        if mt != self._mtime:
            self.load()

    def get(self):
        with _LOCK:
            return json.loads(json.dumps(self._cfg))

    def enabled_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"] if s.get("enabled")]

    def all_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"]]

    def save(self, patch: dict):
        with _LOCK:
            cfg = self.get()
            for k, v in patch.items():
                if k in cfg:
                    cfg[k] = v
            self._persist(cfg)

    def _persist(self, cfg: dict):
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        tmp.replace(_STATE)
        self._cfg = cfg
        self._mtime = _STATE.stat().st_mtime

    @staticmethod
    def make_custom_id(api: str) -> str:
        return "custom_" + hashlib.md5(api.strip().encode()).hexdigest()[:10]

    def add_source(self, name: str, api: str, enabled: bool = True,
                   standalone: bool = False) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            api = (api or "").strip()
            name = (name or "").strip()
            if not api.startswith(("http://", "https://")):
                return False, "API 地址需以 http(s):// 开头"
            if not name:
                name = api.split("/")[2]
            sid = self.make_custom_id(api)
            norm = api.rstrip("/").lower()
            if any(s["id"] == sid or s.get("api", "").rstrip("/").lower() == norm
                   for s in cfg["sources"]):
                return False, "该资源站地址已存在"
            cfg["sources"].append(
                {"id": sid, "name": name, "api": api,
                 "enabled": bool(enabled), "standalone": bool(standalone),
                 "builtin": False})
            self._persist(cfg)
            return True, sid

    def source_priorities(self) -> dict:
        with _LOCK:
            return dict(self.get().get("source_priority") or {})

    def import_t4_sources(self, sources: list[dict]) -> dict:
        import t4
        with _LOCK:
            cfg = self.get()
            existing = {s["id"] for s in cfg["sources"]}
            added, duplicate = [], 0
            for source in sources:
                sid = t4.source_id(source["api"], source.get("ext"))
                if sid in existing:
                    duplicate += 1
                    continue
                cfg["sources"].append(dict(
                    source, id=sid, kind="t4", builtin=False, standalone=False))
                existing.add(sid)
                added.append(source["name"])
            if added:
                self._persist(cfg)
            return {"candidates": len(sources), "added": len(added),
                    "added_names": added, "duplicate": duplicate}

    def set_source_priority(self, mapping: dict) -> tuple[bool, str]:
        """整体保存源优先级（id -> 整数权重），忽略未知源与非法值。"""
        with _LOCK:
            cfg = self.get()
            valid = {s["id"] for s in cfg["sources"]}
            out = {}
            for k, v in (mapping or {}).items():
                if k in valid:
                    try:
                        out[k] = max(-100, min(100, int(v)))
                    except (TypeError, ValueError):
                        continue
            cfg["source_priority"] = out
            self._persist(cfg)
            return True, "ok"

    def set_subscription_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「订阅资源抓取」专用代理（空串=直连）。仅用于订阅配置/jar/DRPY脚本，
        不影响播放与搜索。仅接受 http(s)/socks5 URL 形态。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["subscription_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「资源站取源」专用代理（空串=直连），issue #30。
        仅用于搜索/分类/探测/详情；不影响订阅抓取与媒体播放。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy_mode(self, mode: str) -> tuple[bool, str]:
        """设置资源站代理模式：off/on/auto。"""
        m = str(mode or "auto").strip().lower()
        if m not in ("off", "on", "auto"):
            return False, "模式须为 off/on/auto"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy_mode"] = m
            self._persist(cfg)
            return True, "ok"

    def source_proxy_mode(self) -> str:
        try:
            m = str(self.get().get("source_proxy_mode") or "auto").strip().lower()
            return m if m in ("off", "on", "auto") else "auto"
        except Exception:
            return "auto"

    def update_source(self, sid: str, name: str | None = None,
                      api: str | None = None) -> tuple[bool, str]:
        """编辑资源站的显示名称 / 订阅(API)链接。

        - 改名：任意源均可；空名回退为域名。
        - 改链接：仅自定义源（builtin=False）允许，内置源改链会破坏升级一致性，
          如需自定义请删除后按新地址添加。新地址须 http(s) 且不能与其它源重复。
        改 api 后该自定义源 id（由旧地址哈希生成）会一并重建为新地址的 id。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if name is not None:
                nm = str(name).strip()[:40]
                if not nm:
                    api0 = str(target.get("api", ""))
                    nm = api0.split("/")[2] if "://" in api0 else str(target.get("name", "资源站"))
                target["name"] = nm
            if api is not None:
                newapi = str(api).strip()
                if not newapi.startswith(("http://", "https://")):
                    return False, "API 地址需以 http(s):// 开头"
                if target.get("builtin"):
                    return False, "内置源不支持改链接，请删除后以自定义源重新添加"
                norm = newapi.rstrip("/").lower()
                if any(s["id"] != sid and
                       s.get("api", "").rstrip("/").lower() == norm
                       for s in cfg["sources"]):
                    return False, "该资源站地址已存在"
                new_id = self.make_custom_id(newapi)
                if any(s["id"] == new_id for s in cfg["sources"] if s["id"] != sid):
                    return False, "该资源站地址已存在"
                target["api"] = newapi
                target["id"] = new_id
                # 同步迁移该源的独立库/优先级等按 id 记录的配置
                pr = cfg.get("source_priority") or {}
                if sid in pr:
                    pr[new_id] = pr.pop(sid)
                sid = new_id
            self._persist(cfg)
            return True, sid

    def set_standalone(self, sid: str, on: bool) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("kind") == "t4" and on:
                return False, "T4 来源统一显示在「T4 选片」，无需逐插件建立独立库"
            target["standalone"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    def pinned_lib_guids(self) -> set:
        with _LOCK:
            try:
                return set(self.get().get("pinned_libs") or [])
            except TypeError:
                return set()

    def set_lib_pinned(self, guid: str, on: bool) -> tuple[bool, str]:
        """置顶/取消置顶某个虚拟媒体库（guid）。"""
        if not isinstance(guid, str) or not guid:
            return False, "guid 非法"
        with _LOCK:
            cfg = self.get()
            cur = [g for g in (cfg.get("pinned_libs") or []) if isinstance(g, str)]
            if on:
                if guid not in cur:
                    cur.append(guid)
            else:
                cur = [g for g in cur if g != guid]
            cfg["pinned_libs"] = cur
            self._persist(cfg)
            return True, "ok"

    def set_loose_filter(self, sid: str, on: bool) -> tuple[bool, str]:
        """放宽正片（标题/备注噪音）过滤；分类层面的红线不受影响。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["loose_filter"] = bool(on)
            self._persist(cfg)
            return True, "ok"


    def remove_source(self, sid: str) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("builtin"):
                # 内置源不物理删除，标记移除（可在重置时恢复）
                if sid not in cfg["removed_ids"]:
                    cfg["removed_ids"].append(sid)
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] != sid]
            self._persist(cfg)
            return True, "ok"

    def restore_builtin(self) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            cfg["removed_ids"] = []
            self._persist(cfg)
            self.load()
            return True, "ok"

    @staticmethod
    def normalize_keywords(raw) -> list[str]:
        """把逗号/换行/空白分隔的输入清洗成去重关键词列表。"""
        if isinstance(raw, str):
            parts = re.split(r"[,，、\n\r;；\s]+", raw)
        elif isinstance(raw, (list, tuple)):
            parts = []
            for item in raw:
                parts.extend(re.split(r"[,，、\n\r;；\s]+", str(item)))
        else:
            return []
        out, seen = [], set()
        for p in parts:
            k = p.strip()
            if k and k not in seen and len(k) <= 30:
                seen.add(k)
                out.append(k)
        return out

    def set_junk_keywords(self, raw) -> list[str]:
        kws = self.normalize_keywords(raw)
        self.save({"junk_keywords": kws})
        return kws

    def reset_junk_keywords(self) -> list[str]:
        self.save({"junk_keywords": list(JUNK_KEYWORDS)})
        return list(JUNK_KEYWORDS)

    def is_junk(self, title: str, remarks: str = "") -> bool:
        if not self._cfg.get("filter_junk", True):
            return False
        kws = self._cfg.get("junk_keywords", JUNK_KEYWORDS)
        hay = f"{title} {remarks}"
        return any(k and k in hay for k in kws)


config = Config()
