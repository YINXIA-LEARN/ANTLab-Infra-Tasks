#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  start_model_service.sh —— 只启动模型服务，控制层不受影响
#
#  与 stop_model_service.sh 配套。
#
#  ⚠️ 与 `serve.sh model` 的区别（这是本脚本存在的理由）：
#
#     `serve.sh model` 的语义是【重启】—— 它先停再起。
#     实测那个空窗：
#         +1s  health=000   +2s  health=000   +3s  health=503   +4s  health=200
#
#     于是有两个场景不该用它：
#       ① 服务已经在跑，你只是想确认/确保它在 → 用它等于白白中断 3 秒
#       ② 正在跑测试时 → 中断会让那一轮请求集体失败
#
#     本脚本是【幂等】的：
#        已经在跑 → 什么都不做（除非加 --force）
#        没在跑   → 按注册表参数启动，并等到真的就绪才返回
#
#  ⚠️ 参数来源：configs/models.json 注册表（与 serve.sh 同一份）。
#     不在本脚本里硬编码模型/上下文 —— 否则两处配置打架，
#     界面显示 ctx=16384 而实际跑 4096 这类事故就是这么来的。
#
#  用法：
#     bash start_model_service.sh              # 已跑就跳过；没跑就启动
#     bash start_model_service.sh --force      # 强制重启（等价 serve.sh model）
#     bash start_model_service.sh --status     # 只看状态
#     bash start_model_service.sh --wait 120   # 自定义就绪等待上限（秒）
# ══════════════════════════════════════════════════════════════════════════
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$DIR/.." && pwd)"                 # src/
LOGDIR="$SRC/logs"
MODEL_PIDFILE="$LOGDIR/model-server.pid"
CONTROL_PIDFILE="$LOGDIR/control.pid"
REGISTRY="$SRC/../configs/models.json"
PORT="${ANTLAB_MODEL_PORT:-8080}"

FORCE=0
WAIT=90
MODE="start"
while [ $# -gt 0 ]; do
    case "$1" in
        --force)  FORCE=1 ;;
        --status) MODE="status" ;;
        --wait)   shift; WAIT="${1:-90}" ;;
        -h|--help)
            sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$1（试 --help）" >&2; exit 2 ;;
    esac
    shift
done

c_ok()   { printf '\033[32m  ✅ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m  ⚠️ %s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m  ❌ %s\033[0m\n' "$*"; }
c_dim()  { printf '     %s\n' "$*"; }

model_pids()    { pgrep -f "[l]lama-server.*--port $PORT" 2>/dev/null; }
control_pids()  { pgrep -f "[p]ython3 run.py" 2>/dev/null; }
model_health()  {
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
    local mp cp
    mp="$(model_pids | tr '\n' ' ')"
    cp="$(control_pids | tr '\n' ' ')"
    printf '  ① 模型服务  %s   PID: %s   health: %s\n' \
        "$([ -n "$mp" ] && echo 运行中 || echo 未运行)" "${mp:-—}" "$(model_health)"
    printf '  ② 控制层    %s   PID: %s   /api/status: %s\n' \
        "$([ -n "$cp" ] && echo 运行中 || echo 未运行)" "${cp:-—}" "$(control_health)"
}

echo "════════════════════════════════════════════════════════════════"
echo " 启动模型服务（不动控制层）"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "── 当前状态 ──"
show_status
echo ""

if [ "$MODE" = "status" ]; then
    echo "（--status 模式，不做任何改动）"
    exit 0
fi

# ── 幂等：已经在跑就跳过 ──────────────────────────────────────────────────
if [ -n "$(model_pids)" ] && [ "$FORCE" != "1" ]; then
    H="$(model_health)"
    if [ "$H" = "200" ]; then
        c_ok "模型服务已经在跑且健康（health=200）—— 什么都不做"
        c_dim "如果你想强制重启，用： bash tools/start_model_service.sh --force"
        c_dim "（注意：重启会有约 3 秒空窗，期间推理请求会失败）"
        exit 0
    fi
    c_warn "进程在，但 health=$H —— 可能正在加载，等它一会儿"
fi

if [ "$FORCE" = "1" ] && [ -n "$(model_pids)" ]; then
    c_warn "--force：先停掉现有的模型服务"
    bash "$DIR/stop_model_service.sh" --force >/dev/null 2>&1 || true
    sleep 1
fi

# ── 读注册表，确认要起哪个模型、什么参数 ──────────────────────────────────
if [ ! -f "$REGISTRY" ]; then
    c_err "读不到注册表：$REGISTRY"
    c_dim "serve.sh 会用同一份文件，所以这里失败它也会失败。"
    exit 1
fi
MODEL_NAME="$(python3 -c "
import json, sys
try:
    d = json.load(open('$REGISTRY', encoding='utf-8'))
    print((d.get('active') or d.get('default_active') or ''))
except Exception:
    print('')
" 2>/dev/null)"

c_dim "注册表：$REGISTRY"
c_dim "当前模型 id：${MODEL_NAME:-（读不到）}"
echo ""

# ── 启动 ──────────────────────────────────────────────────────────────────
# ⚠️ 直接把启动交给 serve.sh model —— 不在这里重复实现一遍启停逻辑。
#    服务参数怎么拼、PID 文件怎么写、就绪怎么判，全在 serve.sh 里，
#    复制一份迟早会两处不一致（本项目在 models.json 上就栽过一次）。
echo "── 启动（调用 serve.sh model，它只动模型服务）──"
cd "$SRC" || exit 1
bash serve.sh model 2>&1 | sed 's/^/  /'
RC=${PIPESTATUS[0]}

# ── 自检 ──────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo " 自检"
echo "════════════════════════════════════════════════════════════════"

FAIL=0
T0=$(date +%s)
while [ $(( $(date +%s) - T0 )) -lt "$WAIT" ]; do
    [ "$(model_health)" = "200" ] && break
    sleep 1
done
MH="$(model_health)"

if [ "$MH" = "200" ]; then
    c_ok "模型服务已就绪（health=200，PID $(model_pids | tr '\n' ' '))"
else
    c_err "等待 ${WAIT} 秒后模型服务仍未就绪（health=$MH）"
    c_dim "排查： bash serve.sh status / tail -50 $LOGDIR/model-server.log"
    FAIL=1
fi

CH="$(control_health)"
if [ "$CH" = "200" ]; then
    c_ok "控制层未受影响（/api/status=200，PID $(control_pids | tr '\n' ' '))"
else
    c_warn "控制层此刻响应 $CH —— 如果它本来是停的，这属正常"
fi

echo ""
echo "── 最终状态 ──"
show_status

if [ "$FAIL" = "0" ]; then
    echo ""
    echo " ✅ 完成：模型服务已起，控制层没被碰"
    echo "    网页： http://localhost:8000"
    echo "    停它： bash tools/stop_model_service.sh"
fi
echo "════════════════════════════════════════════════════════════════"
exit "$FAIL"
