#!/usr/bin/env python3
"""TVBox 配置解析：苹果 CMS 与远程 T4，不在本进程运行爬虫。

这三家（及大量换皮 App）共用同一套配置结构：一个 JSON，核心是 sites[]，
每个站点含 key/name/type/api。我们只导入其中能被本插件直接使用的：

- type 1（JSON）且 api 指向苹果CMS provide/vod 的站点；
- type 0（XML）的苹果CMS站点（接口地址在播放链路上可被识别）。
- type 4 的远程 T4 服务（含网盘，依赖由 T4 服务端承担）。

明确不支持、直接跳过：
- type 3 Spider（csp_*，依赖 Android jar/JS 爬虫运行时）；
- 本地网盘运行时、嗅探解析、直播/电台源；
- 加密且无法解出 JSON 的强混淆订阅。

支持的容器形态：单 JSON、base64 包裹、{"sites":[...]}、
{"urls":[{"url":...}]} 多订阅聚合（会递归拉取子订阅，限深度/数量）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

HTTP_TIMEOUT = 12.0
MAX_SUB = 12
MAX_DEPTH = 2

_JSON_HINT = re.compile(r"provide/vod", re.I)
# 苹果CMS XML 接口通常带 ac=list 或 provide/vod 且无 at/json；放宽以路径判定
_CMS_PATH = re.compile(r"/provide/vod", re.I)
_SKIP_NAME = re.compile(r"(直播|电台|广播|iptv|卫视|央视|体育直播)", re.I)
_PAN_HINT = re.compile(r"(quark|uc\.cn|aliyundrive|alipan|115|baidupcs|pan\.baidu|"
                       r"weiyun|xunlei|cloud189|ctfile|c189)", re.I)
_SPIDER_PREFIX = ("csp_", "csp_App", "py_", "java_", "sp_")

# 内置「饭太硬」TVBox 订阅（一键默认导入）。官方 .cc/tv 是落地页，真正维护的
# 配置是 GitHub 上的 FongMi 加密 XC.json（整段 hex + $#671#$ AES，见 _fongmi_decrypt）。
# github raw 直连受限，按顺序尝试官方与多个国内加速镜像，任一取到即停。
FONGMI_FANTAIYING = {
    "id": "fantaiying",
    "name": "饭太硬（内置·在线+网盘聚合）",
    # 上游相对路径（解析子订阅/全局 spider jar 时作 base）
    "base": "https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json",
    "mirrors": [
        "https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json",
        "https://ghfast.top/https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json",
        "https://gh-proxy.com/https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json",
        "https://ghproxy.net/https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json",
        "https://cdn.jsdelivr.net/gh/yoursmile66/TVBox@main/XC.json",
        # 作者官方备用站 http://www.饭太硬.net/tv —— 返回“图种”（JPEG 后追加
        # 前缀**base64(配置)），fetch_text 已能剥离图片自动解码。GitHub 直连不通时
        # 用它兜底（用 punycode 主机名，避免请求库对中文域名处理不一致）。
        "http://www.xn--sss604efuw.net/tv",
        "https://www.xn--sss604efuw.net/tv",
        # 备用站导航页给出的国内直链镜像（同为图片伪装配置）
        "https://cdn09022024.gitlink.org.cn/api/v1/repos/xxooo/in/raw/in.bmp",
    ],
}


def fantaiying_urls() -> list[str]:
    """内置饭太硬订阅的候选 URL（官方 + 镜像），按优先级返回。"""
    return list(FONGMI_FANTAIYING["mirrors"])



def _b64_try(text: str) -> str | None:
    t = text.strip()
    if not t or any(ch in t for ch in "\n{}[]\""):
        return None
    for candidate in (t, t + "=" * (-len(t) % 4)):
        try:
            raw = base64.b64decode(candidate, validate=True)
            s = raw.decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            continue
        if '"sites"' in s or '"urls"' in s or s.lstrip().startswith("{"):
            return s
    return None


def _strip_jsonc(text: str) -> str:
    """字符串感知地去掉 // 与 /* */ 注释（不破坏 URL 里的 //）。"""
    out: list[str] = []
    i, n = 0, len(text)
    instr = False
    esc = False
    quote = ""
    while i < n:
        c = text[i]
        if instr:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                instr = False
            i += 1
            continue
        if c in ('"', "'"):
            instr = True
            quote = c
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _json_loads_dict(text: str) -> dict | None:
    """json.loads，失败再按 JSONC（去 // 与 /* */ 注释）解析。"""
    if not text or not text.lstrip().startswith(("{", "[")):
        return None
    for candidate in (text, _strip_jsonc(text)):
        try:
            j = json.loads(candidate)
            return j if isinstance(j, dict) else None
        except Exception:
            continue
    return None


def _coerce_site_value(v: str):
    """XML 属性都是字符串，按 TVBox 配置语义还原常见标量类型。"""
    if v is None:
        return ""
    s = v.strip()
    if s in ("0", "1") and len(s) == 1:
        return int(s)
    low = s.lower()
    if low in ("true",):
        return 1
    if low in ("false",):
        return 0
    return s


def parse_xml_config(text: str) -> dict | None:
    """解析 TVBox/影视仓 XML 订阅为与 JSON 配置等价的 dict。

    兼容两种常见形态：
      1. <sites><site key=.. type=.. api=.. searchable=.. /></sites>
      2. <config><sites>...</sites><rules>...</rules></config>
    site 元素的属性映射为站点字段；布尔/数值属性还原为 int。仅做结构转换，
    后续仍走与 JSON 完全相同的 classify_subscription 分类与导入流程。"""
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("<"):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(stripped)
    except Exception:
        return None

    def site_elem_to_dict(elem) -> dict:
        site = {k: _coerce_site_value(v) for k, v in elem.attrib.items()}
        # 元素内文本型子节点（个别 XML 用 <api>..</api> 而非属性）
        for child in elem:
            tag = (child.tag or "").strip().lower()
            if tag and tag not in site and (child.text or "").strip():
                site[tag] = child.text.strip()
        return site

    # 任意层级找 site 元素（根即 site、或包在 sites/config 里都兼容）
    site_elems = [e for e in root.iter() if (e.tag or "").lower() == "site"]
    if not site_elems:
        return None
    sites = [site_elem_to_dict(e) for e in site_elems]

    cfg: dict = {"sites": sites}
    # 全局 spider（jar）：根或 config 节点的 spider 属性
    spider = root.attrib.get("spider")
    if not spider:
        for e in root.iter():
            if (e.tag or "").lower() in ("config", "tvbox", "configuration") \
                    and e.attrib.get("spider"):
                spider = e.attrib["spider"]
                break
    if spider:
        cfg["spider"] = spider
    # 直播/解析等附属 URL（若 XML 提供则保留，缺失也不影响站点导入）
    for xml_key, json_key in (("lives", "lives"), ("parses", "parses"),
                              ("flags", "flags"), ("rules", "rules")):
        node = next((e for e in root.iter() if (e.tag or "").lower() == xml_key), None)
        if node is not None:
            children = list(node)
            if children:
                cfg[json_key] = [
                    {k: _coerce_site_value(v) for k, v in c.attrib.items()}
                    or (c.text.strip() if c.text else "")
                    for c in children]
            elif (node.text or "").strip():
                cfg[json_key] = node.text.strip()
    return cfg


# 伪装成图片（.bmp/.jpg）的订阅：图片字节之后追加 base64 配置。
_B64_ALPHA = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_B64_WS = set(b"\r\n\t ")


def _image_embedded_text(data: bytes) -> str | None:
    """从图片伪装订阅中取出真正的配置文本（已 base64 解码，可能仍含注释）。

    常见结构：JPEG(以 FFD9 结束) / PNG(IEND) 之后，先一小段无意义杂凑（常以
    `**` 之类非 base64 字符收尾），随后是 base64(JSON)。
    """
    tails: list[bytes] = []
    for marker in (b"\xff\xd9", b"IEND\xaeB`\x82"):
        k = data.find(marker)
        if k >= 0:
            tails.append(data[k + len(marker):])
    for tail in tails:
        n = len(tail)
        # 找到第一个“非 base64 且非空白”字符（杂凑与正文的分隔符，如 **）。
        j = 0
        while j < n and (tail[j] in _B64_ALPHA or tail[j] in _B64_WS):
            j += 1
        if j < n:
            k = j
            while k < n and tail[k] not in _B64_ALPHA:
                k += 1
            payload = tail[k:]
        else:
            payload = tail
        s = payload.decode("ascii", "ignore").strip()
        if len(s) < 16:
            continue
        for cand in (s, s + "=" * (-len(s) % 4)):
            try:
                dec = base64.b64decode(cand, validate=False).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                continue
            if '"sites"' in dec or '"urls"' in dec or dec.lstrip().startswith("{"):
                return dec
    return None


def _fongmi_decrypt(text: str) -> str | None:
    """解密 FongMi/OK影视 系「加密订阅」（饭太硬 XC.json 即用此格式）。

    形态：整段为十六进制；hex 解码后以 ``$#<code>#$`` 开头（如 ``$#671#$/``），
    其后是 AES-128-CBC 密文。密钥/IV 不单独下发，而是内嵌：
      - key = ``$#`` 与 ``#$`` 之间的字符串（如 ``671``），右补 ``0`` 到 16 字节；
      - IV  = 明文（hex 串）末 13 个字符，右补 ``0`` 到 16 字节；
      - 密文 = 原始 hex 串中 ``2324`` 标记之后、末 26 个 hex 字符之前的部分。
    解密结果通常是版权/警告注释 + 真正 JSON，从第一个 ``{`` 起解析。
    与开源 @whyun/tv-tools 的 decodeTvboxJson 行为一致。
    """
    try:
        s = (text or "").strip()
        if len(s) < 64 or not re.fullmatch(r"[0-9a-fA-F]+", s):
            return None
        blob = bytes.fromhex(s).decode("utf-8", "ignore").lower()
        if "$#" not in blob or "#$" not in blob:
            return None
        mark = s.find("2324")
        if mark < 0:
            return None

        def _pad16(v: str) -> bytes:
            v = (v or "").strip()[:16]
            return (v + "0" * (16 - len(v))).encode("utf-8")

        key = _pad16(blob[blob.find("$#") + 2: blob.find("#$")])
        iv = _pad16(blob[-13:])
        ct = bytes.fromhex(s[mark + 4: len(s) - 26])
        if not ct:
            return None
        pt: bytes | None = None
        try:  # 优先 cryptography（venv 内置）
            from cryptography.hazmat.primitives.ciphers import (
                Cipher, algorithms, modes)
            from cryptography.hazmat.primitives import padding as _pad
            dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            raw = dec.update(ct) + dec.finalize()
            unp = _pad.PKCS7(128).unpadder()
            pt = unp.update(raw) + unp.finalize()
        except Exception:
            try:  # 兜底：pycryptodome
                from Crypto.Cipher import AES  # type: ignore
                from Crypto.Util.Padding import unpad as _unpad  # type: ignore
                pt = _unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16)
            except Exception:
                return None
        out = pt.decode("utf-8", "replace")
        return out if ('"' in out) else None
    except Exception:
        return None


def parse_config_text(text: str) -> dict | None:
    """把文本解析成配置 dict；支持裸 JSON、JSONC(//注释)、base64 包裹、
    以及 FongMi 加密订阅（hex + AES-128-CBC）。"""
    text = (text or "").strip()
    if not text:
        return None
    cfg = _json_loads_dict(text)
    if cfg is not None:
        return cfg
    # XML 订阅（<sites><site .../></sites>）：在 base64/加密尝试之前先识别，
    # 避免被误判为未知格式，转换为与 JSON 等价的 {sites:[...]} 后走同一导入流程。
    if text.lstrip().startswith("<"):
        xml_cfg = parse_xml_config(text)
        if xml_cfg:
            return xml_cfg
    decoded = _b64_try(text)
    if decoded:
        cfg = _json_loads_dict(decoded)
        if cfg is not None:
            return cfg
    # FongMi 加密订阅（hex 密文）
    dec = _fongmi_decrypt(text)
    if dec:
        i = dec.find("{")
        if i >= 0:
            cfg = _json_loads_dict(dec[i:])
            if cfg is not None:
                return cfg
        cfg = _json_loads_dict(dec)
        if cfg is not None:
            return cfg
    return None


def classify_site(site: dict) -> tuple[str, dict | None]:
    """返回 (status, normalized)。status ∈ add_json/add_xml/skip。"""
    if not isinstance(site, dict):
        return "skip", None
    api = str(site.get("api") or "").strip()
    name = str(site.get("name") or site.get("key") or "").strip()
    t = str(site.get("type", 1))
    searchable = site.get("searchable", 1)
    if not api or not api.startswith(("http://", "https://")):
        return "skip", None
    if t == "4":
        import t4
        return "add_t4", {
            "name": name or site.get("key") or "T4",
            "api": api, "kind": "t4", "site_key": str(site.get("key") or ""),
            "ext": site.get("ext"),
            "enabled": t4.switch(site.get("enabled")),
            "searchable": t4.switch(searchable),
            "filterable": t4.switch(site.get("filterable"), False),
            "indexs": t4.switch(site.get("indexs")),
            "method": "POST" if str(site.get("method", "")).upper() == "POST" else "GET",
            "raw_type": t,
        }
    low = api.lower()
    if t == "3" or api.startswith(_SPIDER_PREFIX) or "csp_" in low:
        return "skip", None
    if _PAN_HINT.search(api) or _PAN_HINT.search(name):
        return "skip", None
    if _SKIP_NAME.search(name):
        return "skip", None
    if t not in ("0", "1"):
        return "skip", None
    if not _CMS_PATH.search(api):
        # 部分配置 api 只给到域名/采集根，尝试补全 provide/vod
        if re.search(r"/api\.php", low, re.I):
            api = api.split("?")[0].rstrip("/")
        else:
            return "skip", None
    is_json = bool(re.search(r"at/json|/json(\?|$)|format=json", api, re.I))
    if is_json:
        norm_api = api
    else:
        # 统一为苹果CMS JSON 接口：/provide/vod[/] → /provide/vod/at/json，
        # 保留原有 query（如采集站 token）。仅支持 XML 的站点会在探测时被剔除。
        m = re.match(r"^(.*?/provide/vod)/?(\?.*)?$", api, re.I)
        if m:
            norm_api = m.group(1).rstrip("/") + "/at/json" + (m.group(2) or "")
        else:
            norm_api = re.sub(r"/at/xml(\b|/)", "/at/json", api)
    return ("add_json" if is_json else "add_xml"), {
        "name": name or api.split("/")[2],
        "api": norm_api,
        "searchable": searchable,
        "raw_type": t,
    }


def extract_sites(config: dict) -> list[dict]:
    sites = config.get("sites")
    return [s for s in sites if isinstance(s, dict)] if isinstance(sites, list) else []


def sub_urls(config: dict, base: str = "") -> list[str]:
    urls = config.get("urls")
    out: list[str] = []
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, dict) and isinstance(u.get("url"), str):
                out.append(urljoin(base, u["url"]))
            elif isinstance(u, str):
                out.append(urljoin(base, u))
    return out


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """拉取订阅正文。

    - 多个 UA 回退：部分源（如饭太硬）只对 App 端 UA(okhttp) 返回真正的伪装
      配置，对浏览器 UA 返回落地网页；
    - 支持图片伪装订阅（.bmp/.jpg，正文是 JPEG 字节后追加的 base64 配置）。
    """
    last_text = ""
    for ua in ("okhttp/3.15", "Mozilla/5.0"):
        try:
            r = await client.get(url, headers={"User-Agent": ua},
                                 timeout=HTTP_TIMEOUT, follow_redirects=True)
            r.raise_for_status()
        except Exception:
            continue
        data = r.content or b""
        # 图片伪装：JPEG/PNG 头
        if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
            embedded = _image_embedded_text(data)
            if embedded and parse_config_text(embedded):
                return embedded
        text = r.text
        if parse_config_text(text):
            return text
        last_text = text
    return last_text


async def load_remote(url: str) -> tuple[list[dict], int]:
    """拉取订阅（含多订阅聚合），返回 (sites, 子订阅数)。"""
    sites, sub, _spider, _base = await load_subscription(url=url)
    return sites, sub


# csp_XxxGuard 加固源：type=3 且 api 以 csp_ 开头、Guard 结尾
_GUARD_API = re.compile(r"^csp_.+Guard$", re.I)
# 风险/重资源源：即便能跑也默认关闭，避免拖慢聚合或在缺登录态时报错
_RISK_NAME = re.compile(
    r"(P2P|磁力|磁鏈|云盘|雲盤|网盘|網盤|夸克|誇克|阿里|alipan|aliyun|115|"
    r"百度|baiduwp|UC网盘|迅雷|thunder|我的|私人|收藏)", re.I)
_PAN_API = re.compile(r"(quark|uc\.cn|aliyun|alipan|aliyundrive|115|baidu|"
                      r"weiyun|xunlei|cloud189|pan\.)", re.I)


def is_guard_site(site: dict) -> bool:
    """type=3 且 api=csp_*Guard（本地 Android 加固运行时可执行）。"""
    if not isinstance(site, dict):
        return False
    api = str(site.get("api") or "").strip()
    return str(site.get("type", 1)) == "3" and bool(_GUARD_API.match(api))


def guard_is_risky(site: dict) -> bool:
    """风险加固源：P2P/磁力/云盘/私人，或需要 dict 型外部配置（云盘登录态等）。"""
    if not isinstance(site, dict):
        return False
    if isinstance(site.get("ext"), dict):
        return True
    name = str(site.get("name") or "")
    api = str(site.get("api") or "")
    return bool(_RISK_NAME.search(name) or _PAN_API.search(api))


def _spider_jar_url(spider: str, base: str = "") -> str:
    """解析 TVBox 全局 spider 声明里的 jar 地址。

    形如 "./jar/top98_1.jar;md5;xxxx" 或纯 URL，取分号前的 URL 并用订阅基址
    补全相对地址。
    """
    s = str(spider or "").strip().split(";", 1)[0].strip()
    if not s:
        return ""
    return urljoin(base, s)


def is_drpy_site(site: dict) -> bool:
    """type=3 且 api 指向远程 drpyS 引擎（http 的 drpy2.min.js）。

    站点规则在 ext（也是 http URL）。区别于本地内置 drpy://。
    """
    if not isinstance(site, dict) or str(site.get("type", 1)) != "3":
        return False
    api = str(site.get("api") or "")
    low = api.lower()
    return api.startswith(("http://", "https://")) and (
        ".js" in low or "dr_py" in low or "drpy" in low)


def _is_relative_js(v: str) -> bool:
    """相对路径的 drpy 资源引用（引擎或规则文件）。

    兼容 ./lib/drpy2.min.js 这类 JS 引擎，以及 drpy2 规则常用的 .js/.txt/.json
    后缀（如 ./js/lf_live.txt、./js/alist.json）。仅认无协议、无协议相对前缀
    （//host）、且末段带文件扩展名的相对路径，从而排除 csp_ 类名等非文件引用。
    """
    s = str(v or "").strip()
    if not s or "://" in s or s.startswith("//"):
        return False
    name = s.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return "." in name


def absolutize_drpy_site(site: dict, base: str) -> dict:
    """把 type=3 相对路径 drpy 站点的引擎(api)与规则(ext)补全为绝对 URL。

    GitHub 系订阅（如高天流云 js.json）常用 "./lib/drpy2.min.js" 引擎 +
    "./js/规则.js"（亦可为 .txt/.json 规则）的相对写法；补全后即可被
    is_drpy_site 识别并由 drpy.import_remote 经 subfetch 下载，全程 Node
    执行、不依赖安卓运行时。仅当 api 是相对 JS 引擎时处理，ext 为相对规则
    文件时一并补全；已是绝对 URL、无 base 或非该形态时原样返回。
    """
    if not isinstance(site, dict) or not base:
        return site
    if str(site.get("type", 1)) != "3":
        return site
    api = str(site.get("api") or "").strip()
    if not (_is_relative_js(api) and api.split("?", 1)[0].lower().endswith(".js")):
        return site
    site["api"] = urljoin(base, api)
    ext = site.get("ext")
    if isinstance(ext, str) and _is_relative_js(ext):
        site["ext"] = urljoin(base, ext.strip())
    return site


# 依赖 ARM 原生库（加固 so 经 ndk_translation 反调试自检会 SIGABRT）的
# FongMi/Yoursmile 爬虫类：在 x86_64 redroid 上一调用即杀进程，必须在导入/调度前
# 屏蔽，避免打崩整个 bridge（连累其它正常源）。依据是 jar 内 native so 的 JNI 导出
# 符号 + dex 继承闭包（en.NetPan/en.BaseApi 派生族）。网盘类尤其在此。
_FONGMI_NATIVE_CLASSES = {
    # libstub 直接导出 native 方法的类
    "Wogg", "kongbai", "Jpys", "GuanYing",
    "TgYunDouBanPan", "TgYunPanLocal",
    # en.NetPan / en.BaseApi 派生族（dex 继承闭包，含网盘与网盘聚合）
    "Bili", "ConfigCenter", "Douyu", "Emby", "FourKZN", "GuaZi",
    "HuaJuan", "Huya", "Ikanbot", "PanSearch", "PanSou", "PanSouFish",
    "PanSouQuark", "PanTa", "Push", "Qiwei", "SeedHub", "Star2",
    "WoNiu", "Youtube", "光鸭社区", "网盘集合",
}


def _is_fongmi_native_class(cls: str) -> bool:
    """去 csp_ 前缀后的类名是否命中饭太硬 native 崩溃黑名单。"""
    return (cls or "").strip() in _FONGMI_NATIVE_CLASSES


def classify_subscription(sites: list[dict], spider: str = "",
                          base: str = "") -> dict:
    """把一份订阅的原始 sites 一次分成五类。

    返回：
      cms    : 苹果CMS 归一化结果（name/api/transport...）
      guard  : csp_*Guard 加固源原始 site（交 guard 模块，redroid 跑固定 guard.jar）
      csp    : 普通 csp_ 爬虫（非 Guard，纯 dex），{key,name,cls,ext,jar,...}，
               由安卓运行时按各自 jar 加载
      drpy   : 远程 drpyS（type=3、api 为 http 的 drpy2 js），{name,api,ext}，
               由 Node sidecar 直接跑远程脚本
      skipped: [{name,reason}]
    """
    cms: list[dict] = []
    guard: list[dict] = []
    csp: list[dict] = []
    drpy: list[dict] = []
    t4_sites: list[dict] = []
    seen_t4: set[str] = set()
    skipped: list[dict] = []
    seen_cms: set[str] = set()
    seen_guard: set[str] = set()
    seen_csp: set[str] = set()
    seen_drpy: set[str] = set()
    count: dict[str, int] = {}
    skip_seen: set[str] = set()
    jar_url = _spider_jar_url(spider, base)

    def _skip(name: str, reason: str):
        name = (name or "未命名")[:24]
        tag = name + "|" + reason
        if tag not in skip_seen:
            skip_seen.add(tag)
            skipped.append({"name": name, "reason": reason})

    for s in sites:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or s.get("key") or s.get("api") or "").strip()
        api = str(s.get("api") or "").strip()
        t = str(s.get("type", 1))
        if t == "4":
            _status, normalized = classify_site(s)
            if not normalized:
                _skip(name, "T4 接口地址非 http(s)")
                continue
            import t4
            identity = t4.source_id(normalized["api"], normalized.get("ext"))
            if identity not in seen_t4:
                seen_t4.add(identity)
                t4_sites.append(normalized)
            continue

        # 1) 加固爬虫 csp_*Guard
        if is_guard_site(s):
            gk = api.lower()
            if gk in seen_guard:
                continue
            seen_guard.add(gk)
            guard.append(s)
            continue

        # 2) 远程 drpyS（api 是 http 的 drpy2 js，规则在 ext）
        if is_drpy_site(s):
            dk = (api + "|" + str(s.get("ext") or "")).lower()
            if dk in seen_drpy:
                continue
            seen_drpy.add(dk)
            drpy.append({
                "name": name or api.split("/")[-1] or "drpy",
                "engine": api,
                "ext": str(s.get("ext") or "").strip(),
                "searchable": s.get("searchable", 1),
                "quickSearch": s.get("quickSearch", 0),
            })
            continue

        # 3) type=3 其它爬虫细分
        if t == "3" or api.startswith(_SPIDER_PREFIX) or "csp_" in api.lower():
            low = api.lower()
            if api.startswith("csp_") and not api.endswith("Guard"):
                base_key = api[4:]                    # 去 csp_ 前缀
                # 饭太硬加固族（Yoursmile/libstub 与伪装 PNG jar 的 *Amns）在 x86_64
                # redroid + ndk_translation 下一调用即 SIGABRT 杀进程，导入前直接屏蔽，
                # 避免打崩 bridge 连累其它源。网盘类基本都落在这里。
                if base_key.endswith("Amns") or _is_fongmi_native_class(base_key):
                    _skip(name, "加固原生源（x86 模拟器会崩溃，已屏蔽）")
                    continue
                # 普通纯 dex 爬虫：类名即 csp_Xxx，由订阅全局 spider jar 或站点自带 jar
                # 承载。同一个 Spider 类可被多个站点以不同 ext 复用（如 csp_XBPQ），
                # 唯一性按 (类, ext, jar) 判定；同类多配置追加序号，等同 guard 规则。
                ext = s.get("ext")
                ext_str = ext if isinstance(ext, str) else (
                    "" if ext is None else json.dumps(ext, ensure_ascii=False))
                site_jar = str(s.get("_site_jar") or s.get("_spider_jar") or "").strip() \
                    or jar_url
                sig = (api.lower(), ext_str.strip().lower(), site_jar)
                if sig in seen_csp:
                    continue
                seen_csp.add(sig)
                count[base_key] = count.get(base_key, 0) + 1
                uniq = base_key if count[base_key] == 1 else f"{base_key}{count[base_key]}"
                csp.append({
                    "key": uniq,
                    "spider": api,
                    "name": name or api,
                    "cls": "com.github.catvod.spider." + base_key,
                    "ext": ext_str,
                    "jar": site_jar,
                    "searchable": s.get("searchable", 1),
                    "quickSearch": s.get("quickSearch", 0),
                })
                continue
            if api.startswith(("py_", "java_", "sp_")):
                _skip(name, "Python/Java 内置脚本源（不支持）")
                continue
            _skip(name, "其它爬虫源（无法识别运行时）")
            continue
        if _PAN_HINT.search(api) or _PAN_API.search(api) or _PAN_HINT.search(name):
            _skip(name, "网盘/云盘源")
            continue
        if _SKIP_NAME.search(name):
            _skip(name, "直播/电台源")
            continue
        if not api.startswith(("http://", "https://")):
            _skip(name, "接口地址非 http(s)")
            continue
        if t not in ("0", "1"):
            _skip(name, f"不支持的类型 type={t}")
            continue

        # 4) 苹果 CMS（复用归一化）
        status, norm = classify_site(s)
        if not norm:
            _skip(name or api, "非标准苹果CMS接口")
            continue
        key = norm["api"].rstrip("/").lower()
        if key in seen_cms:
            continue
        seen_cms.add(key)
        norm["transport"] = "json" if status == "add_json" else "xml"
        cms.append(norm)

    return {"cms": cms, "t4": t4_sites, "guard": guard, "csp": csp, "drpy": drpy,
            "skipped": skipped, "jar_url": jar_url}


async def load_subscription_full(url: str | None = None,
                                 text: str | None = None):
    """同 load_subscription，额外返回第 5 项 rules（顶层去广告规则列表）。"""
    seen: set[str] = set()
    all_sites: list[dict] = []
    spider = ""
    root_base = (url or "").strip() if url else ""
    rules: list = []

    async def walk(u: str | None, raw: str | None, depth: int):
        nonlocal spider
        if u is not None:
            if depth > MAX_DEPTH or u in seen or len(seen) >= MAX_SUB:
                return
            seen.add(u)
            try:
                import subfetch
                async with subfetch.async_sub_client() as client:
                    raw = await fetch_text(client, u)
            except Exception:
                return
        cfg = parse_config_text(raw or "")
        if not cfg:
            return
        sub_spider = cfg.get("spider") if isinstance(cfg.get("spider"), str) else ""
        if not spider and (depth == 0) and sub_spider:
            spider = sub_spider
        if depth == 0 and isinstance(cfg.get("rules"), list):
            rules.extend(cfg["rules"])
        sub_base = u or ""
        sub_jar = _spider_jar_url(sub_spider, sub_base)
        for st in extract_sites(cfg):
            if isinstance(st, dict):
                if str(st.get("type")) == "4" and sub_base and st.get("api"):
                    st["api"] = urljoin(sub_base, str(st.get("api") or ""))
                # type=3 相对路径 drpy JS：用当前订阅地址补全引擎与规则，
                # 否则下游 is_drpy_site 只认 http 会把它误归为“无法识别运行时”。
                if sub_base:
                    absolutize_drpy_site(st, sub_base)
                if sub_jar and not st.get("_spider_jar"):
                    st["_spider_jar"] = sub_jar
                # 站点自带 jar（饭太硬部分源用伪装成 PNG 的独立 jar），优先级高于全局。
                # 形如 "https://h/p.png;md5;xxxx"，去掉 ;md5; 后缀并补全相对地址。
                sj = st.get("jar")
                if isinstance(sj, str) and sj.strip():
                    sj_url = sj.strip().split(";", 1)[0].strip()
                    if sj_url and "://" not in sj_url and sub_base:
                        sj_url = urljoin(sub_base.rstrip("/") + "/", sj_url.lstrip("/"))
                    if sj_url:
                        st["_site_jar"] = sj_url
            all_sites.append(st)
        if depth < MAX_DEPTH:
            base = u or ""
            for sub in sub_urls(cfg, base)[:MAX_SUB]:
                await walk(sub, None, depth + 1)

    await walk(root_base or None, text, 0)
    return all_sites, max(0, len(seen) - 1), spider, root_base, rules


async def load_subscription(url: str | None = None,
                            text: str | None = None) -> tuple[list, int, str, str]:
    """拉取/解析订阅，支持裸 JSON、base64、多订阅聚合（向后兼容 4 元组）。"""
    sites, subs, spider, base, _rules = await load_subscription_full(url=url,
                                                                     text=text)
    return sites, subs, spider, base


async def load_fantaiying():
    """加载内置「饭太硬」订阅：按官方 + 镜像顺序尝试，首个成功即用。

    返回 (sites, spider, base, used_url)；全部失败抛最后一个异常。
    """
    last: Exception | None = None
    for u in fantaiying_urls():
        try:
            sites, _subs, spider, base, _rules = await load_subscription_full(url=u)
            if sites:
                return sites, spider, base, u
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"内置饭太硬订阅所有镜像均不可达：{last}")


# 内置「轻量猫源」：随包的纯苹果CMS(type=1)订阅，无 jar/爬虫/网盘，零运行时。
# 与饭太硬（重爬虫、依赖安卓运行时）互补，低配机器/lite 包也能直接用。
_CAT_LITE_FILE = Path(__file__).resolve().parent / "assets" / "cat_lite.json"


def load_cat_lite_sync() -> tuple[list, str, str, str]:
    """同步读取随包轻量猫源配置。返回 (sites, spider, base, used)。"""
    cfg = json.loads(_CAT_LITE_FILE.read_text(encoding="utf-8"))
    sites = [s for s in (cfg.get("sites") or []) if isinstance(s, dict)]
    return sites, str(cfg.get("spider") or ""), "", "builtin:cat_lite"


async def load_cat_lite():
    """加载内置「轻量猫源」（本地文件，不走网络，不依赖任何运行时）。

    返回 (sites, spider, base, used_url)。
    """
    return await asyncio.to_thread(load_cat_lite_sync)

