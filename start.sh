#!/usr/bin/env bash
# VOC洞察平台 · 公网/局域网启动脚本（Linux / macOS / Git Bash）
#
#   ./start.sh              前台启动
#   ./start.sh --daemon     后台启动，日志写到 VOCData/platform.log
#
# 口令从环境变量读取；未设置则本机可用、远程只读（不会裸奔）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${VOC_DATA_DIR:-$HERE/VOCData}"
HOST="${VOC_HOST:-0.0.0.0}"
PORT="${VOC_PORT:-8080}"

PY="${VOC_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
fi
[ -z "$PY" ] && { echo "未找到 Python，请先安装 3.9+ 或设置 VOC_PYTHON"; exit 1; }

mkdir -p "$DATA_DIR"

echo "数据目录 : $DATA_DIR"
echo "监听地址 : $HOST:$PORT"
if [ -z "${VOC_PASSWORD:-}" ]; then
  echo "⚠ 未设置 VOC_PASSWORD —— 远程访问将被限制为只读，且无法登录"
else
  echo "管理员口令 : 已设置"
fi
[ -n "${VOC_GUEST_PASSWORD:-}" ] && echo "访客口令   : 已设置（只读）"

CMD=("$PY" "$HERE/web/serve.py" --host "$HOST" --port "$PORT" --data-dir "$DATA_DIR" --no-browser)

if [ "${1:-}" = "--daemon" ]; then
  mkdir -p "$DATA_DIR"
  nohup "${CMD[@]}" > "$DATA_DIR/platform.log" 2>&1 &
  echo "已后台启动，PID=$!  日志：$DATA_DIR/platform.log"
else
  exec "${CMD[@]}"
fi