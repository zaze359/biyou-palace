#!/usr/bin/env bash
#
# 准圣节点 quasheng 验证脚本
# 建虚拟环境 → 安装依赖 → 运行纯逻辑单元测试（不依赖 Ollama / 网络）
#
# 用法：
#   bash scripts/verify.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
REQ_FILE="$REPO_ROOT/nodes/quasheng/requirements.txt"
TEST_DIR="$REPO_ROOT/nodes/quasheng/tests"

echo "==> 仓库根目录: $REPO_ROOT"

# 1) 选取 python 解释器
#    优先托管 3.13（与 implement.md 一致）；托管路径缺失时回退到 PATH 上的 python3，
#    并要求 >= 3.11（代码用到 PEP 604 等 3.10+ 语法，3.9 会直接语法报错）。
MANAGED_PY="/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
if [ -x "$MANAGED_PY" ]; then
  PY="$MANAGED_PY"
else
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.11+" >&2
  exit 1
fi

PY_MAJOR="$($PY -c 'import sys; print(sys.version_info[0])')"
PY_MINOR="$($PY -c 'import sys; print(sys.version_info[1])')"
PY_VER="$PY_MAJOR.$PY_MINOR"
echo "==> 使用 python ($PY_VER) @ $PY"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  echo "❌ 需要 Python >= 3.11，当前 $PY_VER" >&2
  echo "   安装示例：brew install python@3.13   或   pyenv install 3.13" >&2
  exit 1
fi

# 2) 创建虚拟环境（已存在则复用）
if [ ! -d "$VENV_DIR" ]; then
  echo "==> 创建虚拟环境: $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# 3) 安装依赖
echo "==> 安装依赖: $REQ_FILE"
"$VENV_PIP" install -q -r "$REQ_FILE"

# 4) 运行测试（退出码透传，失败即非零）
echo "==> 运行 pytest: $TEST_DIR"
"$VENV_PY" -m pytest "$TEST_DIR" -v
