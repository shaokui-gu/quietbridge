#!/bin/zsh
set -eu
ROOT="${0:A:h}"
PYTHON="/opt/homebrew/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

print "workbuddy-cua Authorization Plug-in 升级"
print ""
print "本次只做两件事，且不改动 system.login.screensaver 入口规则："
print "  1. 用 build/ 下的新插件替换 /Library/Security/SecurityAgentPlugins 内的旧插件"
print "  2. remote 分支机制：WorkBuddyCUAAuthorizationPlugin:allow,privileged -> :allow"
print ""
print "替换前的插件与授权规则都会保留备份，不会删除。"
print ""
"$PYTHON" "$ROOT/upgrade_plugin.py" --check || exit 1
print ""
read -r "REPLY?输入 UPGRADE 继续："
if [[ "$REPLY" != "UPGRADE" ]]; then
  print "已取消。"
  exit 1
fi
sudo "$PYTHON" "$ROOT/upgrade_plugin.py"
print ""
print "升级完成。验证方式：锁屏后在本机解锁，然后查看日志里的插件记录："
print "  /usr/bin/log show --last 5m --predicate 'eventMessage CONTAINS \"workbuddy-cua\"' | tail -20"
print "期望：出现 mechanism returned Allow；不再出现 stage=server-sectask（该行只在身份不匹配时打印）。"
