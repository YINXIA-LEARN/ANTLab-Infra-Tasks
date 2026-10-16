#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_03_metrics.py —— 性能指标计算是否正确

═══════════════════════════════════════════════════════════════════════════
 对应题目
═══════════════════════════════════════════════════════════════════════════
 提交标准·优化与结果：
     「指标口径清楚，统计结果能够从原始记录复核」

 报告模板 §5：
     「说明 TTFT、TPOT、吞吐、完成耗时和 P95 的定义与公式」

 tests/README.md：
     「测试覆盖……性能指标计算」
     「围绕关键边界设计样例，如流式块与 token 的区别」

═══════════════════════════════════════════════════════════════════════════
 这个测试怎么判（★ 关键）
═══════════════════════════════════════════════════════════════════════════
 每条记录里同时存了【原始测量】和【派生指标】：

     原始（秒）  t_submit / t_first / t_done        ← 客户端计时得到
     派生（毫秒） ttft_ms / total_ms / tpot_*_ms    ← 由上面算出来

 所以判据是：拿原始值【自己重算】，看是否等于派生值。

    ttft_ms  ?= (t_first - t_submit) * 1000
    total_ms ?= (t_done  - t_submit) * 1000
    tpot_excl_first_ms ?= (t_done - t_first) * 1000 / (output_tokens - 1)

 ⚠️ 这一步【不经过被测的统计代码】—— 只用到减法和除法。
    这就是"独立来源"。如果直接调被测代码的函数来验证它自己，那叫循环论证。

 summary.json 的中位数 / P95 用【本文件重写的另一套算法】复算
 （见 _testlib.median_of / percentile_linear），也是对独立来源。

═══════════════════════════════════════════════════════════════════════════
 关键边界样例（题目点名要的）
═══════════════════════════════════════════════════════════════════════════
   ① 单位边界   秒 vs 毫秒，差 1000 倍。算错会让所有指标偏 1000 倍
   ② 除零边界   output_tokens == 1 时，TPOT 分母 (n-1) == 0
   ③ 首 token   排除首 token 与包含首 token 的两个 TPOT 必须【不同】
   ④ 失败请求   不能计入 token 吞吐，但【要】计入成功率分母

 退出码：0 通过 / 1 有失败 / 2 脚本自身出错
"""

import sys
from pathlib import Path

# ⚠️ 本文件在 tests/core/ 下，_testlib.py 在上一层 tests/。
#    两层都要加：本层用于同目录的模块，上一层用于 _testlib。
#    （_testlib 必须留在 tests/ —— 它的 DEV_DIR/RESULTS_DIR
#      是按自己位置算的，挪下来会整体错一层）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _testlib import (                                    # noqa: E402
    Report, load_run, list_runs, median_of, percentile_linear,
    run, successful,
)

# 毫秒与秒的换算。⚠️ 这是本测试第一个要守住的东西：单位。
MS = 1000.0

# 浮点容差：原始值是秒量级的 float64，乘 1000 后的误差应在 1e-9 毫秒以内
TOL_MS = 1e-6


def main():
    runs = list_runs()
    rep = Report("test_03_metrics —— 性能指标计算")

    print(f"发现 {len(runs)} 次可测运行：")
    for r in runs:
        print(f"    · {r}")
    print()

    if not runs:
        print("⚠️ results/ 下没有带 requests.jsonl 的运行，无法测试。")
        # ⚠️ 配置目录已挪到 development/configs/（见 test_00 的落位检查）
        print("   先跑一次： python3 ../src/bench/run_bench.py "
              "--config ../configs/baseline-full.json")
        rep.check_true("results/ 下存在可测运行", False,
                       "没有数据就无从验证指标计算")
        return rep.finish()

    for rid in runs:
        _check_one(rid, rep)

    return rep.finish()


def _check_one(rid, rep):
    d = load_run(rid)
    rows = d["requests"]
    ok = successful(rows)
    print("═" * 72)
    print(f" 运行 {rid}")
    print(f"   记录 {len(rows)} 条，成功 {len(ok)} 条")
    print("═" * 72)

    if not ok:
        rep.check_true(f"{rid}: 有成功的请求可供校验", False)
        return

    # ── 边界③/①：逐请求派生指标（含单位换算）──────────────────────────
    n_ttft = n_total = n_tpot = 0
    for r in ok:
        # ① 单位：原始是秒，派生是毫秒
        want = (r["t_first"] - r["t_submit"]) * MS
        rep.check_close(f"{rid}/{r['request_id']} ttft_ms = (t_first-t_submit)×1000",
                        r["ttft_ms"], want, tol=TOL_MS)
        n_ttft += 1

        want = (r["t_done"] - r["t_submit"]) * MS
        rep.check_close(f"{rid}/{r['request_id']} total_ms = (t_done-t_submit)×1000",
                        r["total_ms"], want, tol=TOL_MS)
        n_total += 1

        # ② 除零：output_tokens == 1 时分母为 0，须有约定（跳过或记为特殊值）
        n_out = r.get("output_tokens")
        if n_out and n_out > 1:
            want = (r["t_done"] - r["t_first"]) * MS / (n_out - 1)
            rep.check_close(
                f"{rid}/{r['request_id']} tpot_excl_first_ms = (t_done-t_first)×1000/(n_out-1)",
                r["tpot_excl_first_ms"], want, tol=TOL_MS)
            n_tpot += 1
        else:
            rep.note(f"{rid}/{r['request_id']}: output_tokens={n_out}，"
                     f"TPOT 分母为 0，已跳过（这是边界②：除零）")

    rep.note(f"{rid}: 校验了 {n_ttft} 条 TTFT、{n_total} 条 total_ms、"
             f"{n_tpot} 条 TPOT")

    # ── 边界③：两个 TPOT 必须不同（一个排除首 token，一个包含）────────
    both = [r for r in ok if r.get("tpot_excl_first_ms") and r.get("tpot_incl_first_ms")]
    if both:
        same = [r for r in both
                if abs(r["tpot_excl_first_ms"] - r["tpot_incl_first_ms"]) < 1e-12]
        rep.check_true(
            f"{rid}: 排除首token 与 包含首token 的 TPOT 不应完全相同",
            len(same) == 0,
            f"有 {len(same)} 条相同 —— 说明其中一个算错了")
        # 包含首 token 的 TPOT 一定【更小】（把一个大数平摊到更多份）
        worse = [r for r in both
                 if r["tpot_incl_first_ms"] > r["tpot_excl_first_ms"] + 1e-9]
        rep.check_true(
            f"{rid}: tpot_incl_first ≤ tpot_excl_first",
            len(worse) == 0,
            f"有 {len(worse)} 条反了")
    rep.note(f"{rid}: 两种 TPOT 口径均有记录（{len(both)} 条可比）")

    # ── summary.json 能否从原始记录复核（提交标准原文要求）────────────
    s = d["summary"]
    if not s:
        rep.check_true(f"{rid}: summary.json 存在", False)
        return
    formal = s.get("formal") or {}

    def _sub(conc, st=None):
        st = st or "success"
        return [r for r in rows
                if r.get("concurrency") == conc and r.get("status") == st]

    def _all(conc):
        """该并发档的全部请求 —— 不筛状态。

        ⚠️ 早先这里是手写枚举几个状态相加，加了新状态（truncated /
           service_lost）之后就会漏掉，报出"分母不对"的假失败。
           凡是"全部"就该真的取全部 —— 这也正是这个断言要查的东西：
           status_breakdown 是否覆盖了每一条记录。
        """
        return [r for r in rows if r.get("concurrency") == conc]

    for conc_key in sorted(formal, key=lambda x: int(x)):
        conc = int(conc_key)
        f = formal[conc_key]
        sub_ok = _sub(conc)

        # 请求数
        rep.check(f"{rid} c{conc}: 成功数 (summary vs 原始记录)",
                  f.get("n_success"), len(sub_ok))
        # 分母：全部请求（含失败/中断/截断/取消/服务消失）—— 边界④
        rep.check(f"{rid} c{conc}: 请求总数分母 = 该并发档全部请求",
                  f.get("n_requests"), len(_all(conc)))

        # ★ 中位数 / P95：用本文件重写的算法复算
        ttfts = [r["ttft_ms"] for r in sub_ok]
        if not ttfts:
            continue
        rep.check_close(f"{rid} c{conc}: TTFT 中位数可复算",
                        median_of(ttfts), (f.get("ttft_ms") or {}).get("median"),
                        tol=0.01)
        rep.check_close(f"{rid} c{conc}: TTFT P95 可复算",
                        percentile_linear(ttfts, 95), (f.get("ttft_ms") or {}).get("p95"),
                        tol=0.01)

        totals = [r["total_ms"] for r in sub_ok]
        rep.check_close(f"{rid} c{conc}: 完成耗时中位数可复算",
                        median_of(totals), (f.get("total_ms") or {}).get("median"),
                        tol=0.01)
        rep.check_close(f"{rid} c{conc}: 完成耗时 P95 可复算",
                        percentile_linear(totals, 95), (f.get("total_ms") or {}).get("p95"),
                        tol=0.01)

        tpots = [r["tpot_excl_first_ms"] for r in sub_ok
                 if r.get("tpot_excl_first_ms")]
        if tpots:
            rep.check_close(f"{rid} c{conc}: TPOT 中位数可复算",
                            median_of(tpots),
                            (f.get("tpot_excl_first_ms") or {}).get("median"),
                            tol=0.01)

        # ★ 边界④：token 吞吐的分子只算成功的、且只用 output_tokens
        #    自己算：成功请求的 output_tokens 之和 ÷ 该档墙钟时间
        #
        # ⚠️ 墙钟时间有两个等价字段，别只认一个：
        #      f["wall_time_s"]                顶层
        #      f["throughput"]["window_s"]     throughput 对象里
        #   早先只查了 throughput.wall_time_s（不存在），
        #   于是误报"无法复算吞吐" —— 是测试自己的 bug。
        tt = f.get("throughput") or {}
        wall = f.get("wall_time_s") or tt.get("window_s")
        if tt.get("output_tokens_per_s") is not None:
            if wall:
                my_tps = sum(r["output_tokens"] for r in sub_ok) / wall
                rep.check_close(
                    f"{rid} c{conc}: 吞吐 = Σ(output_tokens)/墙钟 可复算",
                    my_tps, tt["output_tokens_per_s"], tol=0.01)
                # 每请求的墙钟时间（requests_per_s）也复算一遍
                #
                # ⚠️ 口径是【全部请求 / 墙钟】，不是"成功请求 / 墙钟"。
                #    为什么：它衡量的是"这个并发档总共处理了多少个请求"，
                #    包括被拒绝的（拒绝也是处理）。与 token 吞吐不同 ——
                #    那里排除失败请求，因为它们没产出 token。
                #    早先这里的注释写成了"成功请求数"，与复算代码
                #    （用 n_requests）自相矛盾，是注释的错。
                if tt.get("requests_per_s") is not None:
                    rep.check_close(
                        f"{rid} c{conc}: 请求速率 = 全部请求数/墙钟 可复算",
                        len(_all(conc)) / wall, tt["requests_per_s"],
                        tol=0.01)
                # ★ 边界④：失败请求【不计入】token 分子 ——
                #    如果分子里混进了失败的请求，这里会对不上
                rep.check(f"{rid} c{conc}: 吞吐的 failed_counted 标记应为 False",
                          tt.get("failed_counted"), False,
                          "失败请求未产生 token，不能计入吞吐分子")
            else:
                rep.check_true(
                    f"{rid} c{conc}: 有墙钟时间可供独立复算吞吐",
                    False, "summary 里既没有 wall_time_s 也没有 throughput.window_s")

        # 分子口径核对：汇总里的 output_total 应等于逐请求之和
        tk = f.get("tokens") or {}
        if tk.get("output_total") is not None:
            rep.check(f"{rid} c{conc}: output_total = Σ逐请求 output_tokens",
                      tk["output_total"], sum(r["output_tokens"] for r in sub_ok))
        if tk.get("prompt_total") is not None:
            rep.check(f"{rid} c{conc}: prompt_total = Σ逐请求 prompt_tokens",
                      tk["prompt_total"], sum(r["prompt_tokens"] for r in sub_ok))
        # 成功数 + 各类异常 == 总数（状态分类完备，不重不漏）
        #
        # ⚠️ 这里必须和 run_bench.summarize() 里的 n_* 字段一一对应。
        #    每加一个新的请求状态，都要同时加到这里 ——
        #    否则这个检查会算出一个偏小的和，报"分类不完整"。
        #    我踩过：加了 truncated 和 service_lost 之后这里没跟着改。
        #
        # ⚠️ 但【老运行】的 summary.json 里没有新字段（它们跑在那个版本之前），
        #    不能因为"老数据缺字段"就报失败 —— 那不是缺陷。
        #    判据：只查有 status_breakdown 的运行，并允许缺的字段按 0 计；
        #    真正要保证的是"不重不漏"，即各字段之和 == status_breakdown 之和。
        _ALL_STATUS_COUNTERS = (
            "n_success", "n_truncated", "n_service_lost",
            "n_failed", "n_timeout", "n_incomplete",
            "n_cancelled", "n_harness_error",
        )
        bd = f.get("status_breakdown") or {}
        if not bd:
            rep.note(f"{rid} c{conc}: 没有 status_breakdown（老数据），"
                     f"跳过状态分类检查")
        else:
            # status_breakdown 是原始逐状态计数 —— 它必须覆盖全部记录
            rep.check(f"{rid} c{conc}: status_breakdown 之和 == 请求总数",
                      sum(bd.values()), f.get("n_requests"))
            # 各 n_* 字段之和也要等于它（缺字段按 0，但和必须对得上）
            rep.check(
                f"{rid} c{conc}: n_* 字段之和 == status_breakdown 之和（不重不漏）",
                sum(f.get(k) or 0 for k in _ALL_STATUS_COUNTERS),
                sum(bd.values()))
            # 新状态字段必须存在 —— 但要确认这是在"新版本跑的数据"上查
            present = [k for k in _ALL_STATUS_COUNTERS if k in f]
            if "n_truncated" in f or "n_service_lost" in f:
                rep.check_true(
                    f"{rid} c{conc}: 新版 summary 的状态计数字段齐全",
                    len(present) == len(_ALL_STATUS_COUNTERS),
                    f"缺：{[k for k in _ALL_STATUS_COUNTERS if k not in f]}")

    # ── 整体成功率 ────────────────────────────────────────────────────
    ov = s.get("overall") or {}
    if ov:
        rep.check(f"{rid}: 总体成功数", ov.get("n_success"), len(ok))
        rep.check(f"{rid}: 总体分母 = 全部记录", ov.get("n_requests"), len(rows))
        if ov.get("success_rate") is not None and rows:
            rep.check_close(f"{rid}: 总体成功率 = 成功数/全部",
                            ov["success_rate"], len(ok) / len(rows), tol=1e-9)

    # ── 边界④：失败请求必须【不在】成功集合里，且被单独归类 ────────────
    bad = [r for r in rows if r.get("status") != "success"]
    if bad:
        dist = {x: sum(1 for r in bad if r.get("status") == x)
                for x in {r.get("status") for r in bad}}
        rep.note(f"{rid}: 有 {len(bad)} 条非成功记录，状态分布 {dist}")

        # ⚠️ 这里的断言改过一次，记下来：
        #    原来写的是"非成功记录不应有 finish_reason" —— 太绝对了。
        #    加进 truncated（输出撞上 max_tokens 被截断）之后，
        #    这类记录【有】 finish_reason=length，却确实不算成功
        #    （输出残缺）。所以正确的判据是：
        #       · 成功   → 必须有 finish_reason（或收到 [DONE]）
        #       · 非成功 → 只允许 truncated 带 finish_reason=length
        with_fr = [r for r in bad if r.get("finish_reason")]
        wrong = [r for r in with_fr
                 if not (r.get("status") == "truncated"
                         and r.get("finish_reason") == "length")]
        rep.check_true(
            f"{rid}: 非成功记录里只有『截断』允许带 finish_reason=length",
            not wrong,
            f"有 {len(wrong)} 条不是 truncated 却带 finish_reason："
            + str([(r.get('task_id'), r.get('status'),
                    r.get('finish_reason')) for r in wrong[:3]]))

        # 反过来也要查：成功的记录必须真的拿到了结束标记
        ok_no_fr = [r for r in ok
                    if not r.get("finish_reason") and not r.get("saw_done")]
        rep.check_true(
            f"{rid}: 成功记录都拿到了结束标记（finish_reason 或 [DONE]）",
            not ok_no_fr,
            f"有 {len(ok_no_fr)} 条既无 finish_reason 也没收到 [DONE] 却被算成功")
    else:
        rep.note(f"{rid}: 本次运行全部成功，"
                 f"边界④（失败请求计数）未被数据覆盖 —— 需要专门造样例")
    print()


if __name__ == "__main__":
    run(main)
