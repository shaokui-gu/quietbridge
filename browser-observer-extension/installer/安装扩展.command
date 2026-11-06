#!/bin/bash
# 双击运行；服务和错误留在可见终端，关闭窗口或 Ctrl+C 即停止。
set -euo pipefail
cd "$(dirname "$0")"
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [[ -x "$candidate" ]] || continue
    if [[ "$candidate" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        exec "$candidate" -B serve.py
    fi
done
echo '需要 Python 3.9 或更高版本。请安装后重新打开此安装页。' >&2
read -r -p '按回车关闭。' ignored
exit 1
