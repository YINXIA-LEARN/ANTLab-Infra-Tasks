#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_data.py —— 汇总所有运行，产出报告要用的表格与结论数据。

设计原则（和测试套件一致）：
 · 真实配置一律从 run-config.json / meta.json 读，不信 label
 · 完整性先判：跨服务进程重启或没跑完的运行【不参与对比】
 · 对比只在【单变量变化】的两两之间做
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

DEV = Path(__file__).resolve().parents[2]
RESULTS = DEV / "results"


def load(d):
    cfg = json.loads((d / "run-config.json").read_text(encoding="utf-8"))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) \
        if (d / "meta.json").is_file() else {}
    summ = json.loads((d / "summary.json").read_text(encoding="utf-8")) \
        if (d / "summary.json").is_file() else {}
    st = json.loads((d / "run-state.json").read_text(encoding="utf-8")) \
        if (d / "run-state.json").is_file() else {}
    rows = []
    rq = d / "requests.jsonl"
    if rq.is_file():
        rows = [json.loads(l) for l in
                rq.read_text(encoding="utf-8").splitlines() if l.strip()]
    sc = meta.get("server_config") or {}
    return {
        "rid": d.name, "cfg": cfg, "summ": summ, "rows": rows,
        "state": summ.get("state") or st.get("state"),
        "ctx": cfg.get("server_ctx"),
        "kvk": cfg.get("kv_type_k"), "kvv": cfg.get("kv_type_v"),
        "model": (cfg.get("model_file") or "").replace("Qwen3-", "").replace(
            "-Q8_0.gguf", ""),
        "max_tokens": cfg.get("max_tokens"),
        "flags": sc.get("server_flags"),
        "vram_after": meta.get("vram_after_load_mib"),
        "n": len(rows),
        "has_restart": (d / "service-starts.jsonl").is_file(),
        "pids": sorted({r.get("service_instance_pid") for r in rows
                        if r.get("service_instance_pid") is not None}),
        "dist": dict(Counter(r.get("status") for r in rows)),
    }


def ok_for_compare(r):
    return (r["state"] == "finished" and r["n"] == 90
            and not r["has_restart"] and len(r["pids"]) <= 1)


def per_conc(r):
    return r["summ"].get("formal") or {}


def g(f, *keys):
    cur = f
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def main():
    runs = [load(d) for d in sorted([x for x in RESULTS.iterdir()
                                     if x.is_dir()], key=lambda x: x.name)]

    print("=" * 112)
    print(" 一、运行清单与完整性")
    print("=" * 112)
    print("  %-34s %-6s %-11s %-6s %-5s %-9s %s" % (
        "运行编号", "ctx", "KV", "模型", "条数", "状态", "可对比"))
    for r in runs:
        why = ""
        if not ok_for_compare(r):
            if r["state"] != "finished":
                why = "未跑完"
            elif r["n"] != 90:
                why = "只 %d 条" % r["n"]
            elif r["has_restart"] or len(r["pids"]) > 1:
                why = "服务重启过"
        print("  %-34s %-6s %-11s %-6s %-5s %-9s %s" % (
            r["rid"], r["ctx"], "%s/%s" % (r["kvk"], r["kvv"]), r["model"],
            r["n"], r["state"], "✅" if ok_for_compare(r) else "❌ " + why))

    good = [r for r in runs if ok_for_compare(r)]
    print()
    print("  可对比的运行 %d 个；不可对比 %d 个（用于展示错误状态 / 需重跑）"
          % (len(good), len(runs) - len(good)))

    # ── 指标表 ──
    print()
    print("=" * 112)
    print(" 二、指标（只列可对比的运行）")
    print("=" * 112)
    hdr = ("运行编号", "c", "TTFT中位", "TTFT-P95", "TPOT中位", "吞吐 tok/s",
           "墙钟 s", "显存峰 MiB", "成功/总")
    print("  %-34s %-3s %-9s %-9s %-9s %-12s %-8s %-11s %s" % hdr)
    for r in good:
        for c, f in sorted(per_conc(r).items(), key=lambda kv: int(kv[0])):
            print("  %-34s %-3s %-9s %-9s %-9s %-12s %-8s %-11s %s/%s" % (
                r["rid"], c,
                round(g(f, "ttft_ms", "median") or 0),
                round(g(f, "ttft_ms", "p95") or 0),
                round(g(f, "tpot_excl_first_ms", "median") or 0, 1),
                round(g(f, "throughput", "output_tokens_per_s") or 0, 1),
                round(f.get("wall_time_s") or 0),
                f.get("vram_peak_mib"),
                f.get("n_success"), f.get("n_requests")))

    # ── 单变量对比 ──
    print()
    print("=" * 112)
    print(" 三、单变量对比（每对只差一个变量）")
    print("=" * 112)
    by_key = {}
    for r in good:
        by_key[(r["model"], r["ctx"], r["kvk"])] = r

    def pair(a, b, label):
        ra, rb = by_key.get(a), by_key.get(b)
        if not ra or not rb:
            print("  %-42s （缺数据：%s / %s）" % (label, a, b))
            return
        print("  ── %s" % label)
        print("     A %s  ctx=%s KV=%s  |  B %s ctx=%s KV=%s" % (
            ra["rid"], ra["ctx"], ra["kvk"], rb["rid"], rb["ctx"], rb["kvk"]))
        print("     %-5s %-22s %-22s %s" % ("并发", "A TTFT / 吞吐", "B TTFT / 吞吐",
                                            "变化"))
        for c in ("1", "4", "10"):
            fa, fb = per_conc(ra).get(c), per_conc(rb).get(c)
            if not fa or not fb:
                continue
            ta = g(fa, "ttft_ms", "median") or 0
            tb = g(fb, "ttft_ms", "median") or 0
            ha = g(fa, "throughput", "output_tokens_per_s") or 0
            hb = g(fb, "throughput", "output_tokens_per_s") or 0
            dt = ((tb - ta) / ta * 100) if ta else 0
            dh = ((hb - ha) / ha * 100) if ha else 0
            print("     c%-4s %-22s %-22s TTFT %+.1f%% / 吞吐 %+.1f%%" % (
                c, "%.0f ms / %.1f" % (ta, ha), "%.0f ms / %.1f" % (tb, hb),
                dt, dh))
        va = ra["summ"].get("overall") or {}
        vb = rb["summ"].get("overall") or {}
        print("     显存峰 : %s → %s MiB   成功率: %.1f%% → %.1f%%" % (
            max((f.get("vram_peak_mib") or 0) for f in per_conc(ra).values()),
            max((f.get("vram_peak_mib") or 0) for f in per_conc(rb).values()),
            (va.get("success_rate") or 0) * 100,
            (vb.get("success_rate") or 0) * 100))
        print()

    for model in ("0.6B", "1.7B"):
        pair((model, 16384, "f16"), (model, 32768, "f16"),
             "%s：上下文 16K → 32K（KV 精度不变）" % model)
        pair((model, 16384, "f16"), (model, 16384, "q8_0"),
             "%s：KV 精度 f16 → q8_0（上下文不变）" % model)
    pair(("0.6B", 16384, "f16"), ("1.7B", 16384, "f16"),
         "模型 0.6B → 1.7B（上下文与 KV 精度不变）")


if __name__ == "__main__":
    main()
