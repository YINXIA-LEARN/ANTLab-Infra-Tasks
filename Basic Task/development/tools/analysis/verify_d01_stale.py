#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核实 D01 被判 stale_decision 的那几条：是真用了旧日期，还是我的正则误报。"""
import json
import re
import sys
from pathlib import Path

PAIRS = [
    ("ctx_opt_1.7-20261016-225813", "1.7B"),
    ("kv_baseline_0.6-20261016-223609", "0.6B/q8_0"),
    ("kv_opt_1.7-20261016-232040", "0.6B/q8_0"),
    ("ctx_baseline_0.6-20261016-222516", "0.6B 对照"),
]

for rid, tag in PAIRS:
    d = DEV / "results" / rid
    rq = d / "requests.jsonl"
    if not rq.is_file():
        continue
    rows = [json.loads(l) for l in
            rq.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [r for r in rows if r.get("task_id") == "D01"
            and r.get("status") in ("success", "truncated")]
    if not recs:
        continue
    r = max(recs, key=lambda x: x.get("output_chars") or 0)
    txt = r.get("output_text") or ""
    print("=" * 88)
    print(" %s   (%s)" % (rid, tag))
    print("=" * 88)
    # 所有出现 "10 月 16 日" 的地方，带上下文
    for m in re.finditer(r"10\s*月\s*16\s*日", txt):
        s = max(0, m.start() - 45)
        e = min(len(txt), m.end() + 45)
        ctx = txt[s:e].replace("\n", " ")
        print("   …%s…" % ctx)
    # 演示日期到底写的是哪天
    for m in re.finditer(r"演示[^。\n]{0,40}", txt):
        print("   [演示] %s" % m.group(0).strip()[:80])
    print()
