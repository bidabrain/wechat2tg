#!/usr/bin/env bash
# 启动 Linux 桌面版微信。HOME 指向持久卷，保存登录态和聊天数据库。
set -euo pipefail
export HOME="${WECHAT_HOME:-/data/home}"
mkdir -p "$HOME"
# 等 X 就绪
for _ in $(seq 1 30); do xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1 && break; sleep 1; done
# 每个微信需要自己的 dbus 会话
eval "$(dbus-launch --sh-syntax)"
exec /usr/bin/wechat --no-sandbox
