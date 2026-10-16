#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_02_token_counting.py —— 流式块与 token 的区别

═══════════════════════════════════════════════════════════════════════════
 对应题目（原文）
═══════════════════════════════════════════════════════════════════════════
 任务介绍·性能优化：
     「流式响应的一个数据块可能包含多个 token，请按实际 token 数统计。」

 tests/README.md：
     「围绕关键边界设计样例，如★流式块与 token 的区别」

═══════════════════════════════════════════════════════════════════════════
 这个边界为什么危险
═══════════════════════════════════════════════════════════════════════════
 服务器是【流式】返回的：每生成一点就推一个 SSE 数据块。
 客户端能数到"收了多少块"，但【块 ≠ token】：

     · 一个块可能含多个 token     → 按块统计会【少算】
     · 一个 token 也可能跨多块     → 按块统计会【多算】
     · 结束块、空块不计入内容      → 数块会把它们算进去

 如果统计代码写成 `output_tokens = 收到的块数`，吞吐会偏，
 ★ 而且不会报错 —— 只是数字悄悄错了。

═══════════════════════════════════════════════════════════════════════════
 判据（独立来源）
═══════════════════════════════════════════════════════════════════════════
 每条记录里有两个【互相独立】的来源：

     n_chunks       客户端数的 SSE 块数        ← 我自己数的
     output_tokens  服务端 timings.predicted_n ← llama.cpp 报的

 所以可以断言：
     ① output_tokens 必须注明来源（不能是"数出来的"）
     ② 块数与 token 数【本来就不相等】—— 这份数据要能证明这一点，
        否则说明采集方式有问题（比如恰好一块一 token，那就测不出区别）
     ③ 汇总里的 output_total 必须等于 Σ(output_tokens)，不能等于 Σ(n_chunks)
     ④ 边界：只有 1 个 token 时，TPOT 分母为 0，须有约定

 退出码：0 通过 / 1 有失败 / 2 脚本自身出错
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _testlib import Report, load_run, list_runs, run, successful   # noqa: E402


def main():
    runs = list_runs()
    rep = Report("test_02_token_counting —— 流式块 vs token")

    if not runs:
        rep.check_true("results/ 下有可测运行", False, "没有数据")
        return rep.finish()

    for rid in runs:
        _check_one(rid, rep)
    return rep.finish()


def _check_one(rid, rep):
    d = load_run(rid)
    rows = d["requests"]
    ok = successful(rows)
    if not ok:
        rep.note(f"{rid}: 没有成功记录，跳过")
        return

    print("═" * 72)
    print(f" 运行 {rid}（成功 {len(ok)} 条）")
    print("═" * 72)

    # ── ① token 数必须有明确来源，不能是"数块数出来的" ────────────────
    for r in ok:
        src = r.get("output_tokens_source")
        rep.check_true(
            f"{rid}/{r['request_id']}: output_tokens 注明了来源",
            bool(src), "缺少 output_tokens_source 字段")
        rep.check_true(
            f"{rid}/{r['request_id']}: 来源不是『数块数』",
            src != "chunks" and src != "n_chunks",
            f"来源是 {src!r} —— 那就等于按块统计了")

    # ── ② 块数与 token 数确实不等（证明这个边界真实存在）────────────
    eq = [r for r in ok if r.get("n_chunks") == r.get("output_tokens")]
    lt = [r for r in ok if (r.get("n_chunks") or 0) < (r.get("output_tokens") or 0)]
    gt = [r for r in ok if (r.get("n_chunks") or 0) > (r.get("output_tokens") or 0)]
    print(f"  块数 == token 数 : {len(eq):3d} 条")
    print(f"  块数 <  token 数 : {len(lt):3d} 条   （一块含多 token）")
    print(f"  块数 >  token 数 : {len(gt):3d} 条   （token 跨多块 / 含空块）")

    tot_tok = sum(r.get("output_tokens") or 0 for r in ok)
    tot_chk = sum(r.get("n_chunks") or 0 for r in ok)
    if tot_chk:
        diff_pct = (tot_tok / tot_chk - 1) * 100
        print(f"  合计 token {tot_tok}，块 {tot_chk}，"
              f"按块统计会偏 {diff_pct:+.1f}%")
        rep.note(f"{rid}: 按块统计会偏 {diff_pct:+.1f}% "
                 f"（这正是「按 token 统计」的必要性所在）")

    # 如果这份数据里块数恰好总等于 token 数，那是采集有问题 —— 测不出区别
    rep.check_true(
        f"{rid}: 数据里应存在『块数 ≠ token 数』的记录（否则测不出这个边界）",
        len(lt) + len(gt) > 0,
        f"全部都相等（{len(eq)} 条），说明采集方式使二者无法区分")

    # ── ③ 汇总必须按 token 数求和，不是按块数 ────────────────────────
    s = d["summary"] or {}
    formal = s.get("formal") or {}
    for ck in sorted(formal, key=lambda x: int(x)):
        conc = int(ck)
        sub = [r for r in ok if r.get("concurrency") == conc]
        if not sub:
            continue
        tk = (formal[ck].get("tokens") or {})
        if tk.get("output_total") is not None:
            rep.check(f"{rid} c{conc}: output_total = Σ(output_tokens)",
                      tk["output_total"], sum(r["output_tokens"] for r in sub))
            rep.check_true(
                f"{rid} c{conc}: output_total ≠ Σ(n_chunks)（不能按块统计）",
                tk["output_total"] != sum(r.get("n_chunks") or 0 for r in sub)
                or sum(r["output_tokens"] for r in sub)
                == sum(r.get("n_chunks") or 0 for r in sub),
                "汇总的 token 总数等于块总数 —— 疑似按块统计")

    # ── ④ 边界：只输出 1 个 token 时 TPOT 的分母 ─────────────────────
    one = [r for r in ok if r.get("output_tokens") == 1]
    if one:
        for r in one:
            v = r.get("tpot_excl_first_ms")
            rep.check_true(
                f"{rid}/{r['request_id']}: output_tokens=1 时 TPOT 有约定值",
                v is None or v == 0 or v > 0,
                f"得到 {v!r}（应为 None 或明确的哨兵值，不能是 NaN/异常）")
        rep.note(f"{rid}: 命中 {len(one)} 条『只输出 1 token』的边界样例")
    else:
        rep.note(f"{rid}: 没有『只输出 1 token』的记录 —— 这个除零边界未被覆盖，"
                 f"需要专门造样例（max_tokens=1 的请求）")

    # ── 附加：prompt 侧的恒等式（服务端口径自洽性）──────────────────
    ident_ok = ident_bad = 0
    for r in ok:
        a, b, t = (r.get("prompt_n_computed"), r.get("cache_hit_tokens"),
                   r.get("prompt_tokens"))
        if None in (a, b, t):
            continue
        if a + b == t:
            ident_ok += 1
        else:
            ident_bad += 1
    if ident_ok or ident_bad:
        rep.check_true(
            f"{rid}: 恒等式 prompt_n_computed + cache_hit == prompt_tokens",
            ident_bad == 0,
            f"{ident_bad} 条不满足（满足 {ident_ok} 条）")
        rep.note(f"{rid}: 服务端 token 恒等式在 {ident_ok} 条记录上成立"
                 f"（这验证了『输入 token 数』的口径）")
    print()


if __name__ == "__main__":
    run(main)
