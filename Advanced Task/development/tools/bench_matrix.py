#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_matrix.py —— 基线 vs 优化，每个场景 3 轮

═══════════════════════════════════════════════════════════════════════════
 两组对照
═══════════════════════════════════════════════════════════════════════════
   基线（baseline）  直连 llama-server:8092，不经过代理
                     —— 这是 llama.cpp 的【原生行为】。注意它本身也有
                        "同槽前缀复用"，所以不是"零缓存"，而是"没有跨槽
                        和跨重启的缓存"。
   优化（proxy）     经过代理:8091，L2(tmpfs) + L3(磁盘) 三级缓存

   每个场景跑 3 轮。优化组在开始前清空缓存 → 第 1 轮是冷启动，
   第 2、3 轮是热状态。报告里要分别说明。

═══════════════════════════════════════════════════════════════════════════
 产出
═══════════════════════════════════════════════════════════════════════════
   results/_summary/matrix.json     机器可读
   results/_summary/matrix.md       人可读的对比表
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
OUT = DEV / "results/_summary"
ROUNDS = 3


def sh(cmd, timeout=1800):
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def health(url):
    rc, out = sh(f"curl -s -m 3 -o /dev/null -w '%{{http_code}}' {url}/health")
    return out.strip() == "200"


def clear_cache():
    sh(f'bash "{DEV}/tools/stop_adv.sh"', 180)
    sh(f'rm -rf /dev/shm/antlab_kv "{DEV}/kv_cache/L3" "{DEV}/kv_index.json" '
       f'"{DEV}/results/_live"', 120)


def start():
    rc, out = sh(f'bash "{DEV}/tools/start_adv.sh"', 900)
    return "代理入口" in out


def run_replay(workload, target, rounds):
    """跑一次回放；带硬超时，防止某个场景卡死拖垮整批"""
    try:
        r = subprocess.run(
            ["bash", "-lc",
             f'cd "{DEV}" && python3 -u tools/replay.py '
             f'--workload configs/workloads/{workload}.json '
             f'--target {target} --rounds {rounds} 2>&1'],
            capture_output=True, text=True, timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace")
               if isinstance(e.stdout, bytes) else (e.stdout or ""))
        out += "\n⚠️ 回放超时（900s），已强制结束"
        print("  ⚠️ 回放超时，跳过该组")
    last = ""
    for line in out.splitlines():
        if "输出目录" in line:
            last = line.split("输出目录:")[-1].strip()
    if not last:
        print("  ⚠️ 没拿到输出目录，回放可能失败。尾部输出：")
        for line in out.splitlines()[-8:]:
            print("     " + line)
    return last, out


def load_summary(run_dir):
    p = Path(run_dir) / "summary.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="S1,S2,S3,S4,S5")
    ap.add_argument("--rounds", type=int, default=ROUNDS)
    a = ap.parse_args()
    scen = [s.strip() for s in a.scenarios.split(",") if s.strip()]

    OUT.mkdir(parents=True, exist_ok=True)
    results = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "rounds": a.rounds, "scenarios": {}}

    for s in scen:
        print()
        print("=" * 78)
        print(f" 场景 {s}")
        print("=" * 78)

        # ── 基线 ──
        if not health("http://127.0.0.1:8092"):
            print("  ⚠️ 模型服务不在线，先启动")
            if not start():
                print("  ❌ 启动失败，跳过该场景")
                continue
        print(f"  [基线] 直连 8092，{a.rounds} 轮")
        d, out = run_replay(s, "baseline", a.rounds)
        base = load_summary(d) if d else None
        print(f"         运行目录 {d or '（失败）'}")
        if base:
            print(f"         命中率 {base['hit_rate']*100:.1f}%  "
                  f"复用 {base['reuse_ratio']*100:.1f}%  "
                  f"prefill 合计 {base['prefill_ms']['sum']} ms")

        # ── 优化 ──
        print(f"  [优化] 清空缓存后重启，再跑 {a.rounds} 轮")
        clear_cache()
        if not start():
            print("  ❌ 优化组启动失败")
            continue
        time.sleep(1)
        d2, out2 = run_replay(s, "proxy", a.rounds)
        opt = load_summary(d2) if d2 else None
        print(f"         运行目录 {d2 or '（失败）'}")
        if opt:
            print(f"         命中率 {opt['hit_rate']*100:.1f}%  "
                  f"复用 {opt['reuse_ratio']*100:.1f}%  "
                  f"prefill 合计 {opt['prefill_ms']['sum']} ms")
            print(f"         模式分布 {opt['modes']}")

        results["scenarios"][s] = {
            "baseline": base, "optimized": opt,
            "baseline_dir": d, "optimized_dir": d2,
        }

        # 每轮明细（如果有多轮）
        if opt and opt.get("per_round"):
            for r in opt["per_round"]:
                print(f"         第{r['round']}轮 模式 {r['modes']} "
                      f"prefill {r['prefill_ms_sum']} ms")
        if base and base.get("per_round"):
            for r in base["per_round"]:
                print(f"         基线第{r['round']}轮 "
                      f"prefill {r['prefill_ms_sum']} ms")

    # ── 写汇总 ──
    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (OUT / "matrix.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 对比表
    lines = []
    lines.append("# 基线 vs 优化 对比（每场景 %d 轮）" % a.rounds)
    lines.append("")
    lines.append("> 基线 = 直连 llama-server（原生行为，含同槽前缀复用，"
                 "但没有跨槽/跨重启缓存）")
    lines.append("> 优化 = 经过缓存代理（L1 显存 / L2 tmpfs / L3 磁盘）")
    lines.append("")
    lines.append("| 场景 | 组 | 请求 | 命中率 | 复用 token | prefill 合计 (ms) "
                 "| p50 (ms) | p95 (ms) |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for s in scen:
        r = results["scenarios"].get(s)
        if not r:
            continue
        for tag, key in (("基线", "baseline"), ("优化", "optimized")):
            d = r.get(key)
            if not d:
                lines.append(f"| {s} | {tag} | - | - | - | - | - | - |")
                continue
            pf = d.get("prefill_ms") or {}
            lines.append(
                f"| {s} | {tag} | {d['n_success']}/{d['n_total']} "
                f"| {d['hit_rate']*100:.1f}% | {d['reuse_ratio']*100:.1f}% "
                f"| {pf.get('sum')} | {pf.get('p50')} | {pf.get('p95')} |")

    lines.append("")
    lines.append("## 优化组的模式分布")
    lines.append("")
    lines.append("| 场景 | L1 | L2 | L3 | prefix | miss |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s in scen:
        r = results["scenarios"].get(s)
        if not r or not r.get("optimized"):
            continue
        m = r["optimized"]["modes"]
        lines.append(f"| {s} | {m.get('L1',0)} | {m.get('L2',0)} "
                     f"| {m.get('L3',0)} | {m.get('prefix',0)} "
                     f"| {m.get('miss',0)} |")

    (OUT / "matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print("=" * 78)
    print(f" 汇总已写入 {OUT}/matrix.json 与 matrix.md")
    print("=" * 78)
    print("\n".join(lines[6:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
