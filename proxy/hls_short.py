"""iOS fncache 短分片 URL：只改响应副本，原签名参数留在服务端内存。"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse

SEG_TTL = 6 * 60 * 60.0
SEG_MAX = 60_000

# sid -> (expires, media_guid, endpoint kind, original query)
_SEGS: dict[str, tuple[float, str, str, str]] = {}
_URI_ATTR = re.compile(r'(URI=")([^"]+)(")', re.I)


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _purge(now: float) -> None:
    # 所有项使用相同 TTL，刷新时重新移到末尾，因此首项总是最早过期。
    while _SEGS:
        sid = next(iter(_SEGS))
        if _SEGS[sid][0] > now and len(_SEGS) < SEG_MAX:
            break
        _SEGS.pop(sid, None)


def put_segment(mg: str, kind: str, query: str,
                now: float | None = None) -> str:
    """登记原始分片 query，返回稳定的 16 位十六进制短 ID。"""
    if not mg or kind not in ("seg", "hls"):
        raise ValueError("invalid segment mapping")
    query = query.lstrip("?")
    ts = _now(now)
    _purge(ts)
    payload = (mg, kind, query)
    raw = f"{mg}\0{kind}\0{query}".encode("utf-8")
    for nonce in range(1000):
        suffix = b"" if nonce == 0 else f"\0{nonce}".encode("ascii")
        sid = hashlib.blake2s(raw + suffix, digest_size=8).hexdigest()
        hit = _SEGS.get(sid)
        if hit and hit[0] > ts and hit[1:] != payload:
            continue  # 极低的 64 位碰撞也不能串到另一段视频
        _SEGS.pop(sid, None)
        _SEGS[sid] = (ts + SEG_TTL, *payload)
        return sid
    raise RuntimeError("too many short segment id collisions")


def get_segment(sid: str, now: float | None = None) -> tuple[str, str, str] | None:
    """读取未过期映射；返回值是 media guid、端点类型和原始 query。"""
    hit = _SEGS.get(sid)
    if not hit:
        return None
    expires, mg, kind, query = hit
    if expires <= _now(now):
        _SEGS.pop(sid, None)
        return None
    return mg, kind, query


def _short_uri(uri: str, base_mg: str, prefix: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return uri
    # 已有源站绝对 URL 是明确的直连选择，不能偷偷改回代理。
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.query:
        return uri
    root = prefix.rstrip("/") + "/"
    mg = base_mg
    kind = ""
    if parsed.path in ("seg", "hls"):
        kind = parsed.path
    elif parsed.path.startswith(root):
        parts = parsed.path[len(root):].split("/")
        if len(parts) == 2 and parts[0] and parts[1] in ("seg", "hls"):
            mg, kind = parts
    if not mg or not kind:
        return uri
    sid = put_segment(mg, kind, parsed.query)
    return f"{root}seg/{sid}.ts"


def shorten_playlist(body: bytes, mg: str,
                     prefix: str = "/v/media/online") -> bytes:
    """仅缩短清单中的本代理分片 URI，保留其他字节和换行格式。"""
    try:
        text = body.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return body
    out = []
    for raw in text.splitlines(keepends=True):
        if raw.endswith("\r\n"):
            line, eol = raw[:-2], "\r\n"
        elif raw.endswith(("\n", "\r")):
            line, eol = raw[:-1], raw[-1:]
        else:
            line, eol = raw, ""
        if line.lstrip("\ufeff ").startswith("#"):
            line = _URI_ATTR.sub(
                lambda m: m.group(1)
                + _short_uri(m.group(2), mg, prefix) + m.group(3), line)
        else:
            stripped = line.strip()
            shortened = _short_uri(stripped, mg, prefix)
            if shortened != stripped:
                line = shortened
        out.append(line + eol)
    return "".join(out).encode("utf-8")
