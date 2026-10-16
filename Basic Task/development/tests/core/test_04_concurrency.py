#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_04_concurrency.py —— 并发状态与排队

═══════════════════════════════════════════════════════════════════════════
 对应题目（原文）
═══════════════════════════════════════════════════════════════════════════
 任务介绍·场景与并发测试：
     「测试单并发和至少一种多并发，最大并发为 10。」
     「说明客户端并发数、服务 slot 数及请求排队方式。」
     「每种核心配置至少完成 3 轮相同负载测试，记录请求顺序、
       到达时间和重复方式。」

 提交标准·开发与观测：
     「测试程序能够运行固定负载，支持单并发和至少一种多并发。」

 tests/README.md：
     「测试覆盖……并发状态」

═══════════════════════════════════════════════════════════════════════════
 判据（独立来源）
═══════════════════════════════════════════════════════════════════════════
 并发这块最容易出的问题是【请求数对不上】：
 声明跑 N 个请求，实际记录多了或少了，而汇总照样出数。

 独立来源是【配置声明】与【实际记录】两边的对照：

     配置里写的   任务数 × 轮次 × 并发档数   ← 声明
     记录里实际   逐请求条数                 ← 事实
                            ↑ 两者必须相等

 以及并发档本身的性质：
     · 同一并发档内的请求，concurrency 字段必须一致
     · 最大并发不超过 10（题目硬要求）
     · 每档成功数之和 == 总成功数（不重不漏）

═══════════════════════════════════════════════════════════════════════════
 关键边界样例
═══════════════════════════════════════════════════════════════════════════
   ① 单并发  必须有（题目要求"单并发和至少一种多并发"）
   ② 多并发  必须有，且 ≤ 10
   ③ 排队    并发 10 > 服务 slot 数（4）时，请求必须排队而不是被丢
             → 表现：TTFT 显著变大，但成功数不变
   ④ wave    每个并发档的请求数必须是【任务数×轮次】的整数倍

 退出码：0 通过 / 1 有失败 / 2 脚本自身出错
"""

import sys
from collections import Counter
from pathlib import Path

# ⚠️ 本文件在 tests/core/ 下，_testlib.py 在上一层 tests/。
#    两层都要加：本层用于同目录的模块，上一层用于 _testlib。
#    （_testlib 必须留在 tests/ —— 它的 DEV_DIR/RESULTS_DIR
#      是按自己位置算的，挪下来会整体错一层）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _testlib import Report, load_run, list_runs, run, successful   # noqa: E402

MAX_CONCURRENCY = 10          # 题目原文：最大并发为 10


def main():
    runs = list_runs()
    rep = Report("test_04_concurrency —— 并发状态与排队")

    if not runs:
        rep.check_true("results/ 下有可测运行", False, "没有数据")
        return rep.finish()

    for rid in runs:
        _check_one(rid, rep)

    # ══════════════════════════════════════════════════════════════════
    #  跨运行：题目要求的"单并发 + 至少一种多并发，最大 10"
    #
    #  ⚠️ 这是对【整个测试集】的要求，不是对每一次运行的要求。
    #     baseline-c1 是只做单并发的冒烟配置，拿它要求多并发是错的
    #     （早先这么写，报了一个假失败）。
    # ══════════════════════════════════════════════════════════════════
    print()
    print("═" * 72)
    print(" 跨运行：整套测试的并发覆盖")
    print("═" * 72)
    all_concs = set()
    per_run = {}
    for rid in runs:
        d = load_run(rid)
        cs = sorted({r.get("concurrency") for r in d["requests"]
                     if r.get("concurrency")})
        per_run[rid] = cs
        all_concs |= set(cs)
        print(f"  {rid:40s} {cs}")

    rep.check_true("整套测试覆盖了单并发（=1）", 1 in all_concs,
                   f"合计档位 {sorted(all_concs)}")
    rep.check_true("整套测试覆盖了至少一种多并发（>1）",
                   any(c > 1 for c in all_concs),
                   f"合计档位 {sorted(all_concs)}")
    rep.check_true(f"整套测试的最大并发不超过 {MAX_CONCURRENCY}",
                   (max(all_concs) if all_concs else 0) <= MAX_CONCURRENCY,
                   f"最大 {max(all_concs) if all_concs else '—'}")
    print(f"  合计覆盖的并发档：{sorted(all_concs)}")
    rep.note(f"整套测试的并发覆盖：{sorted(all_concs)}"
             f"（单并发 {'✅' if 1 in all_concs else '❌'}，"
             f"多并发 {'✅' if any(c > 1 for c in all_concs) else '❌'}）")

    return rep.finish()


def _check_one(rid, rep):
    d = load_run(rid)
    rows = d["requests"]
    cfg = d["run_config"] or {}
    ok = successful(rows)

    print("═" * 72)
    print(f" 运行 {rid}（记录 {len(rows)} 条）")
    print("═" * 72)

    concs = sorted({r.get("concurrency") for r in rows if r.get("concurrency")})
    print(f"  出现过的并发档: {concs}")

    # ── ①② 并发档位：逐运行只【记录】，硬要求在跨运行汇总里判 ────────
    #
    #  ⚠️ baseline-c1 是只做单并发的冒烟配置，不该要求它有多并发。
    #     题目那句是对整套测试的要求（跨运行判定见 main）。
    print(f"  本运行的并发档: {concs}")
    if 1 not in concs and not any(c > 1 for c in concs):
        rep.check_true(f"{rid}: 至少有一个并发档", False, "一条记录都没有")
    if max(concs) > MAX_CONCURRENCY if concs else False:
        rep.check_true(f"{rid}: 最大并发不超过 {MAX_CONCURRENCY}", False,
                       f"实际 {max(concs)}")

    # ── 请求总数 = 声明值（配置 vs 记录，独立对照）────────────────────
    n_task = len(cfg.get("task_ids") or [])
    rounds = cfg.get("rounds")
    cfg_concs = cfg.get("concurrency") or []
    if n_task and rounds and cfg_concs:
        want = n_task * rounds * len(cfg_concs)
        rep.check(f"{rid}: 请求总数 = 任务数×轮次×并发档数", len(rows), want)
        # 每档请求数 = 任务数 × 轮次
        per = n_task * rounds
        for c in cfg_concs:
            got = sum(1 for r in rows if r.get("concurrency") == c)
            rep.check(f"{rid} c{c}: 该档请求数 = 任务数×轮次", got, per)
        # ④ 每档必须是 per 的整数倍（wave 切分的必然结果）
        for c in cfg_concs:
            got = sum(1 for r in rows if r.get("concurrency") == c)
            rep.check(f"{rid} c{c}: 请求数能被 任务数×轮次 整除（wave 切分）",
                      got % per if per else 1, 0)
    else:
        rep.note(f"{rid}: run-config.json 缺 task_ids/rounds/concurrency，"
                 f"无法做声明-记录对照")

    # ── 同一档内 concurrency 字段必须一致 ────────────────────────────
    for c in concs:
        sub = [r for r in rows if r.get("concurrency") == c]
        vals = {r.get("concurrency") for r in sub}
        rep.check(f"{rid} c{c}: 该档内 concurrency 字段唯一", len(vals), 1)

    # ── 每档成功数之和 == 总成功数（不重不漏）────────────────────────
    rep.check(f"{rid}: Σ各档成功数 == 总成功数",
              sum(len([r for r in ok if r.get("concurrency") == c])
                  for c in concs), len(ok))

    # ── 每档都覆盖了全部任务（题目要求"固定负载"）────────────────────
    tids = sorted({r.get("task_id") for r in rows if r.get("task_id")})
    want_tids = set(cfg.get("task_ids") or tids)
    for c in concs:
        got = {r.get("task_id") for r in rows if r.get("concurrency") == c}
        rep.check_true(f"{rid} c{c}: 覆盖了配置里的全部任务编号",
                       want_tids.issubset(got),
                       f"缺 {sorted(want_tids - got)}")

    # ── ③ 排队：并发 > slot 数时，TTFT 应显著变大 ────────────────────
    #
    #  ⚠️ 这条不是"必须通过"，而是【必须解释】：
    #     并发数超过服务 slot 数（本机 4）时，多出来的请求要排队，
    #     所以 TTFT 会明显上升。这是预期行为，不是故障。
    #     但如果【没上升】，反而要查是不是并发没真的并发起来。
    if len(concs) >= 2 and 1 in concs:
        def med(c):
            v = sorted(r["ttft_ms"] for r in ok if r.get("concurrency") == c)
            return v[len(v) // 2] if v else None
        base = med(1)
        for c in concs:
            if c == 1:
                continue
            m = med(c)
            if base and m:
                ratio = m / base
                print(f"  c{c} TTFT 中位 {m:.1f} ms = c1 的 {ratio:.1f} 倍")
                if c > 4:      # 4 是服务 slot 数，超过就要排队
                    rep.check_true(
                        f"{rid}: c{c} 超过 slot 数(4)，TTFT 应高于 c1（排队现象）",
                        m > base,
                        f"c{c}={m:.1f}ms 未高于 c1={base:.1f}ms —— "
                        f"并发可能没真正并发起来")
                    rep.note(f"{rid}: c{c} 相对 c1 的 TTFT 放大 {ratio:.1f} 倍"
                             f"（排队代价，属预期）")
    print()


if __name__ == "__main__":
    run(main)
