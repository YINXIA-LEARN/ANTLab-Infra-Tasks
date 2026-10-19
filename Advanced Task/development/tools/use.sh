#!/usr/bin/env bash
# use.sh —— 在基础项目和进阶项目之间【独占切换】
#
# ══════════════════════════════════════════════════════════════════════════
#  为什么需要它
# ══════════════════════════════════════════════════════════════════════════
#  两个项目端口不冲突（基础 8000/8080，进阶 8091/8092），实测也能同时跑，
#  但共存时显存只剩约 270 MiB（7542/8151），跑基准测试数据会失真。
#  所以平时【一次只跑一个】。
#
#  基础项目的脚本不会主动停进阶（那是它的地盘），
#  所以"切回基础"这件事必须由这个脚本替你做。
#
#  用法：
#    bash tools/use.sh adv        切到进阶（独占）
#    bash tools/use.sh basic      切回基础（独占）
#    bash tools/use.sh status     看四个端口 + 显存 + 进程
#    bash tools/use.sh stop-all   全停
# ══════════════════════════════════════════════════════════════════════════
set -u

DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASIC="$HOME/antlab/ANTLab-Infra-Tasks/Basic Task/development"

port_up() {
    local p="$1"
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/"            2>/dev/null && return 0
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/health"     2>/dev/null && return 0
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$p/api/status" 2>/dev/null && return 0
    return 1
}

vram() { nvidia-smi --query-gpu=memory.used,memory.total,memory.free \
             --format=csv,noheader 2>/dev/null; }

show_status() {
    echo "════════ 服务状态 ════════"
    printf '  %-28s %s\n' "基础控制层  :8000" "$(port_up 8000 && echo 在线 || echo 离线)"
    printf '  %-28s %s\n' "基础模型    :8080" "$(port_up 8080 && echo 在线 || echo 离线)"
    printf '  %-28s %s\n' "进阶代理    :8091" "$(port_up 8091 && echo 在线 || echo 离线)"
    printf '  %-28s %s\n' "进阶模型    :8092" "$(port_up 8092 && echo 在线 || echo 离线)"
    echo ""
    echo "  显存  $(vram)"
    echo ""
    echo "  进程："
    pgrep -af 'llama-server' 2>/dev/null | sed 's/^/    /'
    pgrep -af 'proxy/proxy.py' 2>/dev/null | sed 's/^/    /'
    pgrep -af 'run.py' 2>/dev/null | sed 's/^/    /'
    echo "    （没有输出 = 都没跑）"
}

stop_adv() {
    bash "$DEV/tools/stop_adv.sh" >/dev/null 2>&1
}

stop_basic() {
    if [ -f "$BASIC/src/serve.sh" ]; then
        ( cd "$BASIC/src" && bash serve.sh stop ) >/dev/null 2>&1
    fi
    pkill -f 'llama-server.*--port 8080' 2>/dev/null
    for i in $(seq 1 40); do
        if ! port_up 8000 && ! port_up 8080; then break; fi
        sleep 0.5
    done
}

case "${1:-}" in
    adv)
        echo "════════ 切到【进阶项目】（独占）════════"
        stop_basic
        if port_up 8080 || port_up 8000; then
            echo "  ❌ 基础项目没停干净（8000/8080 仍在线），中止"
            exit 1
        fi
        echo "  ✅ 基础项目已停"
        echo ""
        bash "$DEV/tools/start_adv.sh" --exclusive
        ;;
    basic)
        echo "════════ 切到【基础项目】（独占）════════"
        stop_adv
        echo "  ✅ 进阶项目已停"
        if [ ! -f "$BASIC/src/serve.sh" ]; then
            echo "  ❌ 找不到 $BASIC/src/serve.sh"
            exit 1
        fi
        echo ""
        ( cd "$BASIC/src" && bash serve.sh start )
        echo ""
        echo "══════════════════════════════════════════════════"
        echo " 基础网页  http://localhost:8000"
        echo " 显存      $(vram)"
        echo "══════════════════════════════════════════════════"
        ;;
    status)
        show_status
        ;;
    stop-all)
        echo "════════ 全停 ════════"
        stop_adv
        echo "  ✅ 进阶已停"
        stop_basic
        echo "  ✅ 基础已停"
        echo ""
        echo "  显存 $(vram)"
        ;;
    *)
        sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
esac
