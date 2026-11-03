# -*- coding: utf-8 -*-
"""虚拟媒体库统一管理：显示开关、排序、子账号可见授权。

设计要点
--------
- 每张虚拟库有一个与 guid 解耦的**稳定库键 key**（如 ``chart:movie``、
  ``standalone:baofeng``、``guard:abc123``、``xlib:movie``），配置里按 key 存
  显隐 / 排序 / 授权，库 guid 算法调整也不影响用户设置。
- guid 派生与 app.py 中既有定义保持一致，保证老链接/收藏不失效。
- 账号识别（谁在看）在 ``fnusers.py``；本模块只回答“这个账号能不能看到这张库”。
"""
from __future__ import annotations

import hashlib

from config import config
import features

# —— 与 app.py 完全一致的固定 guid ——（勿改，改了旧入口会失效）
CHART_LIB_GUIDS = {
    "movie":   "0b10d767319eb8cab9d99373ede48d0a",
    "tv":      "9e75c8f2616bd01851911eea649db367",
    "anime":   "18372cada44b72c305343c2bfcc026f0",
    "variety": "cb606d5f699560c630c97f7acc84f75b",
}
CATALOG_LIB_GUIDS = {
    "movie":   "e91dd7347a49dbd874fb8a0e78ad7ad7",
    "tv":      "196381f891d66a4de32f9032e8f244f3",
    "anime":   "97fc49015edcb20cc423658401711b6c",
    "variety": "cfff30730cffca2d1357e0c2b52fc2f5",
}
CHART_CAT_LABEL = {"movie": "电影榜", "tv": "剧集榜", "anime": "动漫榜", "variety": "综艺榜"}
CATALOG_CAT_LABEL = {"movie": "海量电影", "tv": "海量剧集", "anime": "海量动漫", "variety": "海量综艺"}

_STANDALONE_PREFIX = "fnmedia-standalone:"
_GUARD_LIB_PREFIX = "fnmedia-guard:"
_GUARD_NATIVE_GUID = hashlib.md5(b"fnmedia-guardnative:root").hexdigest()
_XLIB_LIB_PREFIX = "fnmedia-xlib:"
_T4_GUID = hashlib.md5(b"fnmedia-t4-browse").hexdigest()

# 库类型（固定/组开关）与 key 前缀
K_CHART = "chart"        # 豆瓣真实榜单四库
K_CATALOG = "catalog"    # 海量分类四库
K_GUARD_NATIVE = "guard-native"
K_STANDALONE = "standalone"
K_GUARD = "guard"
K_XLIB = "xlib"
K_T4 = "t4"


def standalone_guid(source_id: str) -> str:
    return hashlib.md5((_STANDALONE_PREFIX + source_id).encode()).hexdigest()


def guard_guid(gkey: str) -> str:
    return hashlib.md5((_GUARD_LIB_PREFIX + gkey).encode()).hexdigest()


def xlib_guid(cat_id: str) -> str:
    return hashlib.md5((_XLIB_LIB_PREFIX + cat_id).encode()).hexdigest()


def make_key(kind: str, ident: str = "") -> str:
    return f"{kind}:{ident}" if ident else kind


def key_of_guid(guid: str) -> str | None:
    """guid 反查稳定库键；非虚拟库返回 None。动态库需要现场遍历（见 catalog 函数）。"""
    if not guid:
        return None
    if guid == _T4_GUID:
        return K_T4
    for cat, g in CHART_LIB_GUIDS.items():
        if g == guid:
            return make_key(K_CHART, cat)
    for cat, g in CATALOG_LIB_GUIDS.items():
        if g == guid:
            return make_key(K_CATALOG, cat)
    if guid == _GUARD_NATIVE_GUID:
        return K_GUARD_NATIVE
    return None


# --------------------------------------------------------------- 显示开关

def hidden_libs() -> set[str]:
    """管理员手动隐藏的库键集合。"""
    try:
        return set(config.get().get("lib_hidden", []) or [])
    except Exception:
        return set()


def is_hidden(key: str) -> bool:
    return key in hidden_libs()


def set_hidden(key: str, hidden: bool) -> None:
    cur = set(config.get().get("lib_hidden", []) or [])
    (cur.add if hidden else cur.discard)(key)
    _update({"lib_hidden": sorted(cur)})


def _update(patch: dict) -> None:
    """复用 config 持久化入口。"""
    config.save(patch)


# --------------------------------------------------------------- 排序

# placement: "front" 全部本地库之前 | "back" 全部本地库之后 |
#            "after-local:<本地库guid>" 插到指定本地库之后
def lib_order() -> list[str]:
    """虚拟库的自定义相对顺序（库键数组）。未列入的库按默认顺序追加在后。"""
    try:
        return list(config.get().get("lib_order", []) or [])
    except Exception:
        return []


def lib_placement_default() -> str:
    return str(config.get().get("lib_placement", "front"))  # front/back/after


def lib_anchor() -> str:
    return str(config.get().get("lib_anchor", "") or "")


def order_index(key: str) -> int:
    try:
        return lib_order().index(key)
    except ValueError:
        return 10_000_000  # 未自定义的排到已定义之后


def set_order(keys: list[str]) -> None:
    _update({"lib_order": [str(k) for k in keys][:500]})


def sort_keys(keys: list[str]) -> list[str]:
    """按自定义顺序排序；未列出的库保持稳定地追加在后。"""
    n = len(keys)
    return sorted(keys, key=lambda k: (order_index(k) if order_index(k) < 10_000_000
                                       else 10_000_000 + keys.index(k)))


# --------------------------------------------------------------- 子账号授权

def _acl() -> dict:
    try:
        return dict(config.get().get("lib_acl", {}) or {})
    except Exception:
        return {}


def set_account_acl(user_guid: str, allowed_keys: list[str] | None) -> None:
    """设置某账号允许观看的库；None=删除其单独配置（回到默认）。"""
    acl = _acl()
    if allowed_keys is None:
        acl.pop(user_guid, None)
    else:
        acl[user_guid] = sorted(set(allowed_keys))[:1000]
    _update({"lib_acl": acl})


def get_account_acl(user_guid: str):
    """返回该账号配置；None 表示未单独配置（走默认）。"""
    return _acl().get(user_guid)


def account_can_see(user: dict, key: str) -> bool:
    """该账号能否看到某虚拟库。

    - 识别不出身份 / 管理员 / 未配置账号 → 默认可见（acl_default_visible）
    - 已配置账号 → 仅允许列表内可见
    - 任何被管理员全局隐藏的库对所有人不可见（含管理员自己的飞牛列表，
      管理页仍可重新打开）。
    """
    if not key:
        return True
    if is_hidden(key):
        return False
    if not user or not user.get("known") or user.get("is_admin"):
        return True
    allow = _acl().get(user.get("guid", ""))
    if allow is None:
        return bool(config.get().get("acl_default_visible", True))
    return key in allow


def account_online_allowed(user: dict) -> bool:
    """该账号能否使用在线搜索 / 在线片源（家长可只给孩子开本地授权库）。"""
    if not user or not user.get("known") or user.get("is_admin"):
        return True
    allow = _acl().get(user.get("guid", ""))
    if allow is None:
        return bool(config.get().get("acl_default_online", True))
    # 已配置账号：用其授权里的特殊标记 "__online__" 决定
    return "__online__" in allow

