#!/bin/zsh
set -eu
ROOT="${0:A:h}"
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    print "需要 Python 3.9 或以上。本版不会自动下载或安装运行环境。"
    print "已有 Python 时，可用它运行同目录 install.py --install。"
    read -r "REPLY?按回车关闭…"
    exit 1
fi
print "安装 workbuddy-cua 到当前用户运行目录，并备份、合并 WorkBuddy 连接配置。"
print "不需要 sudo，不更改系统权限，不自动开启界面操作。"
if "$PYTHON" -B "$ROOT/install.py" --install; then
    print "组件与配置部署完成；请在 WorkBuddy 自定义连接器入口确认信任/启用。"
else
    print "安装未完成，请保留上方错误信息；不会自动重试。"
    read -r "REPLY?按回车关闭…"
    exit 1
fi
read -r "REPLY?按回车关闭…"
