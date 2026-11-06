#!/usr/bin/env python3
"""fnmedia-ext 接管守护：和 fnmusic takeover 同样的 fail-closed 原子换名思路。

流程：
  1. 校验 /var/run/trim_media.sock 的监听者 exe 必须是 trim-media
  2. 在私有暂存目录启动 uvicorn 监听 app:app（仅监听暂存 socket）
  3. 校验暂存 socket 存活后，renameat2(NOREPLACE) 把官方 socket 改名
     为 trim_media_upstream.sock，再把我们的 socket 换名成 trim_media.sock
  4. 健康检查通过后常驻；任何退出/异常都把官方 socket 原子换回
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

TARGET = Path("/var/run/trim_media.sock")
UPSTREAM = Path("/var/run/trim_media_upstream.sock")
STATE = Path("/run/fnmedia-ext")
BASE = Path(__file__).resolve().parent.parent
PROXY = BASE / "proxy"
PY = BASE / ".venv/bin/python"
OFFICIAL = "trim-media"


def log(m):
    print(f"[fnmedia] {m}", flush=True)


def peer(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1)
    try:
        s.connect(str(path))
        pid = struct.unpack("3i", s.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        exe = os.readlink(f"/proc/{pid}/exe")
        return pid, exe
    finally:
        s.close()


def is_official(path):
    try:
        pid, exe = peer(path)
        return Path(exe).name == OFFICIAL, pid, exe
    except Exception:
        return False, None, None


def move(src, dst):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1) != 0:
        raise OSError(ctypes.get_errno(), f"rename {src}->{dst}")


def rollback():
    try:
        if UPSTREAM.exists() and not TARGET.exists():
            ok, pid, exe = is_official(UPSTREAM)
            if ok:
                move(UPSTREAM, TARGET)
                log("已回滚官方 socket")
            else:
                log(f"回滚中止：upstream 非官方 {pid} {exe}")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"回滚失败: {e}")


def health_ok(staged, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2)
            s.connect(str(staged))
            s.sendall(b"GET /_ext/healthz HTTP/1.0\r\nHost: x\r\n\r\n")
            data = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                data += c
            s.close()
            if b"200 OK" in data and b'"ok":true' in data:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    # 预检：target 必须官方；upstream 不得存活
    ok, pid, exe = is_official(TARGET)
    if not ok:
        # 已被接管（我们自己）则直接报错退出，不重复接管
        if UPSTREAM.exists():
            log("target 非官方且 upstream 存在，疑似已接管，退出")
            return 2
        log(f"target 不是已验证的 trim-media: {pid} {exe}")
        return 2
    if UPSTREAM.exists():
        try:
            peer(UPSTREAM)
            log("upstream 已被存活进程占用，退出")
            return 2
        except OSError:
            UPSTREAM.unlink()

    staged = STATE / "listen.sock"
    staged.unlink(missing_ok=True)
    env = os.environ.copy()
    env["FNMEDIA_UPSTREAM_SOCK"] = str(UPSTREAM)
    env["PYTHONUNBUFFERED"] = "1"
    child = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app:app", "--app-dir", str(PROXY),
         "--uds", str(staged), "--no-access-log"],
        cwd=str(PROXY), env=env)

    moved = False
    try:
        # 等暂存 socket 就绪。冷启动要恢复榜单绑定/冠军清单/加固目录，数据多或系统繁忙
        # （bridge 拉起、磁盘 IO 高）时 uvicorn 建 socket 可能明显变慢，窗口太短会在应用
        # 即将就绪时误判失败并杀进程，导致服务 failed、媒体库消失。放宽到 45s。
        for _ in range(225):
            if child.poll() is not None:
                log("uvicorn 提前退出")
                return 1
            if staged.exists():
                try:
                    peer(staged)
                    break
                except Exception:
                    pass
            time.sleep(0.2)
        else:
            child.terminate()
            log("暂存 socket 未就绪")
            return 1

        if not health_ok(staged):
            child.terminate()
            log("健康检查失败，不接管")
            return 1

        # 再次确认 target 身份后原子换名
        ok2, pid2, _ = is_official(TARGET)
        if not ok2 or pid2 != pid:
            log("接管前 target 身份变化，放弃")
            child.terminate()
            return 1
        move(TARGET, UPSTREAM)
        moved = True
        os.chmod(staged, 0o666)
        move(staged, TARGET)
        log(f"接管成功：{TARGET} -> 本代理；官方位于 {UPSTREAM}")

        # 应用中心桌面网关：<应用根>/fnmedia-ext.sock -> 主代理 TARGET。
        # 仅承载 /app/fnmedia-ext 管理窗口，媒体播放不依赖它。
        gw_sock = BASE / "fnmedia-ext.sock" if (BASE.parent / "ui").is_dir() \
            else STATE / "fnmedia-ext.sock"
        gw = subprocess.Popen(
            [str(PY), str(PROXY / "webui_gateway.py"), str(BASE), str(TARGET)],
            cwd=str(PROXY), env=env)

        # 捕获停止信号：先退出子进程再回滚
        stopping = {"v": False}

        def stop(signum, frame):
            stopping["v"] = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping["v"]:
            rc = child.poll()
            if rc is not None:
                log(f"uvicorn 异常退出 rc={rc}")
                break
            if gw.poll() is not None:
                log(f"桌面网关异常退出 rc={gw.returncode}，重启")
                gw = subprocess.Popen(
                    [str(PY), str(PROXY / "webui_gateway.py"), str(BASE), str(TARGET)],
                    cwd=str(PROXY), env=env)
            time.sleep(1)
    finally:
        # 先释放应用根目录里的桌面网关 socket 文件名，再等待子进程退出。
        # systemd 的 TimeoutStopSec 较短，不能把 unlink 放在阻塞 wait 之后。
        gw_path = locals().get("gw_sock")
        if gw_path is not None:
            try:
                gw_path.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                log(f"清理网关 socket 失败: {type(e).__name__}: {e}")
        if 'gw' in locals() and gw.poll() is None:
            gw.terminate()
            try:
                gw.wait(timeout=1)
            except subprocess.TimeoutExpired:
                gw.kill()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
        if moved:
            # 我们的 socket 现在占着 TARGET，先删掉再换回官方
            time.sleep(0.3)
            try:
                if TARGET.exists():
                    TARGET.unlink()
            except Exception:
                pass
            rollback()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"致命错误 {type(e).__name__}: {e}")
        rollback()
        sys.exit(1)
