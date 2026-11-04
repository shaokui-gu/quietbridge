#!/bin/sh
# 双击运行：启动本机扩展安装页，并自动打开浏览器。关掉终端窗口或按 Ctrl+C 即停止。
cd "$(dirname "$0")" || exit 1
if [ -x /usr/bin/python3 ]; then
  exec /usr/bin/python3 -B serve.py
fi
exec python3 -B serve.py
