#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核实一个疑点：配置里 warmup.n=2，但 summary.json 的 warmup 数组到底几条？

如果有运行只出现 1 条，那是真问题（可能预热只跑了 1 次），
也可能是"第 2 条被截断/失败所以没进数组"。
"""
import json
from pathlib import Path

D = Path(__file__).resolve().parents[2]
R = D / "results"

print("=" * 88)
print(" 各运行的 warmup 条数")
print("=" * 88)
print("  %-42s %-8s %-8s %s" % ("运行编号", "配置n", "实际条", "phase 值"))
for d in sorted([x for x in R.iterdir() if x.is_dir()], key=lambda x: x.name):
    rc = d / "run-config.json"
    sm = d / "summary.json"
    if not rc.is_file() or not sm.is_file():
        continue
    cfg = json.loads(rc.read_text(encoding="utf-8"))
    s = json.loads(sm.read_text(encoding="utf-8"))
    n_cfg = (cfg.get("warmup") or {}).get("n")
    wu = s.get("warmup") or []
    cs = s.get("cold_start")
    phases = [w.get("phase") for w in wu]
    # 顺带看 requests.jsonl 里有没有 phase=warmup 的记录
    rq = d / "requests.jsonl"
    n_rq = 0
    if rq.is_file():
        for l in rq.read_text(encoding="utf-8").splitlines():
            if l.strip() and '"warmup"' in l:
                n_rq += 1
    print("  %-42s %-8s %-8s %s  (requests.jsonl 里 warmup=%d, cold_start=%s)"
          % (d.name, n_cfg, len(wu), phases, n_rq, "有" if cs else "无"))

print()
print("=" * 88)
print(" 一个运行里 warmup 的完整内容（看它到底是 1 条还是 2 条）")
print("=" * 88)
d = R / "ctx16k-f16-06b-r1-20261016-222516"
s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
print("  cold_start:")
print("   ", json.dumps({k: s["cold_start"].get(k) for k in
                         ("request_id", "phase", "status", "ttft_ms",
                          "output_tokens", "finish_reason")},
                        ensure_ascii=False) if s.get("cold_start") else "（无）")
print()
print("  warmup 数组 (%d 条):" % len(s.get("warmup") or []))
for w in (s.get("warmup") or []):
    print("   ", json.dumps({k: w.get(k) for k in
                             ("request_id", "phase", "status", "ttft_ms",
                              "output_tokens", "finish_reason")},
                            ensure_ascii=False))
print()
print("=" * 88)
print(" 看 run.log 里「冷启动/预热」那几行（日志逐条打，最能说明跑了几条）")
print("=" * 88)
log = d / "run.log"
if log.is_file():
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "冷启动" in ln or "预热" in ln:
            print("  " + ln.strip())
