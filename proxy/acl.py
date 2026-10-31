#!/usr/bin/env python3
"""按飞牛影视用户控制「在线媒体库」的可见性（本地白名单）。

设计要点
--------
1. **名单在本地**：允许谁看，写在 state/config.json 的 user_acl_* 里，不依赖上游
   的用户表，上游接口字段变了也不影响判定。
2. **身份仍来自飞牛登录态**：取请求头里的 token，经内网 socket 调一次
   /v/api/v1/user/info 换出用户名，按 token 哈希缓存 5 分钟。同一个播放器进程
   只在首次请求时多花一次往返（通常 < 20ms，走 UDS）。
3. **解析不到身份**时按 user_acl_fail_open 处理（默认放行），避免上游鉴权抖动
   导致所有人被踢出媒体库。
4. **最近识别到的用户**会被记在内存里，管理页可以直接点选加入白名单，不用手打。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import config as cfgmod
from config import config
from fnmedia_authx import make_authx

# token -> (过期时间, user|None)；user 为 None 表示解析失败（负缓存，避免反复打上游）
_CACHE: dict[str, tuple[float, dict | None]] = {}
USER_TTL = 300.0        # 身份正缓存 5 分钟
FAIL_TTL = 60.0         # 解析失败负缓存 60 秒
CACHE_MAX = 512

# 最近识别到的用户（username/uid -> 信息），仅内存，供管理页展示
_SEEN: dict[str, dict] = {}
_SEEN_MAX = 50

_TOKEN_HEADERS = ("authorization", "x-token", "x-auth-token")
_TOKEN_COOKIES = ("token", "Authorization", "trim_token", "auth_token")


def _cfg() -> dict:
    """读配置快照。Config.get() 会做一次深拷贝（含全部源表），请求热路径改用
    原始 dict 只读标量键，避免每请求多一次序列化开销。"""
    c = getattr(config, "_cfg", None)
    if isinstance(c, dict):
        return c
    try:
        return config.get() or {}
    except Exception:
        return {}


def _token_of(request) -> str:
    """从请求里取登录 token。飞牛影视是裸 token（非 Bearer），这里两种都兼容。"""
    for h in _TOKEN_HEADERS:
        v = (request.headers.get(h) or "").strip()
        if v:
            return v
    for c in _TOKEN_COOKIES:
        v = (request.cookies.get(c) or "").strip()
        if v:
            return v
    return ""


def _key(token: str) -> str:
    return hashlib.sha1(token.encode("utf-8", "replace")).hexdigest()


def _candidates(token: str):
    """token 的候选写法：原样、去掉 Bearer 前缀、补上 Bearer 前缀。"""
    out = [token]
    t = token.strip()
    if t.lower().startswith("bearer "):
        out.append(t[7:].strip())
    else:
        out.append("Bearer " + t)
    return [x for x in dict.fromkeys(out) if x]


def _pick(d: dict) -> tuple[str, str, bool]:
    """从 user/info 的 data 里取出 (username, uid, is_admin)。

    实测 0.2.33 上游返回（2026-10-30 抓取）：
        {"guid":"ff6f...","username":"zxl","lan":"zh-CN","audio_lan":"",
         "is_admin":0,"last_login_time":0,"sources":[]}
    → name=username，uid=guid（32 位十六进制，改用户名也不变，
      白名单里填 guid 同样能匹配），admin 由 is_admin 整数 0/1 判定。
    """
    name = ""
    for k in ("username", "user_name", "name", "nickname", "account", "email"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    uid = ""
    # guid 放最后：上游同时给 uid 与 guid 时优先用更“像 ID”的那个
    for k in ("uid", "id", "user_id", "userId", "guid", "user_guid"):
        v = d.get(k)
        if v not in (None, "", 0, "0"):
            uid = str(v)
            break
    admin = False
    for k in ("is_admin", "admin", "isAdmin", "is_super", "is_root"):
        v = d.get(k)
        if isinstance(v, bool):
            admin = admin or v
        elif isinstance(v, (int, str)) and str(v) in ("1", "true", "True"):
            admin = admin or True
    role = d.get("role") or d.get("user_type") or d.get("type")
    if isinstance(role, str) and role.lower() in ("admin", "administrator", "super", "root"):
        admin = True
    if isinstance(role, int) and role == 1:
        admin = True
    return name, uid, admin


_HOP_HEADERS = ("connection", "content-length", "host", "authorization",
                "authx", "x-token", "x-auth-token",
                # 不转发客户端编码偏好：浏览器常要 br，venv 里没有 brotli 解码器时
                # r.json() 会解析失败，表现为“有 token 却认不出用户”。让 httpx 自己协商。
                "accept-encoding")


def _base_headers(request) -> dict:
    """复用调用方请求里除鉴权签名外的头（cookie / user-agent / x-* 等）。

    /v/api/v1/user/info 万一还校验 cookie 或客户端标识，光带 Authorization
    会换不出用户；这里跟 forward_upstream 一样只剔掉逐跳头和会被我们覆盖的头。
    """
    if request is None:
        return {}
    try:
        items = list(request.headers.items())
    except Exception:
        return {}
    return {k: v for k, v in items if k.lower() not in _HOP_HEADERS}


async def _fetch(client, token: str, request=None) -> dict | None:
    """用请求里的 token 向上游换用户名。失败返回 None（不抛异常）。"""
    authx, _ = make_authx("GET", "/v/api/v1/user/info")
    base = _base_headers(request)
    for cand in _candidates(token):
        try:
            r = await asyncio.wait_for(client.get(
                "http://localhost/v/api/v1/user/info",
                headers={**base, "Host": "localhost", "authx": authx,
                         "Authorization": cand},
                timeout=6.0), timeout=7.0)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("code") not in (0, "0", None):
            continue
        d = data.get("data")
        if isinstance(d, list):
            d = d[0] if d and isinstance(d[0], dict) else None
        if not isinstance(d, dict):
            continue
        name, uid, admin = _pick(d)
        if not name and not uid:
            continue
        user = {"name": name, "uid": uid, "admin": admin, "ts": time.time()}
        _remember(user)
        return user
    return None


def _remember(user: dict) -> None:
    k = (user.get("name") or "").lower() or user.get("uid") or ""
    if not k:
        return
    _SEEN[k] = {"name": user.get("name", ""), "uid": user.get("uid", ""),
                "admin": bool(user.get("admin")), "ts": int(user.get("ts") or 0)}
    if len(_SEEN) > _SEEN_MAX:
        for kk, _ in sorted(_SEEN.items(), key=lambda kv: kv[1].get("ts", 0))[:len(_SEEN) - _SEEN_MAX]:
            _SEEN.pop(kk, None)


async def resolve(request, client) -> dict | None:
    """解析请求对应的飞牛用户；无 token 或解析失败返回 None。"""
    token = _token_of(request)
    if not token:
        return None
    k = _key(token)
    now = time.time()
    hit = _CACHE.get(k)
    if hit and hit[0] > now:
        u = hit[1]
        if u:
            _remember(u)
        return u
    user = await _fetch(client, token, request)
    _CACHE[k] = (now + (USER_TTL if user else FAIL_TTL), user)
    if len(_CACHE) > CACHE_MAX:
        for kk, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:len(_CACHE) - CACHE_MAX]:
            _CACHE.pop(kk, None)
    return user


def enabled() -> bool:
    return bool(_cfg().get("user_acl_enabled"))


def needs_identity() -> bool:
    """只有「启用 + 白名单」才值得花一次往返去认人；其余情况直接放行。"""
    c = _cfg()
    return bool(c.get("user_acl_enabled")) and \
        (c.get("user_acl_mode") or "all") == "whitelist"


def token_present(request) -> bool:
    """该请求是否带了登录 token（管理页自检用）。"""
    return bool(_token_of(request))


def visible(user: dict | None) -> bool:
    """用户能否看到在线媒体库 / 在线搜索结果。"""
    c = _cfg()
    if not c.get("user_acl_enabled"):
        return True
    mode = c.get("user_acl_mode") or "all"
    if mode != "whitelist":          # all：全员可见
        return True
    fail_open = bool(c.get("user_acl_fail_open", True))
    if user is None:                 # 认不出是谁
        return fail_open
    if user.get("admin") and c.get("user_acl_allow_admin", True):
        return True
    names = {(str(x).strip().lower()) for x in (c.get("user_acl_users") or []) if str(x).strip()}
    if not names:                    # 白名单是空的：不要把人锁在门外
        return fail_open
    if user.get("name") and user["name"].lower() in names:
        return True
    if user.get("uid") and str(user["uid"]).lower() in names:
        return True
    return False


async def gate(request, client) -> bool:
    """供中间件调用：解析身份、写 request.state.acl_ok，返回是否允许。"""
    if not needs_identity():
        request.state.acl_ok = True
        request.state.acl_user = None
        return True
    try:
        user = await resolve(request, client)
    except Exception:
        user = None
    if user is None and _token_of(request):
        _warn_unresolved()
    ok = visible(user)
    request.state.acl_ok = ok
    request.state.acl_user = user
    return ok


_LAST_WARN = [0.0]


def _warn_unresolved() -> None:
    """白名单模式下去了 token 却认不出人，是最容易踩的坑：日志里提示一次。"""
    now = time.time()
    if now - _LAST_WARN[0] < 300:
        return
    _LAST_WARN[0] = now
    logging.getLogger("fnmedia").warning(
        "用户可见性：带了 token 但无法解析用户名（/v/api/v1/user/info 不可用），"
        "当前按 fail_open=%s 处理；管理页「最近识别到的用户」会为空",
        bool(_cfg().get("user_acl_fail_open", True)))


def denies(request) -> bool:
    """当前请求是否被禁止访问在线内容（未经过闸门的请求视为允许）。"""
    return not bool(getattr(request.state, "acl_ok", True))


def recent() -> list[dict]:
    """最近识别到的用户，按时间倒序，供管理页点选加入白名单。"""
    out = [dict(v) for v in _SEEN.values()]
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return out


def reset_cache() -> None:
    """改完白名单后清掉身份缓存（判定结果变了，但身份本身不用重查）。"""
    _CACHE.clear()


def status() -> dict:
    c = _cfg()
    return {
        "enabled": bool(c.get("user_acl_enabled")),
        "mode": c.get("user_acl_mode") or "all",
        "users": list(c.get("user_acl_users") or []),
        "allow_admin": bool(c.get("user_acl_allow_admin", True)),
        "fail_open": bool(c.get("user_acl_fail_open", True)),
        "recent": recent(),
        "cached_identities": len(_CACHE),
    }
