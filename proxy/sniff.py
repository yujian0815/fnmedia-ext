#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页嗅探引擎：把"播放页 URL"解析成真实媒体地址（m3u8/mp4 等）。

纯 HTTP + 正则/JSON 解析，不需要安卓模拟器，用于替代部分原本只能靠加固爬虫 jar
或 WebView 才能取链的网页源。支持的常见形态：
  1) MacCMS player_aaaa = {...,"url":"..."}（最常见，含 unicode 转义）；
  2) 直接内嵌的 .m3u8/.mp4 地址（多种 JS 变量写法）；
  3) <iframe>/<script> 嵌套播放页（递归跟随，最多 N 层，同源/跨域带 Referer）；
  4) 302 跳转型播放接口（ac=videodiag 等）；
  5) 常见加密/混淆：\\xNN、\\uNNNN、HTML 实体、base64 形式的媒体地址。

设计：每个页面只取"最像主播放流"的一个地址；带 TTL 缓存；所有网络由调用方传入
httpx.AsyncClient，便于复用连接与统一代理设置。通用能力，公开构建保留。
"""
from __future__ import annotations

import base64
import html
import json
import re
import time
import urllib.parse

# 直链媒体后缀/特征
_MEDIA_EXT = re.compile(r"\.(m3u8|mp4|flv|mkv|ts|mpd)(\?|$)", re.I)
# 明显是媒体 CDN（无后缀但路径像流）
_MEDIA_PATH = re.compile(
    r"(/hls/|/live/|/play/|/media/|/video/|/m3u8/|/vod/|"
    r"/\d{4,}/[^\"'&\s]+\.(?:m3u8|mp4))", re.I)

# player_aaaa（MacCMS 标准播放器变量）
_PLAYER_AAAA = re.compile(
    r"player_aaaa\s*=\s*(\{.*?\})\s*[;<]", re.I | re.S)
# MacCMS 老版本 player_aaaa 可能用 ={ 换行等
_PLAYER_AAAA_LOOSE = re.compile(
    r"(?:var|let|const)?\s*player_aaaa\s*=\s*(\{)", re.I)

# iframe / embed
_IFRAME_RE = re.compile(
    r"<(?:iframe|embed|source)[^>]+?(?:src|data)\s*=\s*[\"']([^\"']+)[\"']",
    re.I | re.S)
# script src 指向的播放脚本（仅当同域且像播放器时）
_SCRIPT_RE = re.compile(
    r"<script[^>]+?src\s*=\s*[\"']([^\"']+)[\"']", re.I)

# 各种直接写媒体地址的 JS 形态
_INLINE_RES = [
    re.compile(r"""(?:const|let|var)\s+\w*[Uu]rl\w*\s*=\s*["']([^"']+)["']"""),
    re.compile(r"""["'](?:url|src|file|videoUrl|playUrl|playurl|link)["']\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""(?:src|file|url)\s*=\s*["']([^"']+\.(?:m3u8|mp4)[^"']*)["']""", re.I),
    re.compile(r"""["'](https?://[^"'\s<>]+?\.(?:m3u8|mp4)(?:\?[^"']*)?)["']""", re.I),
    re.compile(r"""(https?://[^"'\s<>]+?/m3u8/[^"'\s<>]+)""", re.I),
]

_B64_RE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")
_HEX_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}")
_UNICODE_ESC = re.compile(r"(?:\\u[0-9a-fA-F]{4}){3,}")

_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 30 * 60


def _is_media(u: str) -> bool:
    if not u:
        return False
    if _MEDIA_EXT.search(u):
        return True
    # 阿里云/CDN 签名流常无后缀但带明确媒体路径
    return bool(_MEDIA_PATH.search(u))


def _decode_escapes(s: str) -> str:
    """解码 \\xNN / \\uNNNN / HTML 实体，便于正则在混淆文本里找地址。"""
    try:
        if _HEX_RE.search(s):
            s = re.sub(r"\\x([0-9a-fA-F]{2})",
                       lambda m: chr(int(m.group(1), 16)), s)
        if _UNICODE_ESC.search(s):
            s = re.sub(r"\\u([0-9a-fA-F]{4})",
                       lambda m: chr(int(m.group(1), 16)), s)
        s = html.unescape(s)
    except Exception:
        pass
    return s


def _try_b64_media(text: str) -> str:
    """某些站把 m3u8 地址 base64 后塞进 JS。解出是媒体才用。"""
    for m in _B64_RE.finditer(text):
        try:
            dec = base64.b64decode(m.group(0)).decode("utf-8", "ignore")
        except Exception:
            continue
        mm = _MEDIA_EXT.search(dec)
        if not mm:
            continue
        # 从解码串里取出包含该媒体后缀的完整 http(s) 地址
        u = re.search(r"https?://[^\s\"'<>]+", dec[max(0, mm.start()-300):])
        if u:
            return u.group(0)
    return ""


def _balanced_json_object(text: str, brace_pos: int) -> str | None:
    """从 '{' 起按括号配平截取一个完整 JSON 对象字符串。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(brace_pos, min(len(text), brace_pos + 8000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_pos:i + 1]
    return None


def _extract_player_aaaa(html_text: str) -> dict | None:
    """提取并解析 MacCMS player_aaaa 配置。"""
    m = _PLAYER_AAAA_LOOSE.search(html_text)
    if not m:
        return None
    obj_s = _balanced_json_object(html_text, m.start(1))
    if not obj_s:
        # 退化：用非贪婪正则
        mm = _PLAYER_AAAA.search(html_text)
        obj_s = mm.group(1) if mm else ""
    if not obj_s:
        return None
    obj_s = _decode_escapes(obj_s)
    try:
        j = json.loads(obj_s)
        return j if isinstance(j, dict) else None
    except Exception:
        # 单引号/尾逗号等宽松修复
        try:
            fixed = re.sub(r",\s*}", "}", obj_s)
            fixed = re.sub(r",\s*]", "]", fixed).replace("'", '"')
            return json.loads(fixed)
        except Exception:
            return None


def _extract_inline(html_text: str) -> str:
    for rx in _INLINE_RES:
        m = rx.search(html_text)
        if m:
            cand = m.group(1)
            if _is_media(cand):
                return cand
    return ""


def _extract_iframe(html_text: str) -> list[str]:
    out = []
    for m in _IFRAME_RE.finditer(html_text):
        src = html.unescape(m.group(1)).strip()
        if src and not src.startswith(("javascript:", "data:", "about:")):
            out.append(src)
    return out


async def _get(client, url: str, referer: str = "", timeout: float = 12.0):
    headers = {"User-Agent":
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    if referer:
        headers["Referer"] = referer
    return await client.get(url, headers=headers, timeout=timeout,
                            follow_redirects=True)


async def sniff(client, page_url: str, referer: str = "",
                max_depth: int = 3) -> str | None:
    """把播放页解析为真实媒体地址。成功返回绝对 URL，失败 None。带缓存。"""
    page_url = (page_url or "").strip()
    if not page_url:
        return None
    hit = _cache.get(page_url)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    url = page_url
    ref = referer
    visited: set[str] = set()
    for _depth in range(max_depth + 1):
        if url in visited:
            break
        visited.add(url)
        try:
            r = await _get(client, url, ref)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("content-type") or "").lower()
        # 直接命中媒体（部分播放接口 200 直接回流）
        if "mpegurl" in ctype or "video/" in ctype or _MEDIA_EXT.search(str(r.url)):
            found = str(r.url)
            _cache[page_url] = (time.time(), found)
            return found
        body = r.text if "text" in ctype or "javascript" in ctype or \
            "json" in ctype or not ctype else ""
        if not body:
            return None
        text = _decode_escapes(body)
        base = str(r.url)

        # 1) player_aaaa（最优先，MacCMS 标配）
        pa = _extract_player_aaaa(text)
        if pa:
            for k in ("url", "playUrl", "playurl", "link", "vid"):
                v = pa.get(k)
                if isinstance(v, str) and v.strip():
                    cand = v.strip()
                    if _is_media(cand):
                        found = urllib.parse.urljoin(base, cand)
                        _cache[page_url] = (time.time(), found)
                        return found
                    # url 可能本身还是下一层播放页（相对/域名）
                    if cand.startswith(("http://", "https://", "/")) and k == "url":
                        url = urllib.parse.urljoin(base, cand)
                        ref = base
                        break
            else:
                url = ""
            if url and url != base:
                continue

        # 2) 直接内嵌
        cand = _extract_inline(text)
        if cand:
            found = urllib.parse.urljoin(base, cand)
            _cache[page_url] = (time.time(), found)
            return found

        # 3) base64 混淆
        cand = _try_b64_media(text)
        if cand:
            _cache[page_url] = (time.time(), cand)
            return cand

        # 4) iframe 嵌套：挑最像播放器的一个继续
        frames = _extract_iframe(text)
        if frames:
            # 优先含 player/play/iframe/m3u8 关键词的
            frames.sort(key=lambda u: 0 if re.search(
                r"player|play|iframe|m3u8|vod", u, re.I) else 1)
            url = urllib.parse.urljoin(base, frames[0])
            ref = base
            continue

        return None
    return None


def sniff_sync_html(html_text: str, base_url: str = "") -> str | None:
    """不发请求，只对已有 HTML 做解析（供已抓到页面的调用方复用）。"""
    text = _decode_escapes(html_text)
    pa = _extract_player_aaaa(text)
    if pa:
        for k in ("url", "playUrl", "playurl", "link"):
            v = pa.get(k)
            if isinstance(v, str) and _is_media(v):
                return urllib.parse.urljoin(base_url, v.strip())
    cand = _extract_inline(text) or _try_b64_media(text)
    return urllib.parse.urljoin(base_url, cand) if cand else None
