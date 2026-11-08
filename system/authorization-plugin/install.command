#!/bin/zsh
set -eu
ROOT="${0:A:h}"
PYTHON="/opt/homebrew/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

print "quietbridge Authorization Plug-in"
print "此步骤会修改 system.login.screensaver，并安装系统级授权插件。"
print "这是第二版实验安装，尚未验证真实解锁与密码回落。"
print "原策略会保存用于卸载恢复；写入后会逐项读回核对。"
print "只应在受控测试 Mac 或已确认风险的设备上继续。"
read -r "REPLY?输入 INSTALL-EXPERIMENTAL 继续："
if [[ "$REPLY" != "INSTALL-EXPERIMENTAL" ]]; then
  print "已取消。"
  exit 1
fi
"$PYTHON" "$ROOT/install.py" build
exec "$PYTHON" "$ROOT/prepare_install.py"
