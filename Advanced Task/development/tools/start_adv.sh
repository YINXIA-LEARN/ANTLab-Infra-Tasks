#!/usr/bin/env bash
# 启动进阶项目：llama-server(:8092) + 缓存代理(:8091)
#
# ══════════════════════════════════════════════════════════════════════════
#  ⚠️ 默认是【独占模式】
# ══════════════════════════════════════════════════════════════════════════
#  基础用 8080(模型) + 8000(控制层)，进阶用 8092 + 8091 —— 端口本不冲突，
#  两个 llama-server 实测也能同时跑（7542/8151 MiB）。
#  但共存只剩约 270 MiB 余量，一旦一方压满，驱动可能把数据换到主机内存，
#  延迟暴涨且不报错；跑基准测试时数据会失真。
#
#  所以默认独占：启动前先停掉基础项目，并【校验】端口真的释放了。
#
#  用法：
#    bash tools/start_adv.sh              独占（默认，推荐）
#    bash tools/start_adv.sh --coexist    不碰基础项目（有 OOM 风险）
#    bash tools/start_adv.sh --help       看这段说明
# ══════════════════════════════════════════════════════════════════════════
set -u

MODE="exclusive"
case "${1:-}" in
    --coexist)   MODE="coexist" ;;
    --exclusive|"") MODE="exclusive" ;;
    -h|--help)   sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（可用 --coexist / --help）"; exit 2 ;;
esac

DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASIC="$HOME/antlab/ANTLab-Infra-Tasks/Basic Task/development"
RUN="$DEV/.runtime"
LOGS="$DEV/logs"
CFG="$DEV/configs/cache.json"
mkdir -p "$RUN" "$LOGS"

read_cfg() { python3 -c "import json;print(json.load(open('$CFG'))['$1'])"; }
L2_DIR=$(read_cfg l2_dir)
NP=$(read_cfg num_slots)
CTX=$(read_cfg server_ctx)
PPORT=$(read_cfg proxy_port)
UPORT=8092
UPSTREAM="http://127.0.0.1:$UPORT"

MODEL="${ANTLAB_MODEL:-$HOME/antlab/models/Qwen3-0.6B-Q8_0.gguf}"
BIN="${ANTLAB_LLAMA_SERVER:-$HOME/antlab/src/llama.cpp/build/bin/llama-server}"
[ -x "$BIN" ]   || { echo "❌ 找不到 llama-server: $BIN"; exit 1; }
[ -f "$MODEL" ] || { echo "❌ 找不到模型: $MODEL"; exit 1; }

# 端口上有没有人在听（用 curl，不依赖 ss/netstat）
port_up() {
    local p="$1"
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/"           2>/dev/null && return 0
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/health"    2>/dev/null && return 0
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/api/status" 2>/dev/null && return 0
    return 1
}

echo "════════ 启动进阶项目（模式：$MODE）════════"

# ── 0. 先停自己 ──
bash "$DEV/tools/stop_adv.sh" >/dev/null 2>&1

# ── 1. 独占：停掉基础项目并校验 ──
if [ "$MODE" = "exclusive" ]; then
    if port_up 8000 || port_up 8080; then
        echo "  ① 检测到基础项目在运行 → 独占模式，先停掉它"
        if [ -f "$BASIC/src/serve.sh" ]; then
            ( cd "$BASIC/src" && bash serve.sh stop ) 2>&1 \
                | grep -E '停止|已停|stop|Stopped' | tail -3 | sed 's/^/     /'
        fi
        pkill -f 'llama-server.*--port 8080' 2>/dev/null
        for i in $(seq 1 40); do
            if ! port_up 8000 && ! port_up 8080; then break; fi
            sleep 0.5
        done
        if port_up 8080 || port_up 8000; then
            echo "     ❌ 8000/8080 仍被占用，无法独占启动"
            echo "        手动检查： bash \"$BASIC/src/serve.sh\" status"
            exit 1
        fi
        echo "     ✅ 基础项目已停，8000 / 8080 已释放"
    else
        echo "  ① 基础项目没在运行，直接开始"
    fi
else
    echo "  ① --coexist：不动基础项目"
    if port_up 8080; then
        echo "     ⚠️ 基础项目在跑，两边共用显存，实测余量约 270 MiB；"
        echo "        跑基准测试数据会失真，建议改用独占模式。"
    fi
fi

mkdir -p "$L2_DIR"

# ── 2. 起 llama-server（8092）──
echo ""
echo "  ② 启动 llama-server :$UPORT  (-c $CTX -np $NP, --cache-ram 0)"
echo "     save 目录 = $L2_DIR  （tmpfs = L2 内存层）"
setsid --fork "$BIN" \
  -m "$MODEL" -ngl 99 -c "$CTX" -np "$NP" \
  --cache-ram 0 \
  --slot-save-path "$L2_DIR/" \
  --host 127.0.0.1 --port "$UPORT" \
  > "$LOGS/llama-server.log" 2>&1 < /dev/null

code=""
for i in $(seq 1 240); do
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$UPORT/health" 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 0.5
done
if [ "$code" != "200" ]; then
    echo "     ❌ llama-server 起不来（HTTP $code）"
    tail -25 "$LOGS/llama-server.log" | sed 's/^/       /'
    exit 1
fi
UPID=$(pgrep -f "llama-server.*--port $UPORT" | head -1)
echo "$UPID" > "$RUN/llama.pid"
echo "     ✅ 就绪 (pid=$UPID)"

# ── 3. 起代理（8091）──
echo ""
echo "  ③ 启动缓存代理 :$PPORT"
cd "$DEV"
PROXY_UPSTREAM="$UPSTREAM" setsid --fork python3 -u proxy/proxy.py \
  --config "$CFG" --dev-dir "$DEV" > "$LOGS/proxy.log" 2>&1 < /dev/null

code=""
for i in $(seq 1 80); do
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PPORT/health" 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 0.3
done
if [ "$code" != "200" ]; then
    echo "     ❌ 代理起不来（HTTP $code）"
    tail -30 "$LOGS/proxy.log" | sed 's/^/       /'
    exit 1
fi
PPID2=$(pgrep -f "proxy/proxy.py.*--dev-dir" | head -1)
echo "$PPID2" > "$RUN/proxy.pid"
echo "     ✅ 就绪 (pid=$PPID2)"

# ── 4. 小结 ──
echo ""
echo "══════════════════════════════════════════════════"
echo " 进阶面板   http://127.0.0.1:$PPORT/"
echo " 模型服务   $UPSTREAM"
echo " 显存       $(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
echo " L2 用量    $(df -h "$L2_DIR" 2>/dev/null | awk 'NR==2{print $3" / "$2}')"
if [ "$MODE" = "exclusive" ]; then
    echo " 基础项目   已停止（切回去： bash tools/use.sh basic）"
fi
echo "══════════════════════════════════════════════════"
