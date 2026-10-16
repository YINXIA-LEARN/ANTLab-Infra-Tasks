#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查 ctx16k-q80-06b-r2 那一轮真实的 PID 情况，以及 service-starts.jsonl 的真实字段名。"""
import json
from collections import Counter
from pathlib import Path

D = Path(__file__).resolve().parents[2]
d = D / "results" / "ctx16k-q80-06b-r2-20261016-232040"

print("=" * 92)
print(" 一、requests.jsonl 里出现过的 service_instance_pid")
print("=" * 92)
rows = [json.loads(l) for l in
        (d / "requests.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
pids = Counter(r.get("service_instance_pid") for r in rows)
print("  分布: %s" % dict(pids))
print()
for pid, n in pids.items():
    sub = [r for r in rows if r.get("service_instance_pid") == pid]
    st = Counter(r.get("status") for r in sub)
    tt = sorted((r.get("ttft_ms") or 0) for r in sub)
    med = tt[len(tt) // 2] if tt else 0
    print("  pid=%-10s %3d 条  状态=%s  TTFT中位=%.1f ms" % (pid, n, dict(st), med))

print()
print("=" * 92)
print(" 二、meta.json 的 service_instance")
print("=" * 92)
m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
print("  " + json.dumps(m.get("service_instance"), ensure_ascii=False, indent=2).replace("\n", "\n  "))
print()
print("  meta.json 里跟 pid 有关的其它键：")
for k, v in m.items():
    if "pid" in k.lower() or "service" in k.lower():
        print("    %-24s %s" % (k, json.dumps(v, ensure_ascii=False)[:160]))

print()
print("=" * 92)
print(" 三、summary.json 的 service_instance / by_service_instance")
print("=" * 92)
s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
print("  service_instance     : " + json.dumps(s.get("service_instance"), ensure_ascii=False))
print("  by_service_instance  : " + json.dumps(s.get("by_service_instance"), ensure_ascii=False, indent=2).replace("\n", "\n  "))

print()
print("=" * 92)
print(" 四、★ service-starts.jsonl 的【完整字段】（我之前把字段名猜错了）")
print("=" * 92)
ss = d / "service-starts.jsonl"
for i, l in enumerate(ss.read_text(encoding="utf-8").splitlines(), 1):
    if l.strip():
        e = json.loads(l)
        print("  ── 第 %d 条 ──" % i)
        print("  " + json.dumps(e, ensure_ascii=False, indent=2).replace("\n", "\n  "))

print()
print("=" * 92)
print(" 五、service-events.jsonl（若有）")
print("=" * 92)
for name in ("service-events.jsonl", "recovery-during-run.jsonl"):
    p = d / name
    if p.is_file():
        print("  ── %s ──" % name)
        for l in p.read_text(encoding="utf-8").splitlines()[:12]:
            if l.strip():
                print("    " + json.dumps(json.loads(l), ensure_ascii=False)[:200])
    else:
        print("  （没有 %s）" % name)
