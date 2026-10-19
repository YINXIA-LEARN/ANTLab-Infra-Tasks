#!/usr/bin/env bash
# 停止进阶项目：先停代理，再停 llama-server（SIGINT → SIGTERM → SIGKILL）
set -u
DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$DEV/.runtime"

stop_pidfile() {
    local f="$1" name="$2"
    [ -f "$f" ] || return 0
    local pid
    pid=$(cat "$f" 2>/dev/null)
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill -INT "$pid" 2>/dev/null
        for i in $(seq 1 40); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null
            for i in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.25
            done
        fi
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null
        fi
        echo "  已停 $name (pid=$pid)"
    fi
    rm -f "$f"
}

echo "停止进阶项目…"
stop_pidfile "$RUN/proxy.pid" "缓存代理"
stop_pidfile "$RUN/llama.pid" "llama-server"

# 兜底：按命令行特征清理（只清本项目的，不动基础项目的）
pkill -f "proxy/proxy.py --config $DEV" 2>/dev/null
pkill -f "llama-server .*--slot-save-path $DEV" 2>/dev/null
sleep 1
echo "完成"
