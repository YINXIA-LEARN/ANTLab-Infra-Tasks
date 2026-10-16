#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总这批正式运行：真实配置 + 指标 + 完整性。label 一律不信。"""
import json
from collections import Counter
from pathlib import Path

RUNS = DEV / "results"


def j(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def row(d):
    cfg = j(d / "run-config.json") or {}
    meta = j(d / "meta.json") or {}
    s = j(d / "summary.json") or {}
    st = j(d / "run-state.json") or {}
    sc = meta.get("server_config") or {}
    flags = sc.get("server_flags")
    if isinstance(flags, dict):
        flags = " ".join("%s %s" % (k, v) for k, v in flags.items())
    flags = flags or ""
    rq = d / "requests.jsonl"
    rows = []
    if rq.is_file():
        rows = [json.loads(l) for l in
                rq.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 服务实例
    pids = sorted({r.get("service_instance_pid") for r in rows
                   if r.get("service_instance_pid") is not None})
    starts = (d / "service-starts.jsonl").is_file()
    return {
        "rid": d.name, "dir": d,
        "label": cfg.get("label"), "ctx": cfg.get("server_ctx"),
        "kv": "%s/%s" % (cfg.get("kv_type_k"), cfg.get("kv_type_v")),
        "model": (cfg.get("model_file") or "").replace(
            "Qwen3-", "").replace("-Q8_0.gguf", ""),
        "max_tokens": cfg.get("max_tokens"), "order": cfg.get("order"),
        "state": s.get("state") or st.get("state"),
        "n": len(rows), "dist": dict(Counter(r.get("status") for r in rows)),
        "flags": flags, "real_ctx": sc.get("n_ctx"),
        "real_slots": sc.get("total_slots"),
        "vram_after": meta.get("vram_after_load_mib"),
        "pids": pids, "has_svc_starts": starts,
        "note": cfg.get("note"), "summ": s,
    }


def fmt_ctx(r):
    f = r["flags"]
    keep = []
    toks = f.split()
    for i, t in enumerate(toks):
        if t in ("-c", "-ngl", "-ctk", "-ctv", "--cache-ram", "-np",
                 "--flash-attn", "-fa"):
            keep.append("%s %s" % (t, toks[i + 1] if i + 1 < len(toks) else "?"))
        elif t.startswith("-c") and t != "-c" and len(t) > 2:
            keep.append(t)
    return " ".join(keep) or f[:80]


def main():
    rs = [row(d) for d in sorted([x for x in RUNS.iterdir() if x.is_dir()],
                                 key=lambda x: x.name)]

    print("=" * 104)
    print(" 一、真实配置（label 一律不信，全部从 run-config.json / meta.json 读）")
    print("=" * 104)
    print("  %-34s %-6s %-10s %-6s %-9s %-6s %s" % (
        "运行编号", "ctx", "KV", "模型", "状态", "条数", "完整性"))
    for r in rs:
        complete = "✅" if (r["n"] == 90 and r["state"] == "finished"
                            and not r["has_svc_starts"]) else "⚠️"
        extra = ""
        if r["has_svc_starts"]:
            extra = " 服务重启过"
        if r["n"] != 90:
            extra += " 只有 %d 条" % r["n"]
        print("  %-34s %-6s %-10s %-6s %-9s %-6s %s%s" % (
            r["rid"], r["ctx"], r["kv"], r["model"], r["state"], r["n"],
            complete, extra))

    print()
    print("=" * 104)
    print(" 二、服务实际启动参数（从进程命令行读的，最权威）")
    print("=" * 104)
    for r in rs:
        print("  %-34s ctx=%-6s slots=%-3s 加载后显存=%-5s | %s" % (
            r["rid"], r["real_ctx"], r["real_slots"],
            r["vram_after"], fmt_ctx(r)))

    print()
    print("=" * 104)
    print(" 三、各并发档指标")
    print("=" * 104)
    for r in rs:
        s = r["summ"]
        if not s:
            print("  %-34s （无 summary —— 没跑完）" % r["rid"])
            continue
        print("  ── %s   ctx=%s  KV=%s  %s" % (
            r["rid"], r["ctx"], r["kv"], r["model"]))
        o = s.get("overall") or {}
        print("     overall  n=%s 成功=%s 成功率=%.1f%% | 截断=%s 服务消失=%s "
              "中断=%s" % (o.get("n_requests"), o.get("n_success"),
                           (o.get("success_rate") or 0) * 100,
                           o.get("n_truncated"), o.get("n_service_lost"),
                           o.get("n_incomplete")))
        cs = s.get("cold_start") or {}
        wu = s.get("warmup") or []
        if cs:
            print("     冷启动   TTFT %.0f ms / 总 %.0f ms" % (
                cs.get("ttft_ms") or 0, cs.get("total_ms") or 0))
        if wu:
            print("     预热     %s" % " / ".join(
                "TTFT %.0f ms" % (w.get("ttft_ms") or 0) for w in wu))
        for conc, f in sorted((s.get("formal") or {}).items(),
                              key=lambda kv: int(kv[0])):
            tt = f.get("ttft_ms") or {}
            tp = f.get("tpot_excl_first_ms") or {}
            th = f.get("throughput") or {}
            print("     c%-4s n=%-3s 成=%-3s TTFT中位=%-7s P95=%-8s "
                  "TPOT=%-6s 吞吐=%-7s 墙钟=%-6.0fs 显存峰=%s" % (
                      conc, f.get("n_requests"), f.get("n_success"),
                      round(tt.get("median") or 0), round(tt.get("p95") or 0),
                      round(tp.get("median") or 0, 2),
                      round(th.get("output_tokens_per_s") or 0, 1),
                      f.get("wall_time_s") or 0, f.get("vram_peak_mib")))
        print()


if __name__ == "__main__":
    main()
