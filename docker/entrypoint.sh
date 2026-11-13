#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/logs /data/scan /data/p0 /data/home /data/media /run

# 读密钥需要读取微信进程内存（/proc/pid/mem）。容器需以 --cap-add SYS_PTRACE 运行。
if [[ "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)" != "0" ]]; then
  echo "[entrypoint] 提示：ptrace_scope 非 0，若无法读取微信内存，请以 --cap-add SYS_PTRACE 运行容器。"
fi

exec /usr/bin/supervisord -n -c /etc/supervisor/wx.conf
