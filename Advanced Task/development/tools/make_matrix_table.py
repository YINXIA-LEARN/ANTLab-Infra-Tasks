#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_matrix_table.py —— 从 results/ 里已有的运行直接生成"基线 vs 优化"对比表

不依赖 bench_matrix 是否跑完。只统计【3 轮】的运行，按场景配对。
"""

import json
import statistics
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
RES = DEV / "results"
OUT = RES / "_summary"


def runs():
    out = []
    for d in sorted(RES.glob("*/")):
        s, c = d / "summary.json", d / "run-config.json"
        if not (s.is_file() and c.is_file()):
            continue
        try:
            sm = json.loads(s.read_text(encoding="utf-8"))
            cf = json.loads(c.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        if not cf.get("rounds") or cf["rounds"] < 3:
            continue
        out.append({"dir": d.name, "target": cf.get("target"),
                    "label": sm.get("label"), "sm": sm, "cf": cf})
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_runs = runs()
    by = {}
    for r in all_runs:
        key = (r["label"] or "").split("-")[0]
        by.setdefault(key, {})[r["target"]] = r

    lines = ["# 基线 vs 优化（3 轮）", "",
             "> 基线 = 直连 llama-server（原生行为，含同槽前缀复用）",
             "> 优化 = 经过缓存代理（L1 显存 / L2 tmpfs / L3 磁盘）",
             "> prefill 为 3 轮合计；括号内为各轮数值。", "",
             "| 场景 | 组 | 请求 | 命中率 | 复用 token | prefill 合计 ms "
             "| 各轮 prefill |", "|---|---|---:|---:|---:|---:|---|"]
    data = {}
    for key in sorted(by):
        pair = by[key]
        if "baseline" not in pair or "proxy" not in pair:
            continue
        row = {}
        for tgt in ("baseline", "proxy"):
            r = pair[tgt]
            sm = r["sm"]
            pf = sm.get("prefill_ms") or {}
            rounds = [(x.get("prefill_ms_sum") or 0)
                      for x in (sm.get("per_round") or [])]
            row[tgt] = {
                "n": f"{sm.get('n_success')}/{sm.get('n_total')}",
                "hit": sm.get("hit_rate"),
                "reuse": sm.get("reuse_ratio"),
                "pf_sum": pf.get("sum"),
                "rounds": rounds,
                "modes": sm.get("modes"),
                "dir": r["dir"],
            }
        data[key] = row
        for tgt, name in (("baseline", "基线"), ("proxy", "优化")):
            x = row[tgt]
            lines.append(
                f"| {key} | {name} | {x['n']} | {(x['hit'] or 0)*100:.1f}% "
                f"| {(x['reuse'] or 0)*100:.1f}% | {x['pf_sum']} "
                f"| {' / '.join(str(round(v)) for v in x['rounds'])} |")
        a, b = row["baseline"], row["proxy"]
        if a["pf_sum"] and b["pf_sum"]:
            lines.append(f"| {key} | **提升** | | | | "
                         f"**{(1 - b['pf_sum']/a['pf_sum'])*100:.0f}%** | |")

    lines.append("")
    lines.append("## 优化组的模式分布")
    lines.append("")
    lines.append("| 场景 | L1 | L2 | L3 | prefix | miss |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for key, row in data.items():
        m = row["proxy"]["modes"] or {}
        lines.append(f"| {key} | {m.get('L1',0)} | {m.get('L2',0)} "
                     f"| {m.get('L3',0)} | {m.get('prefix',0)} "
                     f"| {m.get('miss',0)} |")

    md = "\n".join(lines) + "\n"
    (OUT / "matrix.md").write_text(md, encoding="utf-8")
    (OUT / "matrix.json").write_text(
        json.dumps({"scenarios": data}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(md)
    print(f"\n写入 {OUT}/matrix.md 与 matrix.json")


if __name__ == "__main__":
    main()
