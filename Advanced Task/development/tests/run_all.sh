#!/usr/bin/env bash
# 跑全部测试。用法：
#   bash tests/run_all.sh           # 全部
#   bash tests/run_all.sh unit      # 只跑不依赖服务的单元测试
set -u
DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEV" || exit 1
MODE="${1:-all}"

UNIT="test_key test_slotfile test_index test_migrate test_evict"
E2E="test_e2e"

pass=0; fail=0; failed_names=""
run_one() {
    local name="$1"
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    printf  "║ %-56s ║\n" "$name"
    echo "╚══════════════════════════════════════════════════════════╝"
    if python3 "tests/$name.py"; then
        pass=$((pass+1))
    else
        fail=$((fail+1)); failed_names="$failed_names $name"
    fi
}

for t in $UNIT; do run_one "$t"; done
if [ "$MODE" != "unit" ]; then
    for t in $E2E; do run_one "$t"; done
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  测试文件：通过 $pass / 失败 $fail"
[ -n "$failed_names" ] && echo "  失败：$failed_names"
echo "════════════════════════════════════════════════════════════"
[ "$fail" = "0" ] || exit 1
