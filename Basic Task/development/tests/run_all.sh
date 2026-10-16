#!/usr/bin/env bash
# 跑 tests/ 下的全部测试，汇总退出码
set -u
cd "$HOME/antlab/ANTLab-Infra-Tasks/Basic Task/development/tests" || exit 1

echo "══════════════════════════════════════════════════════════════════════"
echo " tests/ 全部测试"
echo "══════════════════════════════════════════════════════════════════════"
echo ""
ls -1 *.py | sed 's/^/  /'
echo ""

FAIL=0
PASS=0
for f in test_*.py; do
    out=$(python3 "$f" 2>&1)
    code=$?
    n_ok=$(printf '%s' "$out" | grep -oE '通过：[0-9]+' | head -1 | grep -oE '[0-9]+')
    n_fail=$(printf '%s' "$out" | grep -oE '失败：[0-9]+' | head -1 | grep -oE '[0-9]+')
    if [ "$code" = "0" ]; then
        PASS=$((PASS+1))
        printf '  ✅ %-34s 退出码 0   通过 %s 项\n' "$f" "${n_ok:-?}"
    else
        FAIL=$((FAIL+1))
        printf '  ❌ %-34s 退出码 %-3s 通过 %s / 失败 %s\n' "$f" "$code" "${n_ok:-?}" "${n_fail:-?}"
        printf '%s\n' "$out" | sed -n '/❌ 失败/,/结果：/p' | head -20 | sed 's/^/       /'
    fi
done

echo ""
echo "══════════════════════════════════════════════════════════════════════"
printf ' 通过 %d 个，失败 %d 个\n' "$PASS" "$FAIL"
echo "══════════════════════════════════════════════════════════════════════"
exit $FAIL
