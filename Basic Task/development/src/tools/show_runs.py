#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整理两轮运行的数据，供报告引用"""
import json
import glob
import os

os.chdir(Path(__file__).resolve().parents[2])

runs = {}
for p in sorted(glob.glob("results/*/summary.json")):
    d = json.load(open(p, encoding="utf-8"))
    runs[d.get("label") or d["run_id"]] = d

order = sorted(runs, key=lambda k: 0 if k.startswith("baseline") else 1)
A = runs[order[0]]
B = runs[order[1]] if len(order) > 1 else None

print("=" * 100)
print("基线 vs 优化：完整对照")
print("=" * 100)
print()
print("  A = %s" % A["label"])
print("      %s" % A["run_id"])
print("  B = %s" % (B["label"] if B else "—"))
if B:
    print("      %s" % B["run_id"])
print()

hdr = ("并发", "成功/总", "TTFT中位", "TTFT P95", "TPOT", "吞吐", "完成中位", "完成 P95", "显存峰")
for label, d in [("A 基线", A), ("B 优化", B)]:
    if not d:
        continue
    print("── %s ──" % label)
    print("  %-6s %-9s %-9s %-10s %-8s %-9s %-9s %-10s %s" % hdr)
    for k in sorted(d["formal"], key=lambda x: int(x)):
        s = d["formal"][k]
        print("  %-6s %-9s %-9.0f %-10.0f %-8.2f %-9.1f %-9.0f %-10.0f %s" % (
            k, "%d/%d" % (s["n_success"], s["n_requests"]),
            s["ttft_ms"]["median"], s["ttft_ms"]["p95"],
            s["tpot_excl_first_ms"]["median"] or 0,
            s["throughput"]["output_tokens_per_s"] or 0,
            s["total_ms"]["median"], s["total_ms"]["p95"],
            s["vram_peak_mib"]))
    print("     冷启动 TTFT %.0f ms | 预热 %s | 总耗时 %.0fs" % (
        d["cold_start"]["ttft_ms"],
        "/".join("%.0f" % w["ttft_ms"] for w in d["warmup"]),
        d["duration_s"]))
    print()

if B:
    print("=" * 100)
    print("变化幅度（B 相对 A）")
    print("=" * 100)
    print("  %-6s %-12s %-12s %-12s %-12s" % ("并发", "TTFT中位", "TTFT P95", "吞吐", "显存峰"))
    for k in sorted(A["formal"], key=lambda x: int(x)):
        a, b = A["formal"][k], B["formal"][k]

        def dch(x, y):
            return "%+.1f%%" % ((y - x) / x * 100) if x else "—"

        print("  %-6s %-12s %-12s %-12s %-12s" % (
            k,
            dch(a["ttft_ms"]["median"], b["ttft_ms"]["median"]),
            dch(a["ttft_ms"]["p95"], b["ttft_ms"]["p95"]),
            dch(a["throughput"]["output_tokens_per_s"],
                b["throughput"]["output_tokens_per_s"]),
            dch(a["vram_peak_mib"], b["vram_peak_mib"])))
    print()

print("=" * 100)
print("数据健康度")
print("=" * 100)
for label, d in [("A", A), ("B", B)]:
    if not d:
        continue
    ov = d["overall"]
    print("  %s (%s): 成功率 %.1f%%  状态 %s" % (
        label, d["label"], (ov.get("success_rate") or 0) * 100,
        json.dumps(ov.get("status_breakdown"), ensure_ascii=False)))
