#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
office_eval.py —— 办公类输出的【可自动核对项】。

═══════════════════════════════════════════════════════════════════════════
 为什么不能全靠自动判
═══════════════════════════════════════════════════════════════════════════
 题目要求「办公输出检查信息提取与事实依据」—— 事实是否忠于资料，
 最终要人来判。但有一批项【可以从资料里自动核对】，而且它们恰好是
 最容易错的地方：

   · 计划变更：资料里有两个会议，演示日期从 10/16 改成 10/20。
     题目明确要求"以日期较新的会议决定为依据"。模型要是写了 10/16
     就是没做版本仲裁 —— 这是可以断言的。
   · 关键事实：8 人工作室、正式交付 10/23、验收负责人郑宁、
     对外窗口林悦 —— 都能从资料里逐字核对。
   · 流程要求：D01 要求"资料未明确的信息标记为待确认"、
     D02 要求"注明关键信息来自哪个章节/哪次会议"、
     D03 要求"注明条款编号"、D04 要求"对应条款编号"。
     这些是【格式性要求】，能查字符串特征。

 本脚本只做这些"有唯一答案"的检查，把真正的质量判断留给人。
 输出结构化结果，供报告 §8 引用。
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parents[2]
RESULTS = DEV / "results"
INPUTS = (DEV.parent / "提示词与输入文档" / "输入文档")

# ── D01：项目管理资料（会议记录）────────────────────────────────────────
D01_FACTS = [
    # ⚠️ 正向核对：问"现行演示日期是不是 10/20"，
    #    而不是"有没有出现 10/16"（后者会把"原定 X 改为 Y"误判）。
    ("现行演示日期 = 10 月 20 日",
     r"10\s*月\s*20\s*日", "date_arbitration"),
    ("明确交代了演示日期的变更（原定/改为/调整）",
     r"(原定|改为|调整为|变更)", "date_arbitration"),
    ("正式交付 10 月 23 日",
     r"10\s*月\s*23\s*日", "fact"),
    ("验收负责人 郑宁", r"郑宁", "fact"),
    ("对外沟通窗口 林悦", r"林悦", "fact"),
    ("阻塞：updated_at 缺失", r"updated_at", "fact"),
    ("阻塞：上传上限/命名规则待客户确认",
     r"上传上限|命名规则|命名方式", "fact"),
    ("阻塞：历史工单状态映射待确认",
     r"状态映射|已完成|已结束", "fact"),
    ("阻塞：平均处理耗时口径未定",
     r"平均处理耗时|处理耗时", "fact"),
    ("下一步行动表含 负责人/截止/依赖",
     r"负责人", "format"),
]
# ⚠️⚠️ 关于"是否用了旧会议决定"这件事 —— 我放弃了自动判定
#
# 前后试了两版正则，**两次都误报**：
#
#   第一版：搜 `10月16日…演示`，排除 lookbehind 里的"调整为/改为"
#     误报：`原定 10 月 16 日的客户演示改为 10 月 20 日` → 判成用了旧日期
#
#   第二版：加了正向条件（"现行日期=10/20" + "有变更措辞"），
#           stale 只在"没有任何变更措辞"时才报
#     误报：`客户演示时间由原定 10 月 16 日调整为 10 月 20 日 14:00`
#           —— lookahead 只往前看 20 字，"调整为"在更远处，没兜住
#     又误报：`由**10月16日**调整为**10月20日14:00**`
#
# 结论：**"没有出现旧词"这类否定模式做不了自动判定** ——
#       正确表述里必然要提到旧值（"由 X 改为 Y"），
#       任何基于"出现了 X 就算错"的规则都会把正确输出判错。
#
# 正确的做法：
#   · 自动部分只做【正向核对】—— 查现行日期是不是 10/20、
#     有没有交代变更（这两条在 D01_FACTS 里，实测 9/9 命中）。
#   · "有没有混淆新旧决定"交给人判 —— 这是语义判断，不硬凑规则。
#
# 这里只保留一个**非常保守**的提示：整篇出现了旧日期，
# 但【通篇找不到任何变更措辞】时才提示人工看一眼。
# 实测 9 个运行全部有变更措辞，所以它不会误报。
D01_STALE = []          # 不再自动判"用了旧日期"

# 保守提示：旧日期在场 + 通篇无变更措辞 → 提示人工复核（不判失败）
def d01_needs_manual_review(text):
    has_old = bool(re.search(r"10\s*月\s*16\s*日", text))
    has_change_lang = bool(
        re.search(r"原定|改为|调整为|调整|变更|由.{0,6}(改为|调整)", text))
    return has_old and not has_change_lang

# ── D03：制度问答 ───────────────────────────────────────────────────────
D03_FACTS = [
    ("远程办公申请条款 A03", r"A03", "clause"),
    ("设备借用登记条款 C01", r"C01", "clause"),
    ("客户资料处理 C03", r"C03", "clause"),
    ("VPN/设备接入 D05", r"D05", "clause"),
    ("验收周补充流程", r"验收周", "format"),
    ("注明需确认的人员", r"唐禾|林悦|郑宁", "format"),
]

# ── D02：客户邮件 ───────────────────────────────────────────────────────
D02_FACTS = [
    ("已完成工作", r"已完成|完成", "format"),
    ("当前问题", r"问题|阻塞|待确认", "format"),
    ("近期交付安排", r"10\s*月\s*20\s*日|10\s*月\s*23\s*日", "fact"),
    ("需要客户确认的事项", r"确认", "format"),
    ("区分已批准与备选方案（后续评估项）",
     r"后续评估|备选|已批准|已确认", "format"),
]

# ── D04：入职清单 ───────────────────────────────────────────────────────
D04_FACTS = [
    ("清单含事项/时间/角色/材料/条款",
     r"条款|编号", "format"),
    ("单独列出接触客户资料前的事项",
     r"客户资料", "format"),
    ("工作地点/设备变化需补办的手续",
     r"地点|设备", "format"),
]

SPEC = {
    "D01": ("办公输入01-项目会议与进度资料.md", D01_FACTS, D01_STALE),
    "D02": ("办公输入01-项目会议与进度资料.md", D02_FACTS, []),
    "D03": ("办公输入02-工作室内部管理制度.md", D03_FACTS, []),
    "D04": ("办公输入02-工作室内部管理制度.md", D04_FACTS, []),
}


def check(tid, text):
    doc, facts, stale = SPEC[tid]
    hits, miss = [], []
    for label, pat, cat in facts:
        if re.search(pat, text):
            hits.append({"item": label, "category": cat})
        else:
            miss.append({"item": label, "category": cat})
    bad = []
    for pat, why in stale:
        if re.search(pat, text):
            bad.append(why)
    # D01 的"新旧决定混淆"只做保守提示，不判失败（见文件头说明）
    review = None
    if tid == "D01" and d01_needs_manual_review(text):
        review = "出现了旧日期但通篇无变更措辞 —— 建议人工看一眼原文"
    return {"hit": hits, "miss": miss, "stale": bad,
            "needs_manual_review": review, "source": doc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    runs = sorted([d for d in RESULTS.iterdir() if d.is_dir()])
    out = {}
    for d in runs:
        rq = d / "requests.jsonl"
        if not rq.is_file():
            continue
        cfg = json.loads((d / "run-config.json").read_text(encoding="utf-8"))
        rows = [json.loads(l) for l in
                rq.read_text(encoding="utf-8").splitlines() if l.strip()]
        per = {}
        for tid in SPEC:
            recs = [r for r in rows if r.get("task_id") == tid
                    and r.get("status") in ("success", "truncated")]
            if not recs:
                per[tid] = {"verdict": "no_record"}
                continue
            r = max(recs, key=lambda x: x.get("output_chars") or 0)
            txt = r.get("output_text") or ""
            c = check(tid, txt)
            c["status"] = r.get("status")
            c["chars"] = len(txt)
            c["output_tokens"] = r.get("output_tokens")
            mt = cfg.get("max_tokens")
            c["truncated"] = bool(
                (mt and (r.get("output_tokens") or 0) >= mt)
                or r.get("finish_reason") == "length")
            c["verdict"] = "stale_decision" if c["stale"] else (
                "hit_all" if not c["miss"] else "partial")
            per[tid] = c
        out[d.name] = {
            "ctx": cfg.get("server_ctx"),
            "kv": "%s/%s" % (cfg.get("kv_type_k"), cfg.get("kv_type_v")),
            "model": cfg.get("model_file"),
            "tasks": per,
        }

    print("=" * 96)
    print(" 办公类输出核对（可自动判的项）")
    print("=" * 96)
    for rid, v in out.items():
        print("  ── %s  ctx=%s KV=%s %s" % (
            rid, v["ctx"], v["kv"],
            (v["model"] or "").replace("Qwen3-", "").replace("-Q8_0.gguf", "")))
        for tid, c in v["tasks"].items():
            if c.get("verdict") == "no_record":
                print("     %-4s 无记录" % tid)
                continue
            n_hit, n_miss = len(c["hit"]), len(c["miss"])
            flag = {"hit_all": "✅", "partial": "🟡",
                    "stale_decision": "⚠️"}[c["verdict"]]
            tr = "  ★截断" if c.get("truncated") else ""
            print("     %s %-4s %-14s 命中 %d/%d  %s%s" % (
                flag, tid, c["verdict"], n_hit, n_hit + n_miss,
                ("漏：" + "、".join(m["item"] for m in c["miss"])[:60])
                if c["miss"] else "", tr))
            for s in c["stale"]:
                print("        ⚠️ %s" % s)
        print()

    # 汇总
    print("=" * 96)
    print(" 汇总")
    print("=" * 96)
    from collections import Counter
    cnt = {t: Counter() for t in SPEC}
    for v in out.values():
        for tid, c in v["tasks"].items():
            cnt[tid][c.get("verdict")] += 1
    print("  %-5s %-10s %-10s %-16s %s" % (
        "任务", "hit_all", "partial", "stale_decision", "总"))
    for tid, c in cnt.items():
        print("  %-5s %-10s %-10s %-16s %s" % (
            tid, c["hit_all"], c["partial"], c["stale_decision"],
            sum(c.values())))

    if args.json:
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n  已写入 %s" % args.json)


if __name__ == "__main__":
    main()
