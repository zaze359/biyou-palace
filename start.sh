#!/usr/bin/env bash
#
# 节点总启动入口
# 运行后弹出菜单，选择要启动的节点；选择后运行对应节点的启动脚本。
# 也支持无交互：bash start.sh <节点名> [节点参数...]   (quasheng | sanxian)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# 节点表： 名称 | 一句话描述 | 启动命令
# 新增节点：在此追加一行即可（启动命令可以是任意 shell 片段）。
NODES=(
  "quasheng|准圣节点 (PC, Python+Ollama 推理代理, 端口4010)|bash scripts/run_quasheng.sh"
  "sanxian|散仙节点 (安卓App, Kotlin/llama.cpp, 端口4011, 需真机+Android SDK)|bash nodes/sanxian/build_and_run_sanxian.sh"
)

run_node() {
  local target="$1"; shift
  for entry in "${NODES[@]}"; do
    local name="${entry%%|*}"
    if [ "$name" = "$target" ]; then
      local cmd="${entry##*|}"
      echo "==> 启动节点 [$target]"
      echo "    命令: $cmd $*"
      # exec 替换为节点启动进程（前台阻塞，Ctrl-C 停止）；其余参数透传给节点命令
      exec $cmd "$@"
    fi
  done
  echo "[ERR] 未知节点: $target" >&2
  echo "    可选节点: $(printf '%s ' "${NODES[@]%%|*}")" >&2
  exit 1
}

# 带参数：直接运行指定节点（跳过菜单），其余参数透传给节点启动命令
if [ $# -ge 1 ]; then
  run_node "$@"
fi

# 无参数：弹出菜单
echo ""
echo "========== 节点启动入口 =========="
echo "仓库: $REPO_ROOT"
echo ""
n=${#NODES[@]}
i=1
for entry in "${NODES[@]}"; do
  name="${entry%%|*}"
  desc="${entry#*|}"
  desc="${desc%%|*}"
  printf "  %2d) %-9s %s\n" "$i" "$name" "$desc"
  i=$((i+1))
done
printf "  %2d) %s\n" "$i" "退出"

while true; do
  read -r -p "请选择要运行的节点 [1-$n, 或 $i 退出] > " choice
  case "$choice" in
    "$i"|q|Q) echo "已取消。"; exit 0 ;;
  esac
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$n" ]; then
    idx=$((choice-1))
    entry="${NODES[$idx]}"
    name="${entry%%|*}"
    run_node "$name"
  fi
  echo "[ERR] 无效选择: ${choice}，请重试。"
done
