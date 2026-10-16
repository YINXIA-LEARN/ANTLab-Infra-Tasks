#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核实 ctx16k-q80-06b-r1 的 P03 到底怎么处理空输入的。"""
import importlib.util
import json
import re
import tempfile
from pathlib import Path

RID = "ctx16k-q80-06b-r1-20261016-223609"
d = DEV / "results" / RID
rows = [json.loads(l) for l in
        (d / "requests.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
recs = [r for r in rows if r.get("task_id") == "P03"
        and r.get("status") in ("success", "truncated")]
r = max(recs, key=lambda x: x.get("output_chars") or 0)
txt = r.get("output_text") or ""
print("状态=%s out_tokens=%s chars=%d finish=%s" % (
    r.get("status"), r.get("output_tokens"), len(txt), r.get("finish_reason")))
print()
print("=" * 90)
print(" P03 输出全文")
print("=" * 90)
print(txt)
print()

FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)```", re.S)
blocks = [(m.group(1) or "?", m.group(2)) for m in FENCE.finditer(txt)]
print("=" * 90)
print(" 代码块 %d 个，语言 %s" % (len(blocks), [b[0] for b in blocks]))
print("=" * 90)
py = [b for b in blocks if b[0].lower() in ("python", "py", "python3")]
best = max(py or blocks, key=lambda b: len(b[1]))
print(best[1])
print()
print("=" * 90)
print(" 真跑空输入")
print("=" * 90)
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "m.py"
    f.write_text(best[1], encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("m", str(f))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        fn = getattr(m, "aggregate_by_owner", None)
        print("  aggregate_by_owner 存在:", fn is not None)
        if fn:
            for name, inp in [("空输入", []),
                              ("单条", [{"owner": "a", "status": "open"}]),
                              ("CLOSED 大写", [{"owner": "a", "status": "CLOSED"}]),
                              ("owner 缺失", [{"status": "open"}])]:
                try:
                    got = fn(json.loads(json.dumps(inp)))
                    print("   %-12s → %r" % (name, got))
                except Exception as e:
                    print("   %-12s → 抛异常 %s: %s" % (name, type(e).__name__, e))
    except Exception as e:
        print("  import 失败:", type(e).__name__, e)
