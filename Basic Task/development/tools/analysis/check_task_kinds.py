#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
先验证 test_08 的判定对不对：它到底把哪些任务当成了"代码"？

⚠️ 这一步很重要：如果检测手段本身错了（比如把 P03 的表格当 Python 编译），
   那么它报的"语法失败"就是假阳性，据此写报告会得出错误结论。
"""
import json
from pathlib import Path

D = Path(".")
TASKS = D / "src" / "common" / "tasks.py"


def main():
    # ① 任务定义里，哪些是编程类？
    print("=" * 92)
    print(" 一、任务定义里怎么区分任务类型")
    print("=" * 92)
    src = TASKS.read_text(encoding="utf-8")
    import re
    # 找 kind / type / category 之类的字段
    for pat in (r'"kind"\s*:\s*"([^"]+)"', r'"type"\s*:\s*"([^"]+)"',
                r'"category"\s*:\s*"([^"]+)"', r'"output_kind"\s*:\s*"([^"]+)"'):
        hits = re.findall(pat, src)
        if hits:
            print("  字段 %s → %s" % (pat, sorted(set(hits))))
    # 直接 import 看结构
    import sys
    sys.path.insert(0, str((D / "src").resolve()))
    try:
        from common import tasks as T
        fn = getattr(T, "TASKS", None) or getattr(T, "ALL_TASKS", None)
        if isinstance(fn, dict):
            items = fn.items()
        elif isinstance(fn, list):
            items = [(t.get("id"), t) for t in fn]
        else:
            items = []
        print()
        print("  ── 每个任务的字段 ──")
        for tid, t in items:
            keys = {k: v for k, v in t.items()
                    if k not in ("prompt", "messages", "system", "text")
                    and not isinstance(v, (dict, list))}
            print("   %-5s %s" % (tid, json.dumps(keys, ensure_ascii=False)[:150]))
    except Exception as e:
        print("  （import 失败：%s）" % e)

    # ② test_08 到底检查了哪些任务
    print()
    print("=" * 92)
    print(" 二、test_08 检查了哪些任务")
    print("=" * 92)
    t8 = (D / "tests" / "core" / "test_08_output_code.py").read_text(encoding="utf-8")
    for ln in t8.splitlines():
        if "TASKS" in ln or "task_ids" in ln or "CODE" in ln:
            print("   " + ln.rstrip()[:150])

    # ③ 拿一个真实输出看看：C01 和 P03 的输出长什么样
    print()
    print("=" * 92)
    print(" 三、真实输出长什么样（判断该不该送去编译）")
    print("=" * 92)
    rd = D / "results" / "kv_baseline_0.6-20261016-223609"
    rows = [json.loads(l) for l in
            (rd / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    for tid in ("P03", "P04", "C01", "C02"):
        r = next((x for x in rows if x.get("task_id") == tid
                  and x.get("status") in ("success", "truncated")), None)
        if not r:
            continue
        txt = r.get("output_text") or ""
        print("  ── %s（%s，%d 字符）──" % (tid, r.get("status"), len(txt)))
        print("     开头 200 字符：")
        for line in txt[:200].splitlines()[:6]:
            print("       | %s" % line[:90])
        has_fence = "```" in txt
        has_def = "\ndef " in txt or txt.startswith("def ")
        has_import = "\nimport " in txt or "from " in txt
        print("     含 ```围栏=%s  含 def=%s  含 import=%s" % (
            has_fence, has_def, has_import))
        print()


if __name__ == "__main__":
    main()
