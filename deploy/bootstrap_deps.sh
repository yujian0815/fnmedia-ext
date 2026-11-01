#!/usr/bin/env bash
# fnmedia-ext 运行时依赖引导（被 install_callback / upgrade_callback 复用）。
#
# 目标：在“干净、可能无法直连 PyPI”的飞牛 NAS 上高成功率地准备 .venv。
#   1) 创建独立 venv（系统 python3-venv 缺失时尝试 virtualenv / 自带 ensurepip 兜底）；
#   2) 优先用随包内置的【离线 wheelhouse】安装（含 x86_64/aarch64 原生 wheel，
#      无需联网、无需编译）；
#   3) 离线不全时，按国内镜像 → 官方源顺序在线兜底；
#   4) 安装后做真实 import 自检；核心服务依赖不齐全才判定失败。
#
# 环境变量：
#   FNMEDIA_REQUIRE_NET=1   跳过离线、强制在线（排障用）
#   FNMEDIA_PIP_INDEX=URL   追加自定义 pip 源
#   FNMEDIA_LOG=路径         日志文件（由 _common 提供默认值）
set -uo pipefail

LOG="${FNMEDIA_LOG:-/tmp/fnmedia-app.log}"
log(){ echo "[deps] $*" >> "$LOG" 2>&1 || true; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "无法进入应用目录" >&2; exit 1; }
VENV="$ROOT/.venv"
PYBIN="$VENV/bin/python"
PIP="$VENV/bin/pip"

# 核心服务启动【强依赖】。wasmtime 等可选重依赖不在此列（dduo WASM 源惰性导入，
# 缺失时相关功能优雅降级，绝不拖垮主服务）。
CORE_PKGS=(fastapi uvicorn httpx)

PIP_MIRRORS=(
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple"
  "https://mirror.nju.edu.cn/pypi/web/simple"
  "https://pypi.mirrors.ustc.edu.cn/simple"
  "https://pypi.org/simple"
)
[ -n "${FNMEDIA_PIP_INDEX:-}" ] && PIP_MIRRORS=("$FNMEDIA_PIP_INDEX" "${PIP_MIRRORS[@]}")

# ---------- 1. venv ----------
# 已有可用 venv 且核心包齐全：直接通过（升级场景不重复安装，秒退）。
if [ -x "$PYBIN" ] && "$PYBIN" -c 'import fastapi,uvicorn,httpx' >/dev/null 2>&1; then
  log "核心依赖已就绪，跳过安装"
  exit 0
fi

if [ ! -x "$PYBIN" ]; then
  log "创建虚拟环境 $VENV (python3 -> $(command -v python3) $(python3 -V 2>&1))"
  if ! python3 -m venv "$VENV" >> "$LOG" 2>&1; then
    log "python3 -m venv 失败，尝试 ensurepip / virtualenv 兜底"
    python3 -m ensurepip --upgrade >> "$LOG" 2>&1 || true
    if python3 -m venv "$VENV" >> "$LOG" 2>&1; then :; elif command -v virtualenv >/dev/null 2>&1; then
      virtualenv -p python3 "$VENV" >> "$LOG" 2>&1
    else
      python3 -m pip install --user virtualenv >> "$LOG" 2>&1 || true
      python3 -m virtualenv -p python3 "$VENV" >> "$LOG" 2>&1 \
        || { echo "创建 venv 失败：请在应用中心或终端安装 python3-venv / python3-pip" >&2; exit 2; }
    fi
  fi
fi
[ -x "$PYBIN" ] || { echo "虚拟环境 python 不可用" >&2; exit 2; }

# pip 基础参数：安静、不超时太死、必要时信任国内镜像 http/自签
PIP_BASE=(--disable-pip-version-check --retries 2 --timeout 25)

# ---------- 2. 离线 wheelhouse 优先 ----------
WHEELHOUSE="$ROOT/wheelhouse"
offline_ok=0
if [ "${FNMEDIA_REQUIRE_NET:-0}" != "1" ] && [ -d "$WHEELHOUSE" ]; then
  if ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
    log "尝试离线安装（内置 wheelhouse：$(ls "$WHEELHOUSE"/*.whl | wc -l | tr -d ' ') 个 wheel）"
    # --no-index 强制只用本地 wheel；平台不符时 pip 会报“无匹配发行版”而非乱装
    if "$PIP" install "${PIP_BASE[@]}" --no-index --find-links "$WHEELHOUSE" \
         "${CORE_PKGS[@]}" >> "$LOG" 2>&1; then
      offline_ok=1
      log "离线安装成功"
    else
      log "离线 wheelhouse 未覆盖当前平台/Python 版本，转在线兜底"
    fi
  fi
fi

# ---------- 3. 在线多镜像兜底 ----------
if [ "$offline_ok" != "1" ]; then
  installed=0
  for idx in "${PIP_MIRRORS[@]}"; do
    log "在线安装尝试：$idx"
    if "$PIP" install "${PIP_BASE[@]}" \
         --index-url "$idx" --trusted-host "$(echo "$idx" | awk -F/ '{print $3}')" \
         "${CORE_PKGS[@]}" >> "$LOG" 2>&1; then
      installed=1; log "在线安装成功（$idx）"; break
    fi
    log "源失败：$idx"
  done
  [ "$installed" = "1" ] || { echo "依赖在线安装全部失败（已尝试内置离线包与多个镜像）" >&2; exit 3; }
fi

# ---------- 4. 真实 import 自检 ----------
if ! "$PYBIN" - <<'PY' >> "$LOG" 2>&1
import fastapi, uvicorn, httpx, starlette, pydantic
print("[deps] import OK:",
      "fastapi", fastapi.__version__,
      "uvicorn", uvicorn.__version__,
      "httpx", httpx.__version__)
PY
then
  echo "依赖已安装但自检 import 失败（平台/Python 版本不兼容）" >&2
  exit 4
fi

log "依赖就绪"
exit 0
