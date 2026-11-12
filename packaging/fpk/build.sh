#!/usr/bin/env bash
# fnmedia-ext FPK 打包：组装骨架 + 项目主体 → fnpack build → dist/*.fpk
# 用法：packaging/fpk/build.sh [--public] [--stage-only DIR] [--fnpack /path] [--out dist]
#   --public  公开包：只含通用公开能力（在全功能源码树上会先执行构建期裁剪）
set -euo pipefail

FPK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FPK_DIR}/../.." && pwd)"
VERSION="$(head -n1 "${REPO_ROOT}/VERSION" | tr -d '[:space:]')"
[ -n "${VERSION}" ] || { echo "ERROR: 无法读取 VERSION" >&2; exit 1; }

FNPACK_BIN="${FNPACK:-}"; STAGE=""; STAGE_ONLY=0; OUT_DIR="${REPO_ROOT}/dist"; PUBLIC=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stage-only) STAGE_ONLY=1; STAGE="$2"; shift 2 ;;
    --fnpack) FNPACK_BIN="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --public) PUBLIC=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done
[ -n "${STAGE}" ] || STAGE="${FPK_DIR}/.build/fpk"
command -v rsync >/dev/null 2>&1 || { echo "ERROR: 需要 rsync" >&2; exit 1; }

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='dist/' --exclude='logs/' \
  --exclude='state/' --exclude='packaging/fpk/.build/' \
  --exclude='.release/' --exclude='.agents/' --exclude='*.fpk' \
  --exclude='.DS_Store' \
  --exclude='/spider-runner/android/' \
  --exclude='/spider-runner/no_android/' \
  --exclude='/proxy/no_android_router.py' \
  --exclude='/proxy/assets/guard/' \
  --exclude='/proxy/assets/spiders/' \
  --exclude='/proxy/assets/guard/guard.jar' \
  --exclude='/proxy/assets/guard/sites.json' \
  --exclude='/proxy/assets/guard/imported.json' \
  --exclude='/proxy/assets/spiders/jars/' \
  --exclude='/proxy/assets/spiders/jars.json' \
  --exclude='/proxy/assets/spiders/spider_sites.json' \
  --exclude='/proxy/assets/spiders/vendor/' \
  --exclude='/FORUM_POST.md' \
  --exclude='/FORUM_POST_CHANGELOG.md' \
  --exclude='/FORUM_RELEASE_*.md' \
  --exclude='/RELEASING.md' \
  --exclude='*.apk' \
  "${REPO_ROOT}/" "${STAGE}/app/repo/"


# 不再创建 Android/redroid、Guard/SPIDER 或 no_android 资产目录，也不播种
# 任何 bridge APK/JAR；这些运行时及其管理入口已从分发包移除。
# 维护者个人本地配置（含网盘账号）不进任何分发包。
LOCAL_CFG="local.json"
rm -f "${STAGE}/app/repo/proxy/"*"${LOCAL_CFG}"

# 清洗【分发包内】CHANGELOG：去除维护者私源名、内置 jar 路径、限制级能力描述。
# 仅改暂存副本，源码 CHANGELOG.md 保留完整开发记录。
if [ -f "${STAGE}/app/repo/tools/sanitize_changelog.py" ]; then
  "${PYTHON:-python3}" "${STAGE}/app/repo/tools/sanitize_changelog.py" \
    "${STAGE}/app/repo/CHANGELOG.md"
fi

# 公开构建：从暂存副本移除可选特性（非标准源适配 / 扩展分类）。
# 全功能源码树里有构建剥离器时执行剥离；若工作树本身已是公开树
# （剥离器已不存在），则内容本就干净，直接跳过。
if [ "${PUBLIC}" -eq 1 ] && [ -f "${STAGE}/app/repo/tools/public_strip.py" ]; then
  PY3="${PYTHON:-python3}"
  "${PY3}" "${STAGE}/app/repo/tools/public_strip.py" "${STAGE}/app/repo"
  # 构建工具不进包；剥空的 assets 目录也清掉
  rm -rf "${STAGE}/app/repo/tools"
  rmdir "${STAGE}/app/repo/proxy/assets" 2>/dev/null || true
fi

MANIFEST_VER="$(echo "${VERSION}" | sed -E 's/([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
sed "s/@VERSION@/${MANIFEST_VER}/" "${FPK_DIR}/manifest.in" > "${STAGE}/manifest"
cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/" 2>/dev/null || true
cp -a "${FPK_DIR}/app/ui/." "${STAGE}/app/ui/"
cp -a "${FPK_DIR}/cmd" "${STAGE}/cmd"
cp -a "${FPK_DIR}/config" "${STAGE}/config"
# 安装向导（安装时展示免责声明，需用户同意）
if [ -d "${FPK_DIR}/wizard" ]; then
  mkdir -p "${STAGE}/wizard"
  cp -a "${FPK_DIR}/wizard/." "${STAGE}/wizard/"
fi
chmod +x "${STAGE}"/cmd/*

echo "[fpk] 组装完成: ${STAGE} (version=${VERSION})"
[ "${STAGE_ONLY}" -eq 1 ] && exit 0

if [ -z "${FNPACK_BIN}" ]; then
  if command -v fnpack >/dev/null 2>&1; then FNPACK_BIN="$(command -v fnpack)"; else
    case "$(uname -m)" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) echo "不支持的架构" >&2; exit 1;; esac
    FNPACK_BIN="${REPO_ROOT}/.tools/fnpack"; mkdir -p "$(dirname "${FNPACK_BIN}")"
    [ -x "${FNPACK_BIN}" ] || curl -fsSL -o "${FNPACK_BIN}" "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-${A}"
    chmod +x "${FNPACK_BIN}"
  fi
fi
WORK="$(mktemp -d)"; trap 'rm -rf "${WORK}"' EXIT
( cd "${WORK}" && "${FNPACK_BIN}" build --directory "${STAGE}" )
BUILT="$(find "${WORK}" "${STAGE}" -maxdepth 2 -name '*.fpk' -print -quit 2>/dev/null || true)"
[ -n "${BUILT}" ] || { echo "ERROR: fnpack 未产出 .fpk" >&2; exit 1; }
mkdir -p "${OUT_DIR}"
SUFFIX=""; [ "${PUBLIC}" -eq 1 ] && SUFFIX="-public"
FINAL="${OUT_DIR}/fnmedia-ext-${VERSION}${SUFFIX}.fpk"
mv "${BUILT}" "${FINAL}"
( cd "$(dirname "${FINAL}")" && sha256sum "$(basename "${FINAL}")" > "$(basename "${FINAL}").sha256" )
echo "[fpk] 打包完成: ${FINAL}"
