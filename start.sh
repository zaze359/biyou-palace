#!/usr/bin/env bash
#
# 碧游宫 · 统一启动入口
#
#   bash start.sh                    弹出菜单选择要启动的组件
#   bash start.sh <组件> [参数...]    无交互直跑，其余参数透传给组件启动命令
#   bash start.sh --list             只列组件
#   bash start.sh --help             帮助
#
# 组件：quasheng | sanxian | zhentu | cluster | desktop
# 旧名兼容：准圣→quasheng、散仙→sanxian、registry→zhentu、client→desktop
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# 组件表： 名称 | 一句话描述 | 启动命令
# 新增组件：在此追加一行即可（启动命令可以是任意 shell 片段，路径里不要带空格）。
ENTRIES=(
  "quasheng|内门仙徒 · PC（Python + Ollama，:4010；旧名准圣）|bash scripts/run_quasheng.sh"
  "sanxian|外门仙徒 · 安卓（Kotlin + llama.cpp，:4011；需真机 + Android SDK；旧名散仙）|bash nodes/sanxian/build_and_run_sanxian.sh"
  "zhentu|阵图 · 节点注册表与选路（:4020；需 ZHENTU_REGISTRY_TOKEN）|bash scripts/run_zhentu.sh"
  "cluster|中枢机 · 网关 + 阵图 + 灵脉编排（默认只生成配置，加 --up 才拉起）|bash scripts/bootstrap_cluster.sh"
  "desktop|桌面客户端 · 碧游宫壳 + 本地 dsh 运行时|bash desktop/start.sh"
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
  bash start.sh --list              列出组件
  bash start.sh --help              本帮助

组件：
  quasheng   内门仙徒 · PC（Python + Ollama，:4010；旧名准圣）
  sanxian    外门仙徒 · 安卓（Kotlin + llama.cpp，:4011；需真机；旧名散仙）
  zhentu     阵图 · 节点注册表与选路（:4020；需 ZHENTU_REGISTRY_TOKEN）
  cluster    中枢机 · 网关 + 阵图 + 灵脉编排（默认只生成配置，--up 才拉起）
  desktop    桌面客户端 · 碧游宫壳 + 本地 dsh 运行时

示例：
  bash start.sh quasheng                # 前台起节点，Ctrl-C 停止
  bash start.sh cluster --check         # 只校验中枢机配置
  bash start.sh cluster --up            # 生成配置并 docker compose up -d
  bash start.sh desktop --no-sandbox    # 受限环境起桌面客户端
  bash start.sh desktop --smoke-dsh     # 只验 dsh 接入，不开窗口
TXT
}

list_entries() {
  local entry name desc
  for entry in "${ENTRIES[@]}"; do
    name="${entry%%|*}"
    desc="${entry#*|}"
    desc="${desc%%|*}"
    printf "  %-9s %s\n" "$name" "$desc"
  done
}

run_entry() {
  local target="$1"; shift
  local entry name desc cmd
  for entry in "${ENTRIES[@]}"; do
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
  echo "    可选组件: $(printf '%s ' "${ENTRIES[@]%%|*}")" >&2
  echo "    用 --list 看描述，--help 看示例。" >&2
  exit 1
}

# 带参数：直跑（--help/--list 除外）
if [ $# -ge 1 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -l|--list) list_entries; exit 0 ;;
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
