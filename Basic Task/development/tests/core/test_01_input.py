#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_01_input.py —— 输入组织与 token 预算

═══════════════════════════════════════════════════════════════════════════
 对应题目（原文）
═══════════════════════════════════════════════════════════════════════════
 任务介绍·本地推理服务：
     「系统能够接收纯提示词，也能够读取指定文件，将文件内容与关联提示词
       组织为模型输入。」
     「记录聊天模板处理后的实际输入 token 数及输出上限。」

 任务介绍·规定输入与输出检查：
     「输入提示词、关联文件、系统消息与聊天模板合计控制在
       ★50,000 tokens 以内，同时满足模型的上下文容量。」

 测试提示词·使用方式：
     「建议输入顺序：系统消息 → 用户消息中的资料区（如有）→ 任务区。」

 提交标准·验收依据：
     「能调用推理服务、读取输入文件并展示模型输出。」

═══════════════════════════════════════════════════════════════════════════
 判据（独立来源）
═══════════════════════════════════════════════════════════════════════════
 ★ 上限 50,000 是【题目规定的外部常量】—— 不来自我们的代码。
   这是最硬的一条独立来源：拿题目的规矩去查我们的数据。

 其余判据来自两类对照：
     · 配置声明 vs 实际记录（任务的关联文件对不对得上）
     · 服务端实测 vs 本地分段计数（模板开销必须为正，且可解释）

═══════════════════════════════════════════════════════════════════════════
 关键边界样例
═══════════════════════════════════════════════════════════════════════════
   ① 上限边界   恰好 50,000 / 超 1 个 —— 必须 ≤ 50000
   ② 上下文边界 输入 + 输出预留 ≤ 模型 ctx（否则会被 400 拒绝）
   ③ 模板开销   服务端实测 > 本地分段和（模板必然增加 token，不会减少）
   ④ 任务类型   4 纯提示词 + 4 办公文档 + 2 源文件（题目规定的构成）
   ⑤ 文件校验   关联文件必须有 sha256，且同一文件多次引用应一致

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
from _testlib import Report, load_run, list_runs, run   # noqa: E402

LIMIT_TOKENS = 50000        # ★ 题目原文规定的上限

# 题目规定的任务构成：4 个纯提示词 + 4 个关联办公文档 + 2 个关联源文件
#
# ⚠️ 这里的键必须与【数据里 kind 字段的真实取值】一致。
#    common/tasks.py 用的取值是 prompt / doc / code，
#    早先我按题目措辞写成 office，结果报了假失败 —— 查真实数据才确认。
KIND_EXPECT = {"prompt": 4, "doc": 4, "code": 2}


def main():
    runs = list_runs()
    rep = Report("test_01_input —— 输入组织与 token 预算")

    if not runs:
        rep.check_true("results/ 下有可测运行", False, "没有数据")
        return rep.finish()

    for rid in runs:
        _check_one(rid, rep)

    # ══════════════════════════════════════════════════════════════════
    #  跨运行的结构性要求
    #
    #  ⚠️ 这几条【不能放在单次运行里判】：
    #     题目说「使用测试提示词中的 10 个任务，输入包括 4 个纯提示词
    #     任务、4 个关联办公文档任务及 2 个关联源文件任务」——
    #     这是对【整个测试集】的要求，不是对每一次运行的要求。
    #     小配置（baseline-c1）本来只跑 5 个任务、只做单并发冒烟，
    #     拿它去要求 10 个任务是错的（早先这么写，报了 5 个假失败）。
    # ══════════════════════════════════════════════════════════════════
    print()
    print("═" * 72)
    print(" 跨运行：整套测试是否覆盖题目规定的 10 个任务")
    print("═" * 72)
    all_kinds = Counter()
    all_tids = set()
    full_runs = []
    for rid in runs:
        d = load_run(rid)
        idx = d["prompt_index"] or {}
        for v in idx.values():
            all_kinds[v.get("kind")] += 1
            all_tids.add(v.get("task_id"))
        if len(idx) >= sum(KIND_EXPECT.values()):
            full_runs.append(rid)
    print(f"  所有运行合计覆盖的任务编号：{sorted(all_tids)}")
    print(f"  跑满 10 个任务的运行：{full_runs or '（没有）'}")

    rep.check_true(
        "整套测试里存在跑满 10 个任务的运行",
        bool(full_runs),
        "题目规定使用 10 个规定任务，至少要有一个完整运行")

    # 10 个任务编号必须齐全（这是最硬的一条：编号是题目给的）
    want_tids = {"P01", "P02", "P03", "P04",
                 "D01", "D02", "D03", "D04", "C01", "C02"}
    rep.check_true(
        "整套测试覆盖了题目规定的全部 10 个任务编号",
        want_tids.issubset(all_tids),
        f"缺 {sorted(want_tids - all_tids)}")

    # 类型构成：以【跑满的那次运行】为准
    for rid in full_runs:
        d = load_run(rid)
        kinds = Counter(v.get("kind") for v in (d["prompt_index"] or {}).values())
        print(f"  {rid} 的类型分布：{dict(kinds)}")
        for kind, want in KIND_EXPECT.items():
            rep.check(f"{rid}: 类型 {kind} 的任务数", kinds.get(kind, 0), want)

    return rep.finish()


def _check_one(rid, rep):
    d = load_run(rid)
    idx = d["prompt_index"]
    cfg = d["run_config"] or {}
    meta = d["meta"] or {}

    print("═" * 72)
    print(f" 运行 {rid}")
    print("═" * 72)

    if not idx:
        rep.check_true(f"{rid}: 存在 prompt-index.json", False)
        return

    print(f"  提示词索引 {len(idx)} 条")

    # ── ① 上限：题目规定的 50,000 —— 最硬的独立来源 ──────────────────
    over = []
    for tid, v in idx.items():
        n = (v.get("tokens") or {}).get("counted_total_input")
        if n is None:
            rep.check_true(f"{rid}/{tid}: 记录了输入 token 总数", False,
                           "缺 counted_total_input")
            continue
        if n > LIMIT_TOKENS:
            over.append((tid, n))
        # 恰好卡边界也报出来（边界样例①）
        if n == LIMIT_TOKENS:
            rep.note(f"{rid}/{tid}: 恰好等于上限 {LIMIT_TOKENS}（边界样例）")
    rep.check_true(
        f"{rid}: 每条输入 ≤ {LIMIT_TOKENS} tokens（题目规定）",
        len(over) == 0,
        f"超出：{over}")

    # ── ② 上下文容量：输入 + 输出预留 ≤ 模型 ctx ─────────────────────
    ctx = (meta.get("model") or {}).get("n_ctx")
    max_out = cfg.get("max_tokens")
    if ctx and max_out:
        worst = max(((v.get("tokens") or {}).get("counted_total_input") or 0)
                    for v in idx.values())
        need = worst + max_out
        rep.check_true(
            f"{rid}: 最长输入({worst}) + 输出预留({max_out}) ≤ ctx({ctx})",
            need <= ctx,
            f"需要 {need} > ctx {ctx} —— 会被服务端 400 拒绝")
        rep.note(f"{rid}: 最长输入 {worst} + 输出预留 {max_out} = {need}，"
                 f"ctx {ctx}，余量 {ctx - need}")

    # ── ③ 模板开销必须为正（服务端实测 > 本地分段和）────────────────
    bad_overhead = []
    n_with_overhead = 0
    for tid, v in idx.items():
        tk = v.get("tokens") or {}
        ov = tk.get("template_overhead")
        loc = tk.get("sum_without_template")
        srv = tk.get("counted_total_input")
        if None in (ov, loc, srv):
            continue
        n_with_overhead += 1
        if ov <= 0:
            bad_overhead.append((tid, ov))
        if srv != loc + ov:
            bad_overhead.append((tid, f"{srv} != {loc}+{ov}"))
    rep.check_true(
        f"{rid}: 聊天模板开销为正，且 服务端实测 = 本地分段 + 开销",
        len(bad_overhead) == 0,
        f"异常：{bad_overhead[:5]}")
    if n_with_overhead:
        rep.note(f"{rid}: {n_with_overhead} 条记录了模板开销"
                 f"（题目要求「记录聊天模板处理后的实际输入 token 数」）")

    # ── ④ 任务构成：只在【跑满 10 个任务】的运行里判（跨运行判定见 main）──
    #
    #  ⚠️ 早先这里对每一次运行都要求 4+4+2，结果 baseline-c1（5 任务的
    #     单并发冒烟配置）报了 4 个假失败。题目那句是对【测试集】的要求。
    kinds = Counter(v.get("kind") for v in idx.values())
    print(f"  任务类型分布: {dict(kinds)}")
    if len(idx) >= sum(KIND_EXPECT.values()):
        for kind, want in KIND_EXPECT.items():
            rep.check(f"{rid}: 类型 {kind} 的任务数", kinds.get(kind, 0), want)
        rep.check(f"{rid}: 任务总数", len(idx), sum(KIND_EXPECT.values()))
    else:
        rep.note(f"{rid}: 只跑了 {len(idx)} 个任务（小型配置，"
                 f"任务构成改在跨运行汇总里判）")

    # ── ⑤ 关联文件：必须有 sha256，同一文件多处引用要一致 ─────────────
    doc_tasks = {t: v for t, v in idx.items() if v.get("file")}
    print(f"  关联文件的任务 {len(doc_tasks)} 个")
    for tid, v in doc_tasks.items():
        rep.check_true(f"{rid}/{tid}: 关联文件记录了 sha256",
                       bool(v.get("file_sha256")),
                       "题目要求记录文件校验值")
        rep.check_true(f"{rid}/{tid}: 关联文件记录了字节数",
                       bool(v.get("file_bytes")))

    # 同一文件被多个任务引用时，sha256 必须一致（否则文件在运行中变了）
    by_file = {}
    for tid, v in doc_tasks.items():
        by_file.setdefault(v["file"], set()).add(v.get("file_sha256"))
    for fname, shas in by_file.items():
        rep.check(f"{rid}: 文件 {fname} 的 sha256 唯一（多次引用一致）",
                  len(shas), 1)
    if by_file:
        rep.note(f"{rid}: 引用了 {len(by_file)} 个不同文件，"
                 f"分别被 {dict((k, sum(1 for v in doc_tasks.values() if v['file']==k)) for k in by_file)} 个任务引用")

    # ── 输入顺序：系统消息 → 资料区 → 任务区（题目建议）───────────────
    has_sys = sum(1 for v in idx.values() if v.get("system_message"))
    rep.check_true(
        f"{rid}: 记录了系统消息（题目建议的顺序里它排第一）",
        has_sys > 0,
        f"{has_sys} 条有系统消息")

    # ── 输出上限必须被记录 ───────────────────────────────────────────
    rep.check_true(f"{rid}: 配置记录了输出上限 max_tokens",
                   cfg.get("max_tokens") is not None,
                   "题目要求「记录……及输出上限」")
    print()


if __name__ == "__main__":
    run(main)
