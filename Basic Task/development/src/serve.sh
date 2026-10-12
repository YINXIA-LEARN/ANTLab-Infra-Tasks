#!/usr/bin/env bash
# ============================================================================
#  serve.sh —— 一键管理这两个服务
#
#  这套系统有两个独立的进程，容易混：
#    ┌─────────────────────────────────────────────────────────────┐
#    │ ① 模型服务 llama-server                                      │
#    │    监听 127.0.0.1:8080   ← 只给本机，外面访问不到             │
#    │    作用：真正加载模型、跑推理                                  │
#    ├─────────────────────────────────────────────────────────────┤
#    │ ② 控制层 （自己写的 Python 程序）                            │
#    │    监听 0.0.0.0:8000     ← Windows 浏览器访问这个             │
#    │    作用：提供页面 + 转发请求给 ①                              │
#    └─────────────────────────────────────────────────────────────┘
#
#  用法：
#     bash serve.sh start      启动两个服务（模型服务已在跑就跳过）
#     bash serve.sh stop       停止两个服务
#     bash serve.sh restart    重启控制层（改代码后用这个；模型服务不动）
#     bash serve.sh status     看两个服务的状态
#     bash serve.sh logs       实时看控制层日志
#     bash serve.sh model      只重启模型服务（换模型/改参数时用）
#
#  ⭐ 模型参数从注册表读（configs/models.json）
#     为什么：以前这里的 -c 是硬编码的，跟注册表不一致 ——
#     界面显示 ctx=16384，实际服务跑 4096，属于"说了不算"。
#     现在以注册表为唯一来源，避免两处配置打架。
#
#  ⚠️ 注册表的路径是 "$DIR/../configs/models.json"，注意那个 ../
#     因为迁移到 development/ 之后，目录布局是：
#         development/configs/models.json     ← 配置（进 git）
#         development/src/serve.sh            ← 本脚本
#         development/src/logs/               ← 运行痕迹
#     早先配置在 src/ 下，迁移后配置上移了一层。这里必须跟着改，
#     否则报"读不到注册表"。
#
# ============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

LOGDIR="$DIR/logs"
mkdir -p "$LOGDIR"

MODEL_BIN="$HOME/antlab/src/llama.cpp/build/bin/llama-server"
MODEL_HOST="127.0.0.1"
MODEL_PORT=8080
CONTROL_PORT=8000
MODEL_LOG="$LOGDIR/model-server.log"
MODEL_PIDFILE="$LOGDIR/model-server.pid"
CONTROL_PIDFILE="$LOGDIR/control.pid"
CONTROL_LOG="$LOGDIR/control.log"

# ── 从注册表解析当前模型（唯一配置来源）──────────────────────────────────
# 输出四行：文件名 / ngl / ctx / 模型绝对路径（空=没找到）
#
# ⚠️ 关于 active 与 default_active：
#    注册表里现在存的是 default_active（默认值），运行中选择存在
#    .runtime/active-model.json。这个函数是"重启模型服务"用的，
#    语义上应该用【注册表的默认值】—— 因为切换模型时会由
#    model_manager 自己带上参数重启，不经过这里。
#    两个键都认，是为了兼容还没改名的旧注册表。
read_registry() {
  python3 - "$DIR/../configs/models.json" <<'PY' 2>/dev/null || true
import json, os, sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.is_file():
    sys.exit(1)
d = json.loads(p.read_text(encoding="utf-8"))
# 兼容新旧两种键名
active = d.get("default_active") or d.get("active")
entry = next((m for m in d.get("models", []) if m["id"] == active), None)
if entry is None:
    # 连默认值都没写，退回列表第一个
    entry = (d.get("models") or [None])[0]
if entry is None:
    sys.exit(1)

cands = [
    os.environ.get("ANTLAB_MODEL_DIR"),
    str(Path.home() / "antlab" / "models"),
    # 注册表在 <development>/configs/ 下，所以 parent.parent 就是 development/
    str(p.resolve().parent.parent / "models"),
    str(Path.cwd() / "models"),
    "/models",
]
found = ""
for c in cands:
    if c and (Path(c) / entry["file"]).is_file():
        found = str(Path(c) / entry["file"])
        break

sa = entry.get("server_args") or {}
print(entry["file"])
print(sa.get("ngl", 99))
print(sa.get("ctx", 4096))
print(found)
PY
}

# ── 工具函数 ───────────────────────────────────────────────────────────────
c_ok()   { printf '\033[32m%s\033[0m\n' "$1"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$1"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$1"; }
c_dim()  { printf '\033[90m%s\033[0m\n' "$1"; }

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
port_listening() { ss -tln 2>/dev/null | grep -q ":$1 "; }

# 严格就绪判定：解析 JSON，不做字符串匹配。
# ⚠️ 两个服务的响应结构不同，必须分别判断：
#     模型服务 /health      → {"status":"ok"}         顶层字段
#     控制层   /api/status  → {"control":{"ok":true}}  嵌在子对象里
wait_model_ready() {
  local timeout="${1:-60}" i
  for ((i = 0; i < timeout; i++)); do
    if curl -sS -m 2 "http://$MODEL_HOST:$MODEL_PORT/health" 2>/dev/null \
       | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('status')=='ok' else 1)" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_control_ready() {
  local timeout="${1:-30}" i
  for ((i = 0; i < timeout; i++)); do
    if curl -sS -m 2 "http://127.0.0.1:$CONTROL_PORT/api/status" 2>/dev/null \
       | python3 -c "import sys,json; sys.exit(0 if (json.load(sys.stdin).get('control') or {}).get('ok') else 1)" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# ── 模型服务 ───────────────────────────────────────────────────────────────
model_status() {
  if port_listening "$MODEL_PORT"; then
    local n
    n=$(curl -sS -m 3 "http://$MODEL_HOST:$MODEL_PORT/health" 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo '?')
    echo "运行中（health=$n）"
  else
    echo "未运行"
  fi
}

model_start() {
  if port_listening "$MODEL_PORT"; then
    c_dim "  模型服务已在运行，跳过"
    return 0
  fi
  if [ ! -x "$MODEL_BIN" ]; then
    c_err "  ❌ 找不到 llama-server：$MODEL_BIN"
    return 1
  fi

  local reg
  reg=$(read_registry)
  if [ -z "$reg" ]; then
    c_err "  ❌ 读不到注册表 configs/models.json（或 active 无效）"
    return 1
  fi
  local FILE NGL CTX MPATH
  FILE=$(echo "$reg" | sed -n '1p')
  NGL=$(echo "$reg"  | sed -n '2p')
  CTX=$(echo "$reg"  | sed -n '3p')
  MPATH=$(echo "$reg" | sed -n '4p')

  if [ -z "$MPATH" ]; then
    c_err "  ❌ 找不到模型文件：$FILE"
    c_err "     设 ANTLAB_MODEL_DIR，或把文件放到 ~/antlab/models/"
    return 1
  fi

  echo "  启动模型服务：$FILE（ngl=$NGL, ctx=$CTX）"
  # ⭐ 两个为"提交标准要求的证据"而加的参数：
  #
  #   -v / --verbose
  #       默认 verbosity=3 时，llama-server 只打 9 行启动日志，
  #       里面【完全没有】GPU 信息 —— 搜 offloaded / n_gpu_layers /
  #       CUDA / KV self 全是 0 命中。
  #       题目要求「提供模型计算层和 KV 在 GPU 的日志证据」，
  #       加 -v 后日志约 4300 行，才有这几行关键证据：
  #           load_tensors: offloaded 29/29 layers to GPU
  #           load_tensors: layer N assigned to device CUDA0
  #           llama_kv_cache: layer N: dev = CUDA0
  #           llama_kv_cache: CUDA0 KV buffer size = 224.00 MiB
  #       代价只是日志变长（每次启动约 4300 行）。
  #
  #   --cache-ram 0
  #       关掉"主机 RAM prompt cache"。默认是开的（上限 8192 MiB），
  #       会把【空闲 slot 的 KV 状态】搬到主机内存，下次用再搬回来。
  #       题目第 17 行要求「关闭主机 RAM prompt cache，使基础阶段的
  #       缓存工作集驻留显存」，且第 18 行把"KV 卸载到主机内存"
  #       划归进阶方向。所以基础阶段的基线必须是纯显存内推理。
  #       ⚠️ 只关主机搬运，不影响：
  #          · KV 本身仍在显存（CUDA0 KV buffer size 不变）
  #          · 计算层的前缀复用（--cache-prompt 仍为 enabled）
  nohup "$MODEL_BIN" -m "$MPATH" \
      -ngl "$NGL" -c "$CTX" \
      -v --cache-ram 0 \
      --host "$MODEL_HOST" --port "$MODEL_PORT" \
      >> "$MODEL_LOG" 2>&1 &
  echo $! > "$MODEL_PIDFILE"

  if wait_model_ready 60; then
    local slot
    slot=$(curl -sS -m 3 "http://$MODEL_HOST:$MODEL_PORT/props" 2>/dev/null \
      | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('default_generation_settings') or {}).get('n_ctx'))" 2>/dev/null)
    c_ok "  ✅ 模型服务就绪（PID $(cat "$MODEL_PIDFILE")，每槽 ctx=$slot）"
    return 0
  fi
  c_err "  ❌ 模型服务 60 秒内未就绪，日志尾部："
  tail -12 "$MODEL_LOG"
  return 1
}

model_stop() {
  local pid=""
  [ -f "$MODEL_PIDFILE" ] && pid=$(cat "$MODEL_PIDFILE" 2>/dev/null || echo "")

  if pid_alive "$pid"; then
    kill "$pid" 2>/dev/null
    sleep 2
    pid_alive "$pid" && kill -9 "$pid" 2>/dev/null
  fi
  if port_listening "$MODEL_PORT"; then
    pkill -f "llama-server" 2>/dev/null
    sleep 2
  fi
  rm -f "$MODEL_PIDFILE"

  if port_listening "$MODEL_PORT"; then
    c_warn "  ⚠️ 端口 $MODEL_PORT 仍被占用"
    return 1
  fi
  c_ok "  ✅ 模型服务已停止，显存已释放"
}

# ── 控制层 ─────────────────────────────────────────────────────────────────
control_status() {
  if port_listening "$CONTROL_PORT"; then
    local pid=""
    [ -f "$CONTROL_PIDFILE" ] && pid=$(cat "$CONTROL_PIDFILE" 2>/dev/null || echo "")
    echo "运行中（PID ${pid:-未知}）"
  else
    echo "未运行"
  fi
}

control_start() {
  if port_listening "$CONTROL_PORT"; then
    c_dim "  控制层已在运行，跳过"
    return 0
  fi
  echo "  启动控制层…"
  # ⚠️ 用 >> 追加，不要用 > 覆盖。
  #    覆盖会让【历史日志全部消失】，出问题后无法回溯。
  #    我踩过这个坑：一次模型切换的记录被随后的 restart 清掉了，
  #    排查时看到"日志里没有切换记录"，误判成"切换没执行"，
  #    还据此怀疑了 PID 机制。日志文件大了可以手动归档，
  #    但绝不能让程序自己丢证据。
  nohup python3 run.py >> "$CONTROL_LOG" 2>&1 &
  echo $! > "$CONTROL_PIDFILE"

  if wait_control_ready 30; then
    c_ok "  ✅ 控制层就绪（PID $(cat "$CONTROL_PIDFILE")）"
    return 0
  fi
  c_err "  ❌ 控制层 30 秒内未就绪，日志尾部："
  tail -15 "$CONTROL_LOG"
  return 1
}

control_stop() {
  local pid=""
  [ -f "$CONTROL_PIDFILE" ] && pid=$(cat "$CONTROL_PIDFILE" 2>/dev/null || echo "")

  if pid_alive "$pid"; then
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 10); do
      pid_alive "$pid" || break
      sleep 0.5
    done
    pid_alive "$pid" && kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$CONTROL_PIDFILE"

  if port_listening "$CONTROL_PORT"; then
    pkill -f "python3 run.py" 2>/dev/null
    sleep 1
  fi

  if port_listening "$CONTROL_PORT"; then
    c_warn "  ⚠️ 端口 $CONTROL_PORT 仍被占用"
    return 1
  fi
  c_ok "  ✅ 控制层已停止"
}

# ── 状态总览 ───────────────────────────────────────────────────────────────
do_status() {
  echo "════════════════════════════════════════════════════"
  echo " ANTLab 控制台 —— 服务状态"
  echo "════════════════════════════════════════════════════"
  printf " ① 模型服务  %-38s\n" "$(model_status)"
  printf "    监听      %s\n" "$MODEL_HOST:$MODEL_PORT"
  printf "    注册表    %s\n" "$(read_registry | sed -n '1p')"
  echo ""
  printf " ② 控制层    %-38s\n" "$(control_status)"
  printf "    监听      0.0.0.0:%s\n" "$CONTROL_PORT"
  echo ""

  if port_listening "$MODEL_PORT"; then
    curl -sS -m 3 "http://$MODEL_HOST:$MODEL_PORT/props" 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    dgs=d.get('default_generation_settings') or {}
    print('    实际模型  %s | %s | 每槽 ctx=%s | slots=%s' % (
        (d.get('model_path') or '').rsplit('/',1)[-1],
        d.get('model_ftype'), dgs.get('n_ctx'), d.get('total_slots')))
except Exception:
    print('    （读取失败）')
" 2>/dev/null
  fi

  echo ""
  echo "  显存: $(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null || echo 不可用)"
  echo ""
  echo "  浏览器访问: http://localhost:$CONTROL_PORT"
  echo "════════════════════════════════════════════════════"
}

# ── 入口 ───────────────────────────────────────────────────────────────────
case "${1:-}" in
  start)
    echo "启动服务"
    model_start || exit 1
    control_start || exit 1
    echo ""
    do_status
    ;;
  stop)
    echo "停止服务"
    control_stop
    model_stop
    echo ""
    do_status
    ;;
  restart)
    echo "重启控制层（模型服务不动）"
    control_stop
    control_start || exit 1
    echo ""
    do_status
    ;;
  model)
    echo "重启模型服务（按注册表的当前模型与参数）"
    model_stop
    model_start || exit 1
    ;;
  status)
    do_status
    ;;
  logs)
    echo "实时日志（Ctrl+C 退出）：$CONTROL_LOG"
    tail -f "$CONTROL_LOG"
    ;;
  *)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -32
    ;;
esac
