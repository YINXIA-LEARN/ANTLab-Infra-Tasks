#!/usr/bin/env bash
# ============================================================================
#  stop.sh —— 停止控制层（按 PID 精确停止，不影响其他 python 进程）
# ============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/logs/control.pid"

stop_one() {
  local pid="$1"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  echo "  停止 PID $pid …"
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || { echo "  ✅ 已停止"; return 0; }
    sleep 0.5
  done
  echo "  温柔停止无效，强制结束"
  kill -9 "$pid" 2>/dev/null
  sleep 1
  kill -0 "$pid" 2>/dev/null && { echo "  ❌ 仍无法停止"; return 2; }
  echo "  ✅ 已强制停止"
  return 0
}

echo "停止控制层"

STOPPED=0
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [ -n "$PID" ] && stop_one "$PID"; then
    STOPPED=1
  fi
  rm -f "$PIDFILE"
else
  echo "  （没有 PID 文件）"
fi

# 兜底：按端口找残留
if ss -tlnp 2>/dev/null | grep -q ":8000 "; then
  echo "  端口 8000 仍有监听，尝试按进程名清理"
  pkill -f "python3 run.py" 2>/dev/null && { echo "  ✅ 已清理"; STOPPED=1; }
  sleep 1
fi

if ss -tlnp 2>/dev/null | grep -q ":8000 "; then
  echo "⚠️  端口 8000 仍被占用："
  ss -tlnp 2>/dev/null | grep ":8000 "
  exit 1
fi

if [ "$STOPPED" -eq 1 ]; then
  echo "✅ 控制层已停止，端口 8000 已释放"
else
  echo "（没有在运行的控制层）"
fi
