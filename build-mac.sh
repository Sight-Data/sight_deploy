#!/usr/bin/env bash
# Mac 上本地打包：bash build-mac.sh  →  dist/sight-deploy-<版本>-mac-<arch>.zip
#   MAC_TARGET_ARCH=universal2 bash build-mac.sh   # 同时支持 Intel / Apple 芯片（需 python.org 的通用版 Python）
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash build-mac.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "需要 Python 3.8+（brew install python 或 python.org 安装）" >&2; exit 1; }
exec "$PY" build.py "$@"
