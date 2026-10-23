#!/usr/bin/env bash
# 启动 stage1 训练监控页（VM 侧，detached：可存活于内核重启/SSH 断开）。
#
# 用法：
#   bash start_monitor.sh [日志路径] [端口]
#   bash start_monitor.sh /content/stage1_out/train_L4.log 8000
#
# 之后在本机起隧道（见 tunnel.ps1）就能用浏览器看：
#   http://127.0.0.1:8010/
set -euo pipefail

LOG="${1:-/content/stage1_out/train_L4.log}"
PORT="${2:-8000}"
DIR="$(dirname "$LOG")"
mkdir -p "$DIR"

# 旧实例先清掉（幂等）
pkill -f monitor_server.py 2>/dev/null || true
sleep 1

# setsid + nohup + 显式重定向：既脱离调用方（避免管道继承导致调用方挂死），
# 也脱离内核（实测重启内核后仍存活）。
setsid nohup python3 -u /content/monitor_server.py \
  --log "$LOG" --out-dir "$DIR" --port "$PORT" \
  --title "stage1 训练监控" \
  > /content/monitor.out 2>&1 < /dev/null &

sleep 3
echo "[monitor] 自检:"
curl -s -m 10 -o /dev/null -w "  health=%{http_code}\n" "http://127.0.0.1:$PORT/health" || echo "  health 请求失败"
pgrep -af monitor_server.py || echo "  进程没起来，看 /content/monitor.out"
echo "[monitor] 本机隧道示例: ssh -F <ssh_config> -N -L 8010:127.0.0.1:$PORT colabmon"
