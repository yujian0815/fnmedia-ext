#!/usr/bin/env python3
"""订阅资源抓取的共享低层工具（仅服务 TVBox/订阅导入）。

唯一职责：
  - 读取用户为「拉取订阅资源」单独配置的 HTTP(S) 代理（订阅配置 / spider jar /
    DRPY 规则脚本）。播放、聚合搜索、CMS 探测等一律不用它，产品客户端始终
    trust_env=False 直连，避免误伤播放流量。
  - 统一构造带该代理（且仍 trust_env=False，不读系统 Clash 环境变量）的 httpx
    客户端；并提供 GitHub 原始域名的国内镜像候选，源站 NXDOMAIN/被墙时兜底。

此模块无任何专有逻辑，可进入公开构建（未配置代理时退化为直连）。
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

# 抓取订阅资源用的超时（jar 可能数 MB，给宽一些）
SUB_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
_UA = "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36"

# GitHub 原始 / 常见加速域名（这些域名在国内 NAS 上常 NXDOMAIN 或被墙）
_GH_HOSTS = ("raw.githubusercontent.com", "raw.kkgithub.com", "raw.bgithub.xyz",
             "raw.fastgit.org", "ghproxy.com")


def subscription_proxy() -> str:
    """当前订阅资源代理 URL（空串=直连）。惰性读取 config，避免循环导入。"""
    try:
        import config
        return str(config.config.get().get("subscription_proxy") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _proxy_kwargs(px: str) -> dict:
    """按运行时 httpx 版本返回代理关键字。

    httpx <0.26 的 Client/AsyncClient 只接受 proxies=，0.26+ 改用 proxy= 且
    proxies= 被移除。配置了代理时必须命中正确参数名，否则 TypeError 会让
    fetch_first 等低层抓取静默全失败（DRPY 规则、spider jar 都走这里）。
    """
    if not px:
        return {}
    try:
        httpx.Client(trust_env=False, proxy=px)
        return {"proxy": px}
    except TypeError:
        return {"proxies": px}


def sub_client() -> httpx.Client:
    """同步客户端：仅用于拉取订阅资源。trust_env=False（不读系统代理），
    但显式应用用户配置的订阅代理。"""
    return httpx.Client(trust_env=False, verify=False, follow_redirects=True,
                        timeout=SUB_TIMEOUT, **_proxy_kwargs(subscription_proxy()))


def async_sub_client() -> httpx.AsyncClient:
    px = subscription_proxy()
    return httpx.AsyncClient(trust_env=False, verify=False, follow_redirects=True,
                             timeout=SUB_TIMEOUT, **_proxy_kwargs(px))


def github_mirror_candidates(url: str) -> list[str]:
    """GitHub 原始/加速域名资源 → [原URL, 各国内镜像候选]；非 GitHub 只返回原 URL。"""
    out = [url]
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        parts = [seg for seg in p.path.split("/") if seg]
        if host in _GH_HOSTS and len(parts) >= 4:
            owner, repo, branch = parts[0], parts[1], parts[2]
            rest = "/".join(parts[3:])
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"
            for c in (
                f"https://ghproxy.net/{raw}",
                f"https://gh-proxy.com/{raw}",
                f"https://ghfast.top/{raw}",
                f"https://raw.gitmirror.com/{owner}/{repo}/{branch}/{rest}",
                f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{rest}",
            ):
                if c not in out:
                    out.append(c)
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_first(urls, *, want_zip: bool = False, timeout: float | None = None):
    """用订阅代理按顺序尝试一组 URL。返回 (bytes, 命中URL)；全失败返回 (None, 错误)。

    want_zip=True 时额外校验 zip(jar/apk) 魔数 PK。
    """
    last = ""
    for u in urls:
        try:
            kw = {} if timeout is None else {"timeout": httpx.Timeout(timeout, connect=10.0)}
            kw.update(_proxy_kwargs(subscription_proxy()))
            with httpx.Client(trust_env=False, verify=False, follow_redirects=True,
                              **kw) as cli:
                r = cli.get(u, headers={"User-Agent": _UA})
                r.raise_for_status()
                blob = r.content
            if not blob:
                last = f"空响应 @ {u}"
                continue
            if want_zip and blob[:2] != b"PK":
                last = f"非有效 jar(zip) @ {u}"
                continue
            return blob, u
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__} @ {u}"
            continue
    return None, last
