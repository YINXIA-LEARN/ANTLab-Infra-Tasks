#!/usr/bin/env bash
# ============================================================================
#  start.sh —— 启动控制层
#
#  用法：  bash start.sh
#
#  为什么要有这个脚本：
#    直接 nohup 启动后，进程的命令行只是 "python3 run.py"，
#    用 pkill -f "src/run.py" 匹配不到，导致"停不掉"。
#    所以这里把 PID 写进文件，停止脚本按 PID 精确停止。
# ============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

PIDFILE="logs/control.pid"
LOGFILE="logs/control.log"
mkdir -p logs

# ── 已在运行？──────────────────────────────────────────────────────────────
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "⚠️  控制层已在运行（PID $OLD）"
    echo "    如要重启： bash $DIR/restart.sh"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

# 兜底：检查端口是否被别的进程占用
if ss -tln 2>/dev/null | grep -q ":8000 "; then
  echo "❌ 端口 8000 已被占用，但不是本脚本启动的进程。"
  echo "   查看占用者： ss -tlnp | grep :8000"
  exit 1
fi

# ── 启动 ───────────────────────────────────────────────────────────────────
nohup python3 run.py > "$LOGFILE" 2>&1 &
NEWPID=$!
echo "$NEWPID" > "$PIDFILE"
echo "▶ 已启动，PID=$NEWPID"
echo "  日志： $DIR/$LOGFILE"

# ── 等待就绪（严格判定：解析 JSON，不做字符串匹配）────────────────────────
for i in $(seq 1 20); do
  if curl -sS -m 2 http://127.0.0.1:8000/api/status 2>/dev/null \
     | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('control',{}).get('ok') else 1)" 2>/dev/null; then
    echo "✅ 就绪（${i} 秒）"
    echo "   WSL：     http://127.0.0.1:8000"
    echo "   Windows： http://localhost:8000"
    exit 0
  fi
  sleep 1
done

echo "⚠️  启动后 20 秒仍未就绪，日志尾部："
tail -20 "$LOGFILE"
exit 1
