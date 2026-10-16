#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  stop_model_service.sh —— 只停模型服务，控制层不受影响
#
#  用途：制造"服务退出"这个异常，用于
#    · 演示题目的「异常与恢复：服务退出后的检测/重启/就绪确认」
#    · 造出「测试中途服务消失」的数据，验证错误分类
#
#  ⚠️ 为什么不直接用 `bash serve.sh stop`：
#     那个命令【同时停掉控制层和模型服务】，控制层一停，
#     网页、接口、以及"检测到服务掉线"这件事本身就没了 ——
#     而我们要观察的恰恰是"控制层发现服务掉了"。
#
#  ⚠️ 为什么不直接 kill PID：
#     模型服务的 PID 有 PID 文件记着，但那个文件可能过时
#     （比如服务被手工起过、或上次没清干净）。直接 kill 一个
#     过时的 PID 可能打到别的进程上。所以这里先核对再杀。
#
#  用法：
#     bash stop_model_service.sh              # 优雅停（SIGTERM）
#     bash stop_model_service.sh --force      # 强杀（SIGKILL，模拟崩溃）
#     bash stop_model_service.sh --status     # 只看状态，不动手
#     bash stop_model_service.sh --check      # 停完自检（默认会做）
# ══════════════════════════════════════════════════════════════════════════
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="$DIR/logs"
MODEL_PIDFILE="$LOGDIR/model-server.pid"
CONTROL_PIDFILE="$LOGDIR/control.pid"
PORT="${ANTLAB_MODEL_PORT:-8080}"

MODE="term"
for a in "$@"; do
    case "$a" in
        --force)  MODE="kill" ;;
        --status) MODE="status" ;;
        --term)   MODE="term" ;;
        -h|--help)
            sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$a（试 --help）" >&2; exit 2 ;;
    esac
done

c_ok()   { printf '\033[32m  ✅ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m  ⚠️ %s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m  ❌ %s\033[0m\n' "$*"; }
c_dim()  { printf '     %s\n' "$*"; }

# ── 工具 ──────────────────────────────────────────────────────────────────
model_pids() {
    # ⚠️ 用 [l] 避免 pgrep 匹配到自己这条命令行
    pgrep -f "[l]lama-server.*--port $PORT" 2>/dev/null
}

control_pids() {
    pgrep -f "[p]ython3 run.py" 2>/dev/null
}

port_listening() {
    ss -tln 2>/dev/null | grep -q ":$PORT "
}

model_health() {
    # ⚠️ 用 `|| true` 而不是 `|| echo 000` —— curl 失败时会自己输出 000，
    #    再加一次 echo 会拼成 "000000"（显示上看很怪）。
    local c
    c="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:$PORT/health" 2>/dev/null)" || true
    echo "${c:-000}"
}

control_health() {
    local c
    c="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:8000/api/status" 2>/dev/null)" || true
    echo "${c:-000}"
}

show_status() {
    local mp cp mh ch
    mp="$(model_pids | tr '\n' ' ')"
    cp="$(control_pids | tr '\n' ' ')"
    mh="$(model_health)"
    ch="$(control_health)"
    printf '  ① 模型服务  %s   PID: %s   health: %s\n' \
        "$([ -n "$mp" ] && echo 运行中 || echo 未运行)" "${mp:-—}" "$mh"
    printf '  ② 控制层    %s   PID: %s   /api/status: %s\n' \
        "$([ -n "$cp" ] && echo 运行中 || echo 未运行)" "${cp:-—}" "$ch"
    printf '  端口 %s 监听: %s\n' "$PORT" \
        "$(port_listening && echo 是 || echo 否)"
}

echo "════════════════════════════════════════════════════════════════"
echo " 停模型服务（不动控制层）"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "── 停止前 ──"
show_status
echo ""

if [ "$MODE" = "status" ]; then
    echo "（--status 模式，不做任何改动）"
    exit 0
fi

# ── 决策：杀哪些进程 ──────────────────────────────────────────────────────
PIDS="$(model_pids)"
if [ -z "$PIDS" ]; then
    c_warn "没有找到正在跑的 llama-server（端口 $PORT）"
    c_dim "可能它本来就没起。用 bash serve.sh model 可以起。"
    # 端口还被占着却不是 llama-server —— 那要提醒一下
    if port_listening; then
        c_warn "但端口 $PORT 仍被占用，占用者："
        ss -tlnp 2>/dev/null | grep ":$PORT " | sed 's/^/       /'
    fi
    exit 0
fi

c_dim "将停止这些进程："
for p in $PIDS; do
    c_dim "  PID $p  $(ps -o args= -p "$p" 2>/dev/null | cut -c1-90)"
done
echo ""

# ── 停 ────────────────────────────────────────────────────────────────────
T0=$(date +%s.%N)
if [ "$MODE" = "kill" ]; then
    echo "── 强杀（SIGKILL），模拟服务崩溃 ──"
    for p in $PIDS; do
        kill -9 "$p" 2>/dev/null && c_ok "已 SIGKILL PID $p"
    done
else
    echo "── 优雅停止（SIGTERM）──"
    for p in $PIDS; do
        kill "$p" 2>/dev/null && c_ok "已 SIGTERM PID $p"
    done
fi

# 等它退出（最多 10 秒）
for i in $(seq 1 40); do
    [ -z "$(model_pids)" ] && break
    sleep 0.25
done

# 兜底：还在就 SIGKILL
LEFT="$(model_pids)"
if [ -n "$LEFT" ]; then
    c_warn "SIGTERM 后仍未退出，改用 SIGKILL：$LEFT"
    for p in $LEFT; do kill -9 "$p" 2>/dev/null; done
    sleep 1
fi

T1=$(date +%s.%N)
ELAPSED=$(awk "BEGIN{printf \"%.2f\", $T1-$T0}")

# 清掉 PID 文件（serve.sh 与 model_manager 的约定：谁停止谁删）
[ -f "$MODEL_PIDFILE" ] && rm -f "$MODEL_PIDFILE" && c_dim "已删除 PID 文件"

echo ""
echo "── 停止后 ──"
show_status

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " 自检"
echo "════════════════════════════════════════════════════════════════"

MH="$(model_health)"
CH="$(control_health)"
FAIL=0

if [ "$MH" = "000" ] || [ "$MH" != "200" ]; then
    c_ok "模型服务已不可用（health=$MH，耗时 ${ELAPSED}s）"
else
    c_err "模型服务居然还活着（health=$MH）"
    FAIL=1
fi

if [ "$CH" = "200" ]; then
    c_ok "控制层【不受影响】，仍在正常响应（/api/status=200）"
else
    c_err "控制层也挂了（/api/status=$CH）—— 这不是本脚本预期的结果"
    FAIL=1
fi

echo ""
echo "  控制层此刻看到的模型服务状态（这是"检测"那一环的证据）："
curl -sS -m 5 "http://127.0.0.1:8000/api/status" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('     （读不到 /api/status）'); raise SystemExit
def walk(o, pfx=''):
    if isinstance(o, dict):
        for k, v in o.items():
            if any(w in k.lower() for w in ('health','model','state','upstream','reach')):
                if isinstance(v, (str, int, float, bool, type(None))):
                    print('     %-28s %s' % (pfx + k, v))
                else:
                    walk(v, pfx + k + '.')
walk(d)
" 2>/dev/null || c_dim "（解析失败）"

echo ""
echo "════════════════════════════════════════════════════════════════"
if [ "$FAIL" = "0" ]; then
    echo " ✅ 完成：模型服务已停，控制层正常 —— 可以用来演示/造异常了"
    echo ""
    echo "  接下来可以做的："
    echo "    · 看控制层怎么报： bash serve.sh status"
    echo "    · 看网页顶部状态条： http://localhost:8000"
    echo "    · 发一条对话请求，看它怎么失败（应为 502 upstream_failed）"
    echo "    · 把服务起回来： bash serve.sh model"
else
    echo " ⚠️ 自检有未通过项，见上面"
fi
echo "════════════════════════════════════════════════════════════════"
exit "$FAIL"
