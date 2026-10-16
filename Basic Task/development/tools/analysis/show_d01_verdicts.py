#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看 D01 的逐运行判定：哪些真被判 stale，理由是什么。"""
import json
import re
from pathlib import Path

ev = json.loads(Path("results/office-eval.json").read_text(encoding="utf-8"))
print("=" * 90)
print(" D01 逐运行")
print("=" * 90)
for rid, v in ev.items():
    c = v["tasks"].get("D01") or {}
    print("  %-36s %-16s 命中 %d/%d" % (
        rid, c.get("verdict"), len(c.get("hit") or []),
        len(c.get("hit") or []) + len(c.get("miss") or [])))
    for m in (c.get("miss") or []):
        print("       漏：%s" % m["item"])
    for s in (c.get("stale") or []):
        print("       ⚠️ %s" % s)

# 看那 3 条被判 stale 的，具体匹配到了什么
print()
print("=" * 90)
print(" 被判 stale 的原文匹配")
print("=" * 90)
PAT = (r"(?s)(?<!改为)(?<!调整)(?<!原定)"
       r"演示[^。\n]{0,25}10\s*月\s*16\s*日"
       r"|10\s*月\s*16\s*日[^。\n]{0,25}演示"
       r"(?![^。\n]{0,20}(改为|调整为|原定))")
for rid, v in ev.items():
    c = v["tasks"].get("D01") or {}
    if not c.get("stale"):
        continue
    d = DEV / "results" / rid
    rows = [json.loads(l) for l in
            (d / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    recs = [r for r in rows if r.get("task_id") == "D01"
            and r.get("status") in ("success", "truncated")]
    r = max(recs, key=lambda x: x.get("output_chars") or 0)
    txt = r.get("output_text") or ""
    print("  ── %s ──" % rid)
    for m in re.finditer(PAT, txt):
        print("     匹配到：%r" % m.group(0)[:110])
        s = max(0, m.start() - 60)
        e = min(len(txt), m.end() + 60)
        print("     上下文：…%s…" % txt[s:e].replace("\n", " "))
    print()
