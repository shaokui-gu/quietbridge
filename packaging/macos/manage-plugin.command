#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source ../runtime.sh
printf '\nQuietBridge 锁屏授权插件\n\n'
echo '1. 安装 / 更新插件'
echo '2. 查看状态（只读）'
echo '3. 恢复系统原始授权并卸载插件'
echo '4. 启用已停用的插件'
echo '0. 退出'
read -r -p '请选择：' choice
case "$choice" in
    1)
        echo '此操作将请求管理员授权。安装后仍需在菜单栏手动打开锁屏能力。'
        if [[ -e '/Library/Application Support/WorkBuddyCUAAuthorizationPlugin' || -e '/Library/Security/SecurityAgentPlugins/WorkBuddyCUAAuthorizationPlugin.bundle' ]]; then
            action=upgrade
        else
            action=install
        fi
        if ! /usr/bin/sudo "$QUIETBRIDGE_INSTALL_PYTHON" -I -B "$PWD/install.py" "$action" --experimental --bundle "$PWD/build/WorkBuddyCUAAuthorizationPlugin.bundle"; then
            echo '操作未完成。恢复记录已保留，请查看状态；未完成的旧事务请用选项 3 恢复。'
            read -r -p '按回车关闭。' ignored
            exit 1
        fi
        echo '已完成。更新插件后请注销并重新登录，使系统重新加载插件。'
        ;;
    2) "$QUIETBRIDGE_INSTALL_PYTHON" -I -B "$PWD/install.py" status ;;
    3)
        echo '将恢复安装前的授权规则并移除插件。'
        read -r -p '输入 RESTORE 继续：' confirm
        [[ "$confirm" == RESTORE ]] || exit 0
        /usr/bin/sudo "$QUIETBRIDGE_INSTALL_PYTHON" -I -B "$PWD/install.py" uninstall
        ;;
    4) /usr/bin/sudo "$QUIETBRIDGE_INSTALL_PYTHON" -I -B "$PWD/install.py" enable ;;
    *) exit 0 ;;
esac
read -r -p '按回车关闭。' ignored
