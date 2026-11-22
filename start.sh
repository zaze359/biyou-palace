#!/usr/bin/env bash
#
# 碧游宫 · 统一启动入口
#
#   bash start.sh                    弹出菜单选择要启动的组件
#   bash start.sh <组件> [参数...]    无交互直跑，其余参数透传给组件启动命令
#   bash start.sh --list             只列组件
#   bash start.sh --help             帮助
#
# 活跃组件：desktop（V2.0-A 主线）
# V1 旧组件（quasheng/sanxian/zhentu/cluster）已归档，仅作参考实现，
#   通过 `bash start.sh legacy <组件>` 启动；详见各模块 ARCHIVED.md。
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# 组件表： 名称 | 一句话描述 | 启动命令
# 新增组件：在此追加一行即可（启动命令可以是任意 shell 片段，路径里不要带空格）。
ENTRIES=(
  "desktop|桌面客户端 · 碧游宫壳 + 本地 dsh 运行时（V2.0-A 主线）|bash desktop/start.sh"
)

# V1 归档组件：仅作参考实现，不出现在默认菜单，通过 legacy 子命令显式启动
LEGACY_ENTRIES=(
  "quasheng|内门仙徒 · PC（Python + Ollama，:4010；旧名准圣；已归档）|bash scripts/run_quasheng.sh"
  "sanxian|外门仙徒 · 安卓（Kotlin + llama.cpp，:4011；需真机 + Android SDK；已归档）|bash nodes/sanxian/build_and_run_sanxian.sh"
  "zhentu|阵图 · 节点注册表与选路（:4020；已归档）|bash scripts/run_zhentu.sh"
  "cluster|中枢机 · 网关 + 阵图 + 灵脉编排（已归档）|bash scripts/bootstrap_cluster.sh"
)

# 旧名/别名 → 组件名
alias_of() {
  case "$1" in
    准圣|quanheng) echo "quasheng" ;;
    散仙|sanxian-node) echo "sanxian" ;;
    registry|router) echo "zhentu" ;;
    gateway|newapi) echo "cluster" ;;
    client|app|biyou) echo "desktop" ;;
    *) echo "$1" ;;
  esac
}

usage() {
  cat <<'TXT'
碧游宫 · 统一启动入口

  bash start.sh                     弹出菜单
  bash start.sh <组件> [参数...]     直跑组件，其余参数透传
  bash start.sh legacy <组件> ...    启动 V1 归档组件（仅作参考实现）
  bash start.sh --list              列出组件
  bash start.sh --help              本帮助

活跃组件（V2.0-A 主线）：
  desktop    桌面客户端 · 碧游宫壳 + 本地 dsh 运行时

V1 归档组件（仅作参考实现，不再演进）：
  quasheng   内门仙徒 · PC（Python + Ollama，:4010；旧名准圣）
  sanxian    外门仙徒 · 安卓（Kotlin + llama.cpp，:4011；旧名散仙）
  zhentu     阵图 · 节点注册表与选路（:4020）
  cluster    中枢机 · 网关 + 阵图 + 灵脉编排

示例：
  bash start.sh desktop --no-sandbox    # 受限环境起桌面客户端
  bash start.sh desktop --smoke-dsh     # 只验 dsh 接入，不开窗口
  bash start.sh legacy cluster --check  # 只校验中枢机配置（V1）
  bash start.sh legacy cluster --up     # 生成配置并 docker compose up -d（V1）
TXT
}

list_entries() {
  local entry name desc
  echo "活跃组件（V2.0-A 主线）："
  for entry in "${ENTRIES[@]}"; do
    name="${entry%%|*}"
    desc="${entry#*|}"
    desc="${desc%%|*}"
    printf "  %-9s %s\n" "$name" "$desc"
  done
  echo "V1 归档组件（bash start.sh legacy <组件>）："
  for entry in "${LEGACY_ENTRIES[@]}"; do
    name="${entry%%|*}"
    desc="${entry#*|}"
    desc="${desc%%|*}"
    printf "  %-9s %s\n" "$name" "$desc"
  done
}

run_entry() {
  local target="$1"; shift
  local entry name desc cmd
  local -a table=("${ENTRIES[@]}")
  # 第一个位置参数是表名时切换表（legacy），否则默认活跃表
  if [ "${1:-}" = "@legacy" ]; then shift; table=("${LEGACY_ENTRIES[@]}"); fi
  for entry in "${table[@]}"; do
    name="${entry%%|*}"
    if [ "$name" = "$target" ]; then
      desc="${entry#*|}"
      desc="${desc%%|*}"
      cmd="${entry##*|}"
      echo "==> 启动 [$target] $desc"
      echo "    命令: $cmd $*"
      # 拆成数组再 exec：严格保留后续参数，不依赖未加引号的变量拆分
      local -a argv
      read -r -a argv <<< "$cmd"
      exec "${argv[@]}" "$@"
    fi
  done
  echo "[ERR] 未知组件: $target" >&2
  echo "    可选组件: $(printf '%s ' "${table[@]%%|*}")" >&2
  echo "    用 --list 看描述，--help 看示例。" >&2
  exit 1
}

# 带参数：直跑（--help/--list/legacy 除外）
if [ $# -ge 1 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -l|--list) list_entries; exit 0 ;;
    legacy)
      if [ $# -lt 2 ]; then
        echo "[ERR] legacy 需要一个组件名，可选：$(printf '%s ' "${LEGACY_ENTRIES[@]%%|*}")" >&2
        exit 1
      fi
      run_entry "$(alias_of "$2")" "@legacy" "${@:3}"
      ;;
  esac
  # V1 组件已归档：老命令给出迁移提示，不静默启动
  case "$(alias_of "$1")" in
    quasheng|sanxian|zhentu|cluster)
      echo "[INFO] $1 属于 V1 归档组件，仅作参考实现。" >&2
      echo "       若仍需启动，请用：bash start.sh legacy $1 ${*:2}" >&2
      exit 1
      ;;
  esac
  run_entry "$(alias_of "$1")" "${@:2}"
fi

# 无参数：弹出菜单
echo ""
echo "========== 碧游宫 · 启动入口 =========="
echo "仓库: $REPO_ROOT"
echo ""
count=${#ENTRIES[@]}
i=1
for entry in "${ENTRIES[@]}"; do
  name="${entry%%|*}"
  desc="${entry#*|}"
  desc="${desc%%|*}"
  printf "  %2d) %-9s %s\n" "$i" "$name" "$desc"
  i=$((i + 1))
done
printf "  %2d) %s\n" "$i" "退出"

while true; do
  read -r -p "请选择组件 [1-$count, 或 $i 退出] > " choice
  case "$choice" in
    "${i}"|q|Q) echo "已取消。"; exit 0 ;;
  esac
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$count" ]; then
    entry="${ENTRIES[$((choice - 1))]}"
    run_entry "${entry%%|*}"
  fi
  echo "[ERR] 无效选择: ${choice}，请重试。"
done
