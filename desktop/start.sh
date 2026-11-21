#!/usr/bin/env bash
#
# 碧游宫桌面客户端 · 一键启动脚本
#
#   bash desktop/start.sh                 # 自检后起壳（开发模式）
#   bash desktop/start.sh --setup         # 先装依赖 / 下载锁定 Node，再起壳
#   bash desktop/start.sh --no-sandbox    # 受限环境（容器 / CI / 沙箱）追加 --no-sandbox
#   bash desktop/start.sh --smoke         # 壳冒烟：截图 + 报告
#   bash desktop/start.sh --smoke-quit    # 验证关窗退出与子进程清理
#   bash desktop/start.sh --smoke-dsh     # 只验 dsh 接入（无图形界面）
#   bash desktop/start.sh --dsh           # 只起 dsh（终端里），不开壳
#
# 其余参数原样透传给启动器（例如 --devtools、--dev）。
set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DESKTOP_DIR"

MODE="dev"
SETUP=0
NO_SANDBOX=0
PASSTHRU=()

usage() {
  cat <<'TXT'
碧游宫桌面客户端 · 一键启动

  bash desktop/start.sh                 # 自检后起壳（开发模式）
  bash desktop/start.sh --setup         # 先装依赖 / 下载锁定 Node，再起壳
  bash desktop/start.sh --no-sandbox    # 受限环境（容器 / CI / 沙箱）追加 --no-sandbox
  bash desktop/start.sh --smoke         # 壳冒烟：截图 + 报告
  bash desktop/start.sh --smoke-quit    # 验证关窗退出与子进程清理
  bash desktop/start.sh --smoke-dsh     # 只验 dsh 接入（无图形界面）
  bash desktop/start.sh --dsh           # 只起 dsh（终端里），不开壳

其余参数原样透传给启动器（例如 --devtools、--dev）。
TXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --setup) SETUP=1 ;;
    --no-sandbox) NO_SANDBOX=1 ;;
    --smoke) MODE="smoke" ;;
    --smoke-quit) MODE="smoke-quit" ;;
    --smoke-dsh) MODE="smoke-dsh" ;;
    --dsh) MODE="dsh" ;;
    -h|--help) usage; exit 0 ;;
    *) PASSTHRU+=("$1") ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
die() { printf '[ERR] %s\n' "$*" >&2; exit 1; }

need_node() {
  command -v npm >/dev/null 2>&1 || die "找不到 npm：请先安装 Node（推荐 24.22.3）"
  command -v node >/dev/null 2>&1 || die "找不到 node：请先安装 Node（推荐 24.22.3）"
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "$major" -ge 20 ] 2>/dev/null || die "构建/启动脚本需要 Node >= 20，当前 $(node -v 2>/dev/null || echo '未知')"
}

missing=()
[ -d node_modules/electron ] || missing+=("Electron")
[ -d runtime/home/profiles/biyou/node_modules/@deepseek-ai/dsh ] || missing+=("dsh 运行时依赖")

install_all() {
  say "==> 安装 Electron（约 100 MB）"
  npm install --no-audit --no-fund
  say ""
  say "==> 安装 dsh 运行时依赖（约 1.2 GB，首次较慢）"
  npm run dsh:install
}

maybe_setup() {
  if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$SETUP" -eq 1 ]; then
      say "==> 缺少：${missing[*]}，开始安装"
      install_all
    elif [ -t 0 ]; then
      say "缺少：${missing[*]}"
      printf '现在安装？需要下载约 1.3 GB，可能耗时几分钟。[Y/n] > '
      read -r answer
      case "${answer:-Y}" in
        n|N) die "已取消。手动装：npm install && npm run dsh:install" ;;
        *) install_all ;;
      esac
    else
      die "缺少：${missing[*]}。请先执行：npm install && npm run dsh:install"
    fi
  fi

  if [ ! -x .runtime/node/bin/node ]; then
    if [ "$SETUP" -eq 1 ]; then
      say "==> 下载锁定 Node 24.22.3（约 200 MB）"
      npm run node:bootstrap
    else
      say "! 未安装锁定 Node（.runtime/node），本次回退 Electron 内置 Node；要严格锁版本请跑：npm run node:bootstrap"
    fi
  fi
}

need_node
maybe_setup

if [ "$NO_SANDBOX" -eq 1 ]; then
  export BIYOU_NO_SANDBOX=1
  say "! 已启用 --no-sandbox（仅限受限环境）"
fi

# 用锁定 Node 跑我们的脚本，避免用户 PATH 上的旧 Node 触发语法错误
RUN_NODE="node"
if [ -x .runtime/node/bin/node ]; then
  RUN_NODE="$DESKTOP_DIR/.runtime/node/bin/node"
fi

say "=============================================="
say " 碧游宫桌面客户端"
say " 目录   $DESKTOP_DIR"
say " Node   $("$RUN_NODE" -v 2>/dev/null || echo '未知')"
say " 锁定   Electron $(node -p 'require("./package.json").devDependencies.electron' 2>/dev/null || echo '?') · dsh $(node -p 'require("./package.json").dsh.version' 2>/dev/null || echo '?')"
say " 模式   $MODE"
say "=============================================="

case "$MODE" in
  dev) exec "$RUN_NODE" scripts/dev.mjs --dev "${PASSTHRU[@]+"${PASSTHRU[@]}"}" ;;
  smoke) exec "$RUN_NODE" scripts/dev.mjs --smoke "${PASSTHRU[@]+"${PASSTHRU[@]}"}" ;;
  smoke-quit) exec "$RUN_NODE" scripts/dev.mjs --smoke-quit "${PASSTHRU[@]+"${PASSTHRU[@]}"}" ;;
  smoke-dsh) exec "$RUN_NODE" scripts/smoke-dsh.mjs "${PASSTHRU[@]+"${PASSTHRU[@]}"}" ;;
  dsh) exec "$RUN_NODE" scripts/run-dsh.mjs "${PASSTHRU[@]+"${PASSTHRU[@]}"}" ;;
  *) die "未知模式：$MODE" ;;
esac
