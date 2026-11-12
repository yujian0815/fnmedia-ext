"""应用自更新：后台检查 GitHub Release、下载 fpk 校验后热替换升级。

设计要点（安全/可逆优先）：
- 只读 GitHub 官方 API（releases/latest 与 releases/tags），不接受第三方地址；
- 资产仅在本仓库自己的 Release 附件中按文件名规则挑选（轻量版/全功能版）；
- 下载后强制 sha256 校验（Release 必须带同名 .sha256 附件），不符则拒绝安装；
- 安装走 systemd-run 瞬态系统单元做自包含热替换（独立于本服务 cgroup，
  停 fnmedia-ext 不会杀掉它）：停服→备份 state→解包 rsync 覆盖代码（保留 .venv）
  →恢复 state→install.sh 渲染单元并重启；与 fpk 的 upgrade 生命周期等价；
- 升级是覆盖安装而非先卸载（保留 state/.venv）；不依赖应用中心 wizard RPC。
- 全程只在管理页显式点击“立即更新”时触发；后台只做只读检查与提示，绝不自动安装。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

try:
    import features
except Exception:  # pragma: no cover
    features = None

REPO_SLUG = "yujian0815/fnmedia-ext"
API_BASE = f"https://api.github.com/repos/{REPO_SLUG}/releases"
# 检查结果缓存时长（秒）：避免频繁打 GitHub，也让管理页打开即时。
CHECK_TTL = 30 * 60
# 后台启动后首次检查延迟 + 周期间隔。
BOOT_DELAY = 20
PERIOD = 6 * 3600
# 下载总时长上限（秒）。
DOWNLOAD_TIMEOUT = 180

_LOCK = threading.Lock()
_STATE: dict = {
    "last_check": 0.0,
    "last_error": "",
    "info": None,          # 最近一次 release 信息
    "status": "idle",      # idle | checking | downloading | installing | done | error
    "progress": "",        # 人类可读的当前步骤
    "installed_path": "",  # 已落盘待/正在安装的 fpk
    "updated_version": "",
    "finished_at": 0.0,
}

_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = Path("/tmp/fnmedia-update/state.json")


def _persist() -> None:
    """把进度落盘：安装会重启服务，重启后管理页仍能读到“已完成/失败”。"""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        snap = {k: _STATE.get(k) for k in (
            "last_check", "last_error", "info", "status", "progress",
            "installed_path", "updated_version", "finished_at")}
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _restore() -> None:
    try:
        if _STATE_FILE.exists():
            snap = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            # 重启后若停在“下载/安装中”，说明安装已接管，归一成完成态
            if snap.get("status") in ("downloading", "installing"):
                snap["status"] = "done"
                snap["progress"] = "安装完成，服务已重启"
                snap["finished_at"] = time.time()
            _STATE.update(snap)
    except Exception:  # noqa: BLE001
        pass


_restore()



# --------------------------------------------------------------- 版本

def current_version() -> str:
    """读取打包/仓库根目录 VERSION；读不到返回 '0.0.0'。"""
    for cand in (_ROOT / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            v = cand.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return "0.0.0"


def _vtuple(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in (nums + [0, 0, 0])[:3])


def is_newer(latest: str, current: str | None = None) -> bool:
    return _vtuple(latest) > _vtuple(current or current_version())


def edition_full() -> bool:
    """当前运行副本是否为全功能版（决定默认推荐下载哪个 fpk）。"""
    try:
        return bool(getattr(features, "DDUO", False) or getattr(features, "GUARD", False))
    except Exception:
        return False


# --------------------------------------------------------------- GitHub

def _pick_asset(assets: list[dict], full: bool) -> dict | None:
    """在 Release 附件里挑目标安装包。

    全功能版：fnmedia-ext-<ver>-full.fpk
    轻量版：fnmedia-ext-<ver>.fpk（排除 -full / -lite / -public），
            找不到则退回 -lite.fpk。
    """
    lite_plain = lite_alt = full_pkg = None
    for a in assets:
        name = a.get("name") or ""
        if not name.endswith(".fpk"):
            continue
        if re.search(r"-full\.fpk$", name):
            full_pkg = a
        elif re.search(r"-(lite|public)\.fpk$", name):
            lite_alt = a
        elif re.match(r"^fnmedia-ext-.+\.fpk$", name):
            lite_plain = a
    if full:
        return full_pkg
    return lite_plain or lite_alt


def _version_key(tag: str) -> tuple:
    """Release tag → 可比较版本元组（取数字段，兼容 1.0.1-beta.1 这类预发布号）。"""
    nums = re.findall(r"\d+", str(tag or ""))
    return tuple(int(x) for x in (nums + [0, 0, 0])[:3])


def _fetch_release_sync() -> dict | None:
    """拉取最新 Release（同步，固定在工作线程执行，绝不阻塞事件循环）。

    注意不能只用 /releases/latest：GitHub 的 latest 端点会跳过 prerelease，
    导致公开测试版（如 1.0.1-beta.1）永远检测不到。这里拉 Release 列表，
    取非草稿中版本号最高的一个（含预发布）。同时复用预留的更新/订阅代理通道，
    国内直连 api.github.com 不稳定时可经代理获取。
    """
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "fnmedia-ext-updater"}
    last_err = ""
    for proxy in _proxy_candidates():
        try:
            kwargs = dict(timeout=httpx.Timeout(20, connect=12),
                          follow_redirects=True, trust_env=False,
                          headers=headers)
            if proxy:
                kwargs["proxy"] = proxy
            with httpx.Client(**kwargs) as client:
                r = client.get(f"{API_BASE}?per_page=20")
                if r.status_code != 200:
                    raise RuntimeError(f"GitHub 返回 {r.status_code}")
                releases = r.json()
            # 过滤草稿，按版本号取最高（含 prerelease）；同版本优先正式版
            candidates = [x for x in releases
                          if isinstance(x, dict) and not x.get("draft")]
            if not candidates:
                raise RuntimeError("Release 列表为空")
            candidates.sort(
                key=lambda x: (_version_key(x.get("tag_name", "")),
                               0 if x.get("prerelease") else 1),
                reverse=True)
            return candidates[0]
        except Exception as exc:  # noqa: BLE001
            last_err = f"{'代理' if proxy else '直连'}：" \
                       f"{type(exc).__name__} {str(exc)[:80]}"
            continue
    raise RuntimeError("检查更新的所有网络通道均失败：" + last_err)


def _normalize(release: dict, full: bool) -> dict:
    tag = (release.get("tag_name") or "").lstrip("v")
    assets = release.get("assets") or []
    pkg = _pick_asset(assets, full)
    sum_url = ""
    if pkg:
        want = pkg.get("name") + ".sha256"
        for a in assets:
            if a.get("name") == want:
                sum_url = a.get("browser_download_url") or ""
                break
    return {
        "version": tag,
        "name": release.get("name") or tag,
        "notes": (release.get("body") or "").strip(),
        "published_at": release.get("published_at") or "",
        "html_url": release.get("html_url") or "",
        "edition": "full" if full else "lite",
        "asset": (pkg.get("browser_download_url") if pkg else ""),
        "asset_name": (pkg.get("name") if pkg else ""),
        "asset_size": (pkg.get("size") if pkg else 0),
        "sum_url": sum_url,
        "has_package": bool(pkg),
    }


def check_sync(force: bool = False) -> dict:
    """检查更新（同步、带缓存）。仅在工作线程执行，避免网络阻塞事件循环。"""
    now = time.time()
    with _LOCK:
        if (not force and _STATE["info"]
                and now - _STATE["last_check"] < CHECK_TTL):
            return _snapshot()
        if _STATE["status"] in ("checking", "downloading", "installing"):
            return _snapshot()
        _STATE["status"] = "checking"
        _STATE["progress"] = "正在检查 GitHub 更新…"

    full = edition_full()
    try:
        release = _fetch_release_sync()
        if not release or release.get("draft"):
            raise RuntimeError("未获取到有效 Release")
        info = _normalize(release, full)
        cur = current_version()
        info["current_version"] = cur
        info["update_available"] = is_newer(info["version"], cur)
        with _LOCK:
            _STATE.update(info=info, last_check=now, last_error="",
                          status="idle", progress="")
        _persist()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE.update(last_check=now, last_error=str(exc),
                          status="error", progress="检查更新失败")
        _persist()
    return _view()


async def check(force: bool = False) -> dict:
    """异步入口：把同步网络检查丢到工作线程，绝不占用事件循环。"""
    return await asyncio.to_thread(check_sync, force)


def _snapshot() -> dict:
    """组装状态视图（不加锁；调用方需自行确保不在写竞争中或已持锁）。"""
    info = dict(_STATE["info"]) if _STATE["info"] else None
    return {
        "code": 0,
        "current_version": current_version(),
        "edition": "full" if edition_full() else "lite",
        "status": _STATE["status"],
        "progress": _STATE["progress"],
        "last_check": _STATE["last_check"],
        "last_error": _STATE["last_error"],
        "update": info,
        "updated_version": _STATE["updated_version"],
        "finished_at": _STATE["finished_at"],
    }


def _view() -> dict:
    with _LOCK:
        return _snapshot()


def status() -> dict:
    return _view()


# --------------------------------------------------------------- 下载 / 安装

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def start_update() -> dict:
    """触发一次“下载校验 → 系统安装覆盖”的升级（仅管理页显式调用）。"""
    with _LOCK:
        if _STATE["status"] in ("checking", "downloading", "installing"):
            return {"code": 409, "msg": "已有更新任务在进行中", "data": _snapshot()}
        info = dict(_STATE["info"]) if _STATE["info"] else None
        if not info or not info.get("update_available"):
            return {"code": 400, "msg": "当前没有可安装的更新", "data": _snapshot()}
        if not info.get("asset"):
            return {"code": 400, "msg": "该版本未提供对应安装包", "data": _snapshot()}
        # 自更新走 systemd-run 瞬态单元热替换，无需应用中心 CLI。
        if os.geteuid() != 0:
            return {"code": 500, "msg": "自更新需要以 root 运行（应用特权未就绪）",
                    "data": _snapshot()}
        _STATE["status"] = "downloading"
        _STATE["progress"] = "正在下载安装包…"
    _persist()

    asyncio.get_event_loop().run_in_executor(
        None, lambda: _blocking_install(info, None))
    return {"code": 0, "msg": "已开始下载并安装更新", "data": _view()}


def _proxy_candidates() -> list[str | None]:
    """更新下载的通道候选，按优先级：update_proxy、subscription_proxy、直连。

    服务以干净 systemd 环境运行（无 *_proxy），国内直连 GitHub Release 附件
    CDN 常握手超时，因此允许用户在管理页为“更新”单独配置代理。
    """
    cands: list[str | None] = []
    if _config is not None:
        try:
            cfg = _config.config.get()
            for key in ("update_proxy", "subscription_proxy"):
                v = (cfg.get(key) or "").strip()
                if v and v not in cands:
                    cands.append(v)
        except Exception:  # noqa: BLE001
            pass
    cands.append(None)  # 直连兜底
    return cands


def _download_with_fallbacks(url: str, dest: Path, total_hint: int = 0) -> None:
    """依次尝试各代理通道流式下载到 dest；任一通道完整成功即返回。"""
    last_err = ""
    for i, proxy in enumerate(_proxy_candidates()):
        tag = "代理" if proxy else "直连"
        try:
            kwargs = dict(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=15),
                          follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "fnmedia-ext-updater"})
            if proxy:
                kwargs["proxy"] = proxy
            with httpx.Client(**kwargs) as cli:
                with cli.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    total = int(resp.headers.get("content-length") or total_hint or 0)
                    got = 0
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with tmp.open("wb") as f:
                        for chunk in resp.iter_bytes(1 << 20):
                            f.write(chunk)
                            got += len(chunk)
                            if total:
                                pct = got * 100 // total
                                with _LOCK:
                                    _STATE["progress"] = f"下载中 {pct}%（{tag}）"
                    # 大小校验（有 content-length 时）
                    if total and got != total:
                        raise RuntimeError(f"下载不完整 {got}/{total}")
                    tmp.replace(dest)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = f"{tag}：{type(exc).__name__} {str(exc)[:80]}"
            try:
                dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            continue
    raise RuntimeError("所有下载通道均失败：" + last_err)


def _get_text_with_fallbacks(url: str) -> str:
    last_err = ""
    for proxy in _proxy_candidates():
        try:
            kwargs = dict(timeout=httpx.Timeout(30, connect=12),
                          follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "fnmedia-ext-updater"})
            if proxy:
                kwargs["proxy"] = proxy
            with httpx.Client(**kwargs) as cli:
                r = cli.get(url)
                r.raise_for_status()
                return r.text
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
    raise RuntimeError("下载校验文件失败：" + last_err)


def _live_root() -> Path:
    """当前运行的应用根目录（repo/）。"""
    return Path(__file__).resolve().parent.parent


def _write_upgrade_script(pkg: Path, target_version: str) -> Path:
    """生成独立热替换脚本。由 systemd-run 瞬态单元以 root 执行。"""
    root = _live_root()
    work = Path("/tmp/fnmedia-update")
    script = work / "apply-update.sh"
    marker = work / "apply-result"
    extract = work / ("extract-" + target_version.replace(".", "_"))
    pkg_q = str(pkg)
    extract_q = str(extract)
    root_q = str(root)
    marker_q = str(marker)
    body = f"""#!/usr/bin/env bash
# fnmedia-ext 自更新热替换（瞬态系统单元执行，与 fpk upgrade 生命周期等价）。
set -uo pipefail
exec >/tmp/fnmedia-update/apply.log 2>&1
echo "[upd] start $(date -Is) target={target_version}"
PKG={pkg_q!r}; EXTRACT={extract_q!r}; ROOT={root_q!r}; MARK={marker_q!r}
fail(){{ echo "[upd] FAIL: $*"; echo "FAIL $*" > "$MARK"; exit 1; }}
rm -rf "$EXTRACT"; mkdir -p "$EXTRACT"
# fpk 外层为 tar.gz，内层 app.tgz 内是 repo/
tar xzf "$PKG" -C "$EXTRACT" app.tgz || fail "解开 fpk 失败"
tar xzf "$EXTRACT/app.tgz" -C "$EXTRACT" || fail "解开 app.tgz 失败"
[ -f "$EXTRACT/repo/deploy/install.sh" ] || fail "包内缺少 repo/deploy/install.sh"
[ -f "$EXTRACT/repo/VERSION" ] || fail "包内缺少 VERSION"
NEWVER="$(cat "$EXTRACT/repo/VERSION" 2>/dev/null | tr -d '[:space:]')"
echo "[upd] new version=$NEWVER"
# 备份运行态：state + 用户自行导入/产生的本地资产（guard 源、spider jar、
# 本地配置等）。这些不随安装包分发，但热替换必须原样保留。
BK=/tmp/fnmedia-update/state-backup.tar.gz
rm -f "$BK" /tmp/fnmedia-update/localjson-backup.tar.gz
TARARGS=()
[ -d "$ROOT/state" ] && TARARGS+=(state)
[ -d "$ROOT/proxy/assets/guard" ] && TARARGS+=(proxy/assets/guard)
[ -d "$ROOT/proxy/assets/spiders" ] && TARARGS+=(proxy/assets/spiders)
[ -d "$ROOT/proxy/assets/catvod" ] && TARARGS+=(proxy/assets/catvod)
[ -d "$ROOT/proxy/assets/drpy" ] && TARARGS+=(proxy/assets/drpy)
if [ "${{#TARARGS[@]}}" -gt 0 ]; then
  tar --ignore-failed-read -czf "$BK" -C "$ROOT" "${{TARARGS[@]}}" || true
fi
( cd "$ROOT" 2>/dev/null && find proxy -name '*local.json' -type f 2>/dev/null \
  | tar --ignore-failed-read -czf /tmp/fnmedia-update/localjson-backup.tar.gz -T - ) || true
# 停服（supervise 自动还原官方 socket）
systemctl stop fnmedia-ext.service || true
sleep 2
# 覆盖代码：绝不使用 --delete（避免删掉用户自导入源/jar）。仅把包内文件叠加
# 覆盖到 ROOT，并显式排除全部用户运行数据；这些目录 live 上有用户文件、包里
# 只有 .gitkeep 占位，叠加不会触碰它们。
RSYNC_EX=(
  --exclude='.venv/' --exclude='state/' --exclude='__pycache__/'
  --exclude='*.pyc' --exclude='proxy/assets/guard/'
  --exclude='proxy/assets/spiders/' --exclude='proxy/assets/catvod/'
  --exclude='proxy/assets/drpy/' --exclude='*local.json'
)
if command -v rsync >/dev/null 2>&1; then
  rsync -a "${{RSYNC_EX[@]}}" "$EXTRACT/repo/" "$ROOT/" || fail "rsync 覆盖代码失败"
else
  ( cd "$EXTRACT/repo" && tar --exclude='.venv' --exclude='state' \
      --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='proxy/assets/guard' --exclude='proxy/assets/spiders' \
      --exclude='proxy/assets/catvod' --exclude='proxy/assets/drpy' \
      --exclude='*local.json' -cf - . ) | ( cd "$ROOT" && tar -xf - ) \
      || fail "tar 覆盖代码失败"
fi
# 恢复运行态（state / 本地资产 / local.json）
[ -f "$BK" ] && tar xzf "$BK" -C "$ROOT" || true
[ -f /tmp/fnmedia-update/localjson-backup.tar.gz ] && tar xzf /tmp/fnmedia-update/localjson-backup.tar.gz -C "$ROOT" || true
# 仅清理字节码缓存（不碰任何数据文件）
find "$ROOT/proxy" -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
echo "[upd] code replaced; installed VERSION=$(cat "$ROOT/VERSION" 2>/dev/null)"
# 渲染 systemd 单元并重启（install.sh 内部会 daemon-reload/enable/restart）
bash "$ROOT/deploy/install.sh" || fail "install.sh 启动失败"
sleep 3
if systemctl is-active --quiet fnmedia-ext.service; then
  echo "[upd] OK service active; VERSION=$(cat "$ROOT/VERSION")"
  echo "OK $NEWVER" > "$MARK"
else
  fail "服务未正常启动"
fi
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _launch_transient_updater(pkg: Path, target_version: str) -> None:
    """用 systemd-run 启动独立瞬态单元执行热替换；单元独立于本服务 cgroup。"""
    script = _write_upgrade_script(pkg, target_version)
    # 先清结果标记
    try:
        (Path("/tmp/fnmedia-update") / "apply-result").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    unit = "fnmedia-ext-auto-update.service"
    if shutil.which("systemd-run"):
        cmd = ["systemd-run", "--unit", unit, "--collect",
               "--description", "fnmedia-ext self update",
               "/bin/bash", str(script)]
    else:
        # 兜底：setsid 完全脱离（无 systemd-run 的环境）
        cmd = ["setsid", "/bin/bash", str(script)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    # 给瞬态单元一点启动时间
    time.sleep(1.5)


def _blocking_install(info: dict, binp: str) -> None:
    tmp_dir = Path("/tmp/fnmedia-update")
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pkg_name = info["asset_name"] or "fnmedia-ext.fpk"
        pkg_path = tmp_dir / pkg_name

        with _LOCK:
            _STATE["status"] = "downloading"
            _STATE["progress"] = "正在下载安装包…"
        _persist()

        # 多通道下载（更新代理 → 订阅代理 → 直连），自动回退。
        _download_with_fallbacks(
            info["asset"], pkg_path, int(info.get("asset_size") or 0))

        # sha256 强制校验：Release 必须带 .sha256 附件。
        if info.get("sum_url"):
            expect = _get_text_with_fallbacks(info["sum_url"]).strip().split()[0].lower()
            actual = _sha256(pkg_path)
            if not expect or not re.fullmatch(r"[0-9a-f]{64}", expect):
                raise RuntimeError("校验文件格式异常，已中止")
            if expect != actual:
                raise RuntimeError("sha256 校验不一致，已中止安装")
        else:
            raise RuntimeError("Release 缺少 sha256 校验附件，已中止")

        with _LOCK:
            _STATE["status"] = "installing"
            _STATE["progress"] = "正在应用更新（服务将短暂重启）…"
            _STATE["installed_path"] = str(pkg_path)
        _persist()

        # 走自包含的瞬态系统单元完成热替换（systemd-run，独立于本服务的 cgroup，
        # 停 fnmedia-ext 不会连带杀掉它）。它与 fpk 的 upgrade_init/callback 等价：
        # 停服→备份 state→解包校验→rsync 覆盖 repo 代码（保留 .venv/state）→
        # 恢复 state→install.sh 渲染并重启服务。不依赖应用中心 wizard RPC。
        _launch_transient_updater(pkg_path, info["version"])

        with _LOCK:
            _STATE.update(status="done",
                          progress="安装完成，服务正在重启…",
                          updated_version=info["version"],
                          finished_at=time.time())
        _persist()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE.update(status="error",
                          progress=f"更新失败：{exc}",
                          last_error=str(exc))
        _persist()


# --------------------------------------------------------------- 后台周期检查

async def periodic_loop(stop_event: asyncio.Event) -> None:
    """启动后延迟首检，之后每 PERIOD 秒只读检查一次（绝不自动安装）。"""
    try:
        await asyncio.wait_for(
            asyncio.shield(stop_event.wait()), timeout=BOOT_DELAY)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(check_sync, False)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()), timeout=PERIOD)
        except asyncio.TimeoutError:
            continue
