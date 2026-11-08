#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source ./runtime.sh
"$QUIETBRIDGE_INSTALL_PYTHON" -B ./deploy.py --recover-latest
read -r -p '按回车关闭。' ignored
