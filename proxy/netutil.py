#!/usr/bin/env python3
"""资源站取源的共享低层工具（聚合搜索 / 分类深翻 / 测速探测 / CMS 详情）。

职责：
  - 读取用户为「访问资源站」单独配置的 HTTP(S)/SOCKS5 代理（issue #30）。
    部分运营商直连资源站超时或被墙，挂代理后搜索结果与速度显著改善。
  - 支持三种代理模式（source_proxy_mode）：
      * "off" ：一律直连，忽略已配置代理；
      * "on"  ：所有取源请求都走代理；
      * "auto"：默认直连；当某个资源站主机直连发生**连接级失败**（连接被重置、
                超时、DNS 失败、TLS 握手失败等，而非正常的 HTTP 4xx/5xx）时，
                自动对该主机改用代理重试，并在本进程内记住它，其余站点保持直连。
  - 统一构造 trust_env=False（不读系统 Clash 环境变量）的 httpx 客户端。

不含任何专有逻辑，可进入公开构建。媒体播放（m3u8 / 分片）默认不走此代理，
以免增加代理带宽压力；需要时调用方可显式传 proxy。
"""
from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


# 阻塞式源适配器（例如需要同步 HTTP/RPC 的源）不能占用 asyncio 默认线程池：
# 默认池还负责 httpx 的 DNS/连接等异步底层工作，一旦被慢源排队占满，海报、
# 搜索和播放都会表现为 ConnectTimeout。独立小池把慢源等待隔离在自身内部。
_BLOCKING_SOURCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="blocking-source")


async def run_blocking_source(func, *args):
    """在独立小线程池运行同步源适配器，避免饿死全局 asyncio 工作线程。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _BLOCKING_SOURCE_EXECUTOR, functools.partial(func, *args))

try:
    from urllib.parse import urlsplit
except Exception:  # pragma: no cover
    urlsplit = None


# 连接级失败：这些异常意味着"根本没连上/被墙"，值得切换代理重试。
# HTTP 状态错误（4xx/5xx）不在其中——能连上只是业务/鉴权响应，换代理无意义。
_CONN_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    httpx.ProxyError,
)

# auto 模式下"已确认需要代理"的主机集合，进程内共享（学习结果跨客户端复用）。
# 值为学习到的时间戳，用于定期复检（避免某站恢复直连后永远走代理）。
_proxy_hosts: dict[str, float] = {}
_LOCK = threading.RLock()
_RECHECK_SECONDS = 1800.0  # 已判定走代理的主机，30 分钟后允许再尝试一次直连


def source_proxy() -> str:
    """当前资源站代理 URL（空串=直连）。惰性读 config，避免循环导入。"""
    try:
        from config import config as _cfg
        return str(_cfg.get().get("source_proxy") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def proxy_mode() -> str:
    """资源站代理模式：off / on / auto。"""
    try:
        from config import config as _cfg
        m = str(_cfg.source_proxy_mode() or "auto").strip().lower()
        return m if m in ("off", "on", "auto") else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


def host_of(url: str | httpx.URL) -> str:
    try:
        u = url if isinstance(url, httpx.URL) else httpx.URL(url)
        return (u.host or "").lower()
    except Exception:  # noqa: BLE001
        try:
            return (urlsplit(str(url)).hostname or "").lower()
        except Exception:  # noqa: BLE001
            return ""


def host_needs_proxy(host: str) -> bool:
    """auto 模式学习结果：该主机是否已被判定为需要代理。"""
    if not host:
        return False
    with _LOCK:
        ts = _proxy_hosts.get(host)
        if ts is None:
            return False
        if time.time() - ts >= _RECHECK_SECONDS:
            # 到期复检：先摘除，给直连一次机会；仍失败会再次学习。
            _proxy_hosts.pop(host, None)
            return False
        return True


def mark_host_proxy(host: str) -> None:
    if host:
        with _LOCK:
            _proxy_hosts[host] = time.time()


def clear_proxy_hosts() -> None:
    """清空 auto 模式学习结果（管理页切换/排错用）。"""
    with _LOCK:
        _proxy_hosts.clear()


def _proxy_kwargs(px: str) -> dict:
    if not px:
        return {}
    # httpx >= 0.26 使用 proxy=；极旧版用 proxies=
    try:
        httpx.AsyncClient(proxy=px)
        return {"proxy": px}
    except TypeError:
        return {"proxies": px}


def _client_kwargs(timeout: float, follow_redirects: bool, verify: bool) -> dict:
    return {"follow_redirects": follow_redirects, "trust_env": False,
            "verify": verify, "timeout": timeout}


def async_client(*, timeout: float = 12.0, follow_redirects: bool = True,
                 verify: bool = True, px: str | None = None) -> httpx.AsyncClient:
    """构造访问资源站用的异步客户端。

    - px 显式给出（""或URL）时：按该值构造（兼容旧调用，不做自适应）；
    - px=None（默认）时：按配置的代理模式构造——off 直连、on 全走代理、
      auto 返回自适应客户端（直连失败按主机自动切代理）。
    """
    if px is not None:
        url = px.strip()
        return httpx.AsyncClient(**_client_kwargs(timeout, follow_redirects, verify),
                                 **_proxy_kwargs(url))

    mode = proxy_mode()
    purl = source_proxy()
    if mode == "on" and purl:
        return httpx.AsyncClient(**_client_kwargs(timeout, follow_redirects, verify),
                                 **_proxy_kwargs(purl))
    if mode != "auto" or not purl:
        # off，或 auto 但没配置代理：纯直连
        return httpx.AsyncClient(**_client_kwargs(timeout, follow_redirects, verify))
    # auto 且已配置代理：自适应
    direct = httpx.AsyncClient(**_client_kwargs(timeout, follow_redirects, verify))
    proxied = httpx.AsyncClient(**_client_kwargs(timeout, follow_redirects, verify),
                                **_proxy_kwargs(purl))
    return _AdaptiveAsyncClient(direct, proxied)


def sync_client(*, timeout: float = 12.0, follow_redirects: bool = True,
                verify: bool = True, px: str | None = None):
    """构造访问资源站用的同步客户端（测速/探测等同步场景）。

    auto 模式同样自适应：直连连接级失败时按主机自动切代理重试。
    """
    if px is not None:
        url = px.strip()
        return httpx.Client(**_client_kwargs(timeout, follow_redirects, verify),
                            **_proxy_kwargs(url))

    mode = proxy_mode()
    purl = source_proxy()
    if mode == "on" and purl:
        return httpx.Client(**_client_kwargs(timeout, follow_redirects, verify),
                            **_proxy_kwargs(purl))
    if mode != "auto" or not purl:
        return httpx.Client(**_client_kwargs(timeout, follow_redirects, verify))
    direct = httpx.Client(**_client_kwargs(timeout, follow_redirects, verify))
    proxied = httpx.Client(**_client_kwargs(timeout, follow_redirects, verify),
                           **_proxy_kwargs(purl))
    return _AdaptiveSyncClient(direct, proxied)


class _AdaptiveBase:
    """按主机在"直连/代理"两个底层客户端之间路由。

    - 已学习为需要代理的主机直接走代理；其余先走直连；
    - 直连抛出连接级异常时，学习该主机并用代理重试同一次请求；
    - 走代理请求需重新构造（httpx 的 Request 绑定在原 client 的 transport 上）。
    """

    def __init__(self, direct, proxied):
        self._direct = direct
        self._proxied = proxied
        self._closed = False

    @staticmethod
    def _host_of(request: httpx.Request) -> str:
        try:
            return (request.url.host or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    def _is_conn_error(self, exc: BaseException) -> bool:
        if isinstance(exc, _CONN_ERRORS):
            # httpx 把 SSL/握手等包在 ConnectError 里；超时也算
            return True
        return False


class _AdaptiveAsyncClient(_AdaptiveBase):
    async def send(self, request: httpx.Request, *, stream: bool = False,
                   **kwargs) -> httpx.Response:
        host = self._host_of(request)
        use_proxy = bool(host) and host_needs_proxy(host)
        client = self._proxied if use_proxy else self._direct
        try:
            return await client.send(request, stream=stream, **kwargs)
        except Exception as exc:
            if use_proxy or stream or not self._is_conn_error(exc):
                raise
            # 直连连接级失败：学习该主机并改用代理重发同一次请求（重建请求对象，
            # 因为它绑定在原直连 transport 上）。
            mark_host_proxy(host)
            retry = self._rebuild(request)
            return await self._proxied.send(retry, stream=stream, **kwargs)

    @staticmethod
    def _rebuild(request: httpx.Request) -> httpx.Request:
        return httpx.Request(
            request.method, str(request.url),
            params=list(request.url.params.multi_items()),
            headers=request.headers.raw,
            content=request.content,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for c in (self._direct, self._proxied):
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # 便捷方法：build_request 委托直连 client 构造（send 时再按主机路由）。
    # 注意 timeout/extensions 属于 build_request（Request 级），不能传给 send。
    def build_request(self, *args, **kwargs) -> httpx.Request:
        return self._direct.build_request(*args, **kwargs)

    async def get(self, url, *, params=None, headers=None, timeout=None, **kw):
        req = self._direct.build_request(
            "GET", url, params=params, headers=headers, timeout=timeout)
        return await self.send(req)

    async def post(self, url, *, params=None, content=None, json=None,
                   headers=None, timeout=None, **kw):
        req = self._direct.build_request(
            "POST", url, params=params, content=content, json=json,
            headers=headers, timeout=timeout)
        return await self.send(req)


class _AdaptiveSyncClient(_AdaptiveBase):
    def send(self, request: httpx.Request, *, stream: bool = False, **kwargs):
        host = self._host_of(request)
        use_proxy = bool(host) and host_needs_proxy(host)
        client = self._proxied if use_proxy else self._direct
        try:
            return client.send(request, stream=stream, **kwargs)
        except Exception as exc:
            if use_proxy or stream or not self._is_conn_error(exc):
                raise
            mark_host_proxy(host)
            retry = httpx.Request(
                request.method, str(request.url),
                params=list(request.url.params.multi_items()),
                headers=request.headers.raw,
                content=request.content,
            )
            return self._proxied.send(retry, stream=stream, **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for c in (self._direct, self._proxied):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def build_request(self, *args, **kwargs) -> httpx.Request:
        return self._direct.build_request(*args, **kwargs)

    def get(self, url, *, params=None, headers=None, timeout=None, **kw):
        req = self._direct.build_request("GET", url, params=params,
                                         headers=headers, timeout=timeout)
        return self.send(req)

    def post(self, url, *, params=None, content=None, json=None,
             headers=None, timeout=None, **kw):
        req = self._direct.build_request(
            "POST", url, params=params, content=content, json=json,
            headers=headers, timeout=timeout)
        return self.send(req)


def _strip_send_kw(kw: dict) -> dict:
    send_keys = {"extensions", "timeout"}
    return {k: v for k, v in kw.items() if k not in ("stream",) }


def _send_only_kw(kw: dict) -> dict:
    return {k: v for k, v in kw.items() if k in ("extensions", "timeout")}
