#!/usr/bin/env bash
# 虚拟显示器 + 窗口管理器。微信是 GUI 程序，需要 X 环境。
set -euo pipefail
rm -f /tmp/.X${DISPLAY#:}-lock "/tmp/.X11-unix/X${DISPLAY#:}" 2>/dev/null || true
exec Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp
