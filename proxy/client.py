#!/usr/bin/env python3
"""客户端画像（client profile）：从 User-Agent 准确识别请求来自哪种飞牛端。

为什么需要它：
  飞牛影视在不同端用不同的 JSON 模型反序列化我们注入的虚拟条目，严格度差异极大：
    * TV/盒子（Flutter，UA 含 com.trim.tv，okHttp 栈）最严格——卡片字段必须是它
      真实本地库字段集合的子集，status 必须是数字枚举串、日期必须 yyyy-MM-dd，
      多余字段或形态不符会让整页 fromJson 失败（整库“加载失败/无法播放”）。
    * iOS/Android 手机端（com.trim.media.ios、Dalvik/okhttp、Alamofire、Dart）较宽松。
    * Web（trimemedia-web）最宽松。

本模块只做“识别 + 规整动作开关”，不发网络请求、无副作用，便于在每个出口统一调用。

真实 UA 样本（取自 trim 媒体 nginx 访问日志，2026-10）：
  TV   : "1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0"
  iOS  : "1.12.2 (com.trim.media.ios; build:1122011; iOS 27.2) Flutter/3.11.5"
         "Runner/1.12.2 (com.trim.media.ios; ...) Alamofire/5.11.2"
  安卓 : "Dalvik/2.1.0 (Linux; U; Android 15; V2203A ...)"  /  "okhttp/4.12.0"
  通用 : "Dart/3.11 (dart:io)"
  Web  : 资产标识 "trimemedia-web"（浏览器导航器 UA，通常不含 trim 字样）
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import date, datetime

# 端类别常量
TV = "tv"            # 电视 / 盒子（Flutter，严格模型）
ANDROID = "android"  # 安卓手机 / 平板原生
IOS = "ios"          # iPhone / iPad
WEB = "web"          # 网页版
UNKNOWN = "unknown"  # 其它 / 未能识别（按宽松处理，避免误伤）


@dataclass(frozen=True)
class Client:
    kind: str = UNKNOWN
    ua: str = ""
    is_tv: bool = False
    is_mobile: bool = False
    is_web: bool = False
    strict_card: bool = False  # 是否需要“严格卡片字段集合 + status 数字串 + 完整日期”

    @property
    def label(self) -> str:
        return {TV: "TV", ANDROID: "Android", IOS: "iOS",
                WEB: "Web", UNKNOWN: "?"}.get(self.kind, "?")


def parse_user_agent(ua: str | None) -> Client:
    """把原始 User-Agent 解析成 Client。无法识别时按最宽松（UNKNOWN）处理，
    绝不为了“严格”而误伤未知客户端。"""
    s = (ua or "").strip()
    low = s.lower()

    # 1) TV/盒子：com.trim.tv 是最可靠标识；历史代码也匹配裸 "trim.tv"。
    if "com.trim.tv" in low or "trim.tv" in low:
        return Client(TV, s, is_tv=True, strict_card=True)

    # 2) iOS：bundle 标识最可靠；再退到系统/网络栈线索。
    if "com.trim.media.ios" in low or "iphone" in low or "ipad" in low \
            or "alamofire" in low:
        return Client(IOS, s, is_mobile=True)

    # 3) Web：飞牛 web 资产会带自定义标识；普通浏览器（含 Mozilla + 不是上面各端）
    #    在媒体播放链路上极少直接打到插件（web 走浏览器），单列以便观测。
    if "trimemedia-web" in low:
        return Client(WEB, s, is_web=True)

    # 4) Android 手机/平板：Dalvik/ART 运行时 UA；裸 okhttp 多为安卓原生网络栈。
    #    注意：TV 的 UA 也含 okHttp，但已在第 1 步优先拦截，不会落到这里。
    if "dalvik" in low or "android" in low or low.startswith("okhttp"):
        return Client(ANDROID, s, is_mobile=True)

    # 5) Dart/Flutter 通用网络栈（未带 bundle 标识）：手机端常见，按宽松手机处理。
    if "dart/" in low or "flutter/" in low:
        return Client(ANDROID, s, is_mobile=True)

    # 6) 明显是浏览器（Mozilla）但无 trim 标识：按 Web/宽松处理。
    if "mozilla" in low:
        return Client(WEB, s, is_web=True)

    return Client(UNKNOWN, s)


# --------------------------------------------------------------- 出口规整

# TV(Flutter) 列表模型真实接受的本地库卡片字段集合（白名单）。
# 多余字段（如 can_play/genres/poster_width/poster_height 等）或取值形态不符，
# 都可能导致整页 fromJson 失败。任何来源的列表卡片（榜单/海量/独立库/加固专区/搜索）
# 在 TV 端都统一过这一层，杜绝“某个出口漏规整 → TV 无法播放”。
TV_CARD_KEEP = (
    "guid", "video_guid", "lan", "imdb_id", "trim_id", "tv_title",
    "parent_guid", "parent_title", "title", "type", "poster",
    "is_favorite", "watched", "watched_ts", "vote_average", "media_stream",
    "release_date", "season_number", "episode_number", "air_date",
    "number_of_seasons", "number_of_episodes",
    "local_number_of_seasons", "local_number_of_episodes", "status",
    "overview", "ancestor_guid", "ancestor_name", "ancestor_category",
    "ts", "duration", "single_child_guid", "file_name", "path", "runtime",
    # TV 选集/Episode 节点额外需要的身份字段
    "media_guid", "audio_guid", "subtitle_guid", "is_watched", "logos",
)


def _strict_date(v) -> str:
    """规整成 TV 严格模型能解析的 yyyy-MM-dd；无法识别一律返回空串。

    资源站常见脏值 '0'/''/'null'/纯年份/'yyyy-MM'，此前只补 4 位年份，'0' 原样
    下发会让 TV(Flutter) DateTime 解析抛异常，导致整页卡片空白。
    """
    s = str(v if v is not None else "").strip()
    if not s or s.lower() in ("0", "none", "null", "-", "nan"):
        return ""
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", s)
    try:
        if m:
            return date(int(m[1]), int(m[2] or 1), int(m[3] or 1)).isoformat()
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return ""
    return ""


def normalize_card(row: dict, client: Client) -> dict:
    """按端规整“媒体库/搜索列表卡片”。

    仅对 TV 严格模型做白名单裁剪 + 枚举/日期规整；其它端原样返回（它们更宽松，
    额外字段无害，裁剪反而会丢信息）。对单条与列表都安全（不修改入参）。
    """
    if not isinstance(row, dict):
        return row
    if not client.strict_card:
        return row
    out = {k: row[k] for k in TV_CARD_KEEP if k in row}
    out["status"] = "1"
    # 仅在原卡确实带日期键时才输出（补成完整日期）；缺键给空串而非字符串 "None"，
    # 否则比缺键更易触发严格模型解析失败。
    if "release_date" in row:
        out["release_date"] = _strict_date(row.get("release_date"))
    else:
        out["release_date"] = ""
    if "air_date" in row:
        out["air_date"] = _strict_date(row.get("air_date"))
    else:
        out["air_date"] = ""
    # 列表模型必需的身份/占位键缺失时补齐，不依赖每个构造器自觉。
    out.setdefault("path", "")
    out.setdefault("runtime", 0)
    if not out.get("video_guid") and out.get("guid"):
        from store import video_guid as _vg
        out["video_guid"] = _vg(out["guid"])
    return out


def normalize_cards(rows, client: Client):
    """批量规整列表卡片；非严格端零拷贝原样返回。"""
    if not rows or not client.strict_card:
        return rows
    return [normalize_card(r, client) for r in rows]


# 详情/播放 info 里需要补成完整日期的日期键（与列表模型同一种严格日期解析）。
_DATE_KEYS = ("release_date", "air_date")


def normalize_detail(obj, client: Client):
    """按端规整“详情/播放 info 单对象”。

    与列表卡不同：详情模型依赖 can_play/play_item_guid/play_error/posters 等大量字段
    才能进入播放页，故这里【绝不裁剪字段】，只把 TV 严格端会解析失败的形态归一：
      * 年份/日期/空值 -> 合法 yyyy-MM-dd 或空串，状态与列表枚举一致。
      * 递归处理内嵌的 item / Episode / Season 对象与列表。
    手机/iOS/Web 原样返回。返回新对象，不修改入参。
    """
    if not client.strict_card or not isinstance(obj, dict):
        return obj

    def _norm(o):
        if isinstance(o, dict):
            x = {}
            for k, v in o.items():
                if k in _DATE_KEYS:
                    x[k] = _strict_date(v)
                elif k == "status":
                    x[k] = "1"
                else:
                    x[k] = _norm(v)
            return x
        if isinstance(o, list):
            return [_norm(v) for v in o]
        return o

    return _norm(obj)


