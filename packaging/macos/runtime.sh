#!/bin/bash
# Source from a desktop-user command. Do not invoke the CLT Python stub when
# Command Line Tools are absent: it opens a developer-tools installation dialog.
QUIETBRIDGE_INSTALL_PYTHON=''
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [[ -x "$candidate" ]] || continue
    if [[ "$candidate" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        QUIETBRIDGE_INSTALL_PYTHON="$candidate"
        break
    fi
done
if [[ -z "$QUIETBRIDGE_INSTALL_PYTHON" ]]; then
    echo '需要 Python 3.9 或更高版本。请先安装 Python，再运行安装器。' >&2
    return 1
fi
