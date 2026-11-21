#!/usr/bin/env bash
#
# macOS 双击入口：Finder 里双击本文件即开终端跑 start.sh。
# 失败时停住窗口，方便看报错（Terminal 默认执行完就关）。
#
cd "$(dirname "$0")" || exit 1
bash ./start.sh "$@"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  echo "启动失败（退出码 $status）。按回车关闭窗口。"
  read -r _
fi
exit "$status"
