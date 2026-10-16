#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  跑 tests/ 下的全部测试，汇总退出码
#
#  ⚠️ 测试分两处放（2026-10-16 调整）：
#       tests/test_11_service_watch.py   服务监测与自动重启（留在根）
#       tests/core/test_*.py             功能与数据检查
#       tests/_testlib.py                共用骨架，【故意留在根】
#
#     _testlib 不能跟着进 core/ —— 它的 DEV_DIR / RESULTS_DIR 是按
#     自己所在位置向上推算的，挪一层会整体错位，所有读 results/ 的
#     测试都会失灵。core/ 里的测试用两行 sys.path.insert 找到它。
#
#  ⚠️ 所以这里不能用 `for f in test_*.py`（那只覆盖根目录），
#     改用 find 递归找 —— 保证以后再加子目录也不会漏跑。
# ══════════════════════════════════════════════════════════════════════════
set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

echo "══════════════════════════════════════════════════════════════════════"
echo " tests/ 全部测试"
echo "══════════════════════════════════════════════════════════════════════"
echo ""
find . -name 'test_*.py' -not -path './__pycache__/*' | sort | sed 's/^/  /'
echo ""

FAIL=0
PASS=0
while IFS= read -r f; do
    d=$(dirname "$f")
    # ⚠️ 必须 cd 到测试所在目录再跑：测试里的 sys.path 是用
    #    __file__ 算的，虽然理论上与 cwd 无关，但保持一致更稳妥；
    #    而且 core/ 里的测试要靠 parent.parent 找到 _testlib。
    out=$(cd "$d" && python3 "$(basename "$f")" 2>&1)
    code=$?
    n_ok=$(printf '%s' "$out" | grep -oE '通过：[0-9]+' | head -1 | grep -oE '[0-9]+')
    n_fail=$(printf '%s' "$out" | grep -oE '失败：[0-9]+' | head -1 | grep -oE '[0-9]+')
    if [ "$code" = "0" ]; then
        PASS=$((PASS+1))
        printf '  ✅ %-40s 退出码 0   通过 %s 项\n' "${f#./}" "${n_ok:-?}"
    else
        FAIL=$((FAIL+1))
        printf '  ❌ %-40s 退出码 %-3s 通过 %s / 失败 %s\n' "${f#./}" "$code" "${n_ok:-?}" "${n_fail:-?}"
        printf '%s\n' "$out" | sed -n '/❌ 失败/,/结果：/p' | head -20 | sed 's/^/       /'
    fi
done < <(find . -name 'test_*.py' -not -path './__pycache__/*' | sort)

echo ""
echo "══════════════════════════════════════════════════════════════════════"
printf ' 通过 %d 个，失败 %d 个\n' "$PASS" "$FAIL"
echo "══════════════════════════════════════════════════════════════════════"
exit $FAIL
