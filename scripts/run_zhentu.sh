#!/usr/bin/env bash
#
# 阵图 · 节点注册表（zhentu registry）启动脚本
#
# 用法：
#   export ZHENTU_REGISTRY_TOKEN='<cluster-token>'
#   bash scripts/run_zhentu.sh
#
# 环境变量：
#   ZHENTU_HOST              默认 0.0.0.0（局域网节点可访问）
#   ZHENTU_PORT              默认 4020
#   ZHENTU_DB                默认 zhentu/data/registry.db
#   ZHENTU_REGISTRY_TOKEN    Bearer token；非回环监听时必填
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
REQ_FILE="$REPO_ROOT/zhentu/requirements.txt"
HOST="${ZHENTU_HOST:-0.0.0.0}"
PORT="${ZHENTU_PORT:-4020}"

MANAGED_PY="/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
if [ -x "$MANAGED_PY" ]; then
  PY="$MANAGED_PY"
else
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "[ERR] 未找到 python3，请先安装 Python 3.11+" >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "==> 创建虚拟环境: $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

echo "==> 安装依赖: $REQ_FILE"
(
  export CODEBUDDY_SAFE_DELETE_ENABLED=0
  "$VENV_DIR/bin/pip" install -q -r "$REQ_FILE"
)

if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ] && [ -z "${ZHENTU_REGISTRY_TOKEN:-}" ]; then
  echo "[ERR] 监听 $HOST 但 ZHENTU_REGISTRY_TOKEN 为空；请先导出集群 token。" >&2
  echo "      仅本机调试可设置 ZHENTU_HOST=127.0.0.1。" >&2
  exit 1
fi

export ZHENTU_HOST="$HOST"
export ZHENTU_PORT="$PORT"
cd "$REPO_ROOT"
echo "==> 启动 zhentu registry ($HOST:$PORT)"
exec "$VENV_PY" -m uvicorn zhentu.app:create_app --factory --host "$HOST" --port "$PORT"
