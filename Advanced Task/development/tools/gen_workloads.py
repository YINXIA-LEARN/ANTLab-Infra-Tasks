#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_workloads.py —— 依据 corpus/ 生成 6 份负载定义到 configs/workloads/

设计依据（计划书 §10.1）：
  S1 单文档重复   ：测最高命中率
  S2 小文档轮转   ：L1 只有 4 槽，8 份小文档必然换入换出
  S3 大文档竞争   ：3 份大文档 × 各 5 次
  S4 冷热混合     ：热 2 × 10 + 温 3 × 3 + 冷 6 × 1 → 测淘汰
  S5 版本更新     ：v1 → v2，测失效判定与轻微改动
  S6 跨重启       ：配合 --restart-first，测 L2 丢失、L3 生效
"""

import json
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
CORPUS = DEV / "corpus"
OUT = DEV / "configs" / "workloads"

QUESTIONS = [
    "这份资料的核心要求是什么？",
    "有哪些关键的时间期限？",
    "责任人需要做什么？",
    "金额或数量的上限是多少？",
    "遇到例外情况怎么处理？",
    "如果违规会有什么后果？",
    "总结一下最关键的三点。",
    "这份资料适用于哪些场景？",
]


def small_docs():
    return sorted(p.name for p in (CORPUS / "small").glob("*.md"))


def large_docs():
    return sorted(p.name for p in (CORPUS / "large").glob("*.md"))


def build(name, desc, items, max_tokens=96):
    reqs = []
    for i, (at_ms, doc, q, user) in enumerate(items, 1):
        reqs.append({
            "id": f"{name}-r{i:02d}",
            "at_ms": at_ms,
            "doc": doc,
            "q": q,
            "user": user,
            "max_tokens": max_tokens,
        })
    return {"name": name, "desc": desc, "max_tokens": max_tokens,
            "requests": reqs}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    S, L = small_docs(), large_docs()
    assert len(S) >= 8 and len(L) >= 3, f"语料不够: S={S} L={L}"

    wl = {}

    # ── S1 单文档重复：一份大文档问 10 次 ──
    items = [(i * 1200, f"large/{L[0]}", QUESTIONS[i % len(QUESTIONS)],
              "U01") for i in range(10)]
    wl["S1"] = build("S1-单文档重复",
                     "同一份大文档连问 10 次，理想情况只有第一次要全量算",
                     items)

    # ── S2 小文档轮转：8 份 × 3 轮，槽只有 4 个 ──
    items, t = [], 0
    for r in range(3):
        for j, d in enumerate(S):
            items.append((t, f"small/{d}", QUESTIONS[(r * 3 + j) % len(QUESTIONS)],
                          f"U{(j % 7) + 1:02d}"))
            t += 700
    wl["S2"] = build("S2-小文档轮转",
                     "8 份小文档各问 3 次，槽只有 4 个 → 必然换入换出",
                     items)

    # ── S3 大文档竞争：3 份 × 5 次 ──
    items, t = [], 0
    for r in range(5):
        for j, d in enumerate(L):
            items.append((t, f"large/{d}",
                          QUESTIONS[(r + j) % len(QUESTIONS)], f"U{j+1:02d}"))
            t += 2500
    wl["S3"] = build("S3-大文档竞争",
                     "3 份大文档各问 5 次，看 L2 与 L3 的分布",
                     items)

    # ── S4 冷热混合：热 2×10 + 温 3×3 + 冷 6×1 ──
    items, t = [], 0
    hot = S[:2]
    warm = S[2:5]
    cold = ([f"small/{d}" for d in S[5:8]]
            + ["versions/知识库-v1.md", "versions/知识库-v2.md",
               f"large/{L[0]}"])
    for k in range(10):
        for j, d in enumerate(hot):
            items.append((t, f"small/{d}",
                          QUESTIONS[(k + j) % len(QUESTIONS)],
                          f"U{(j % 7) + 1:02d}"))
            t += 800
        if k < 3:
            for j, d in enumerate(warm):
                items.append((t, f"small/{d}",
                              QUESTIONS[(k + j + 2) % len(QUESTIONS)],
                              f"U{(j + 3) % 7 + 1:02d}"))
                t += 800
        if k < 1:
            for j, d in enumerate(cold):
                items.append((t, d, QUESTIONS[(j + 5) % len(QUESTIONS)],
                              f"U{(j + 5) % 7 + 1:02d}"))
                t += 1500
    wl["S4"] = build("S4-冷热混合",
                     "热 2 份各 10 次 + 温 3 份各 3 次 + 冷 6 份各 1 次 → 测淘汰",
                     items)

    # ── S5 版本更新：v1 三次，然后 v2 三次 ──
    items, t = [], 0
    for k in range(3):
        items.append((t, "versions/知识库-v1.md", "设备借用期限是几天？", "U01"))
        t += 2000
    for k in range(3):
        items.append((t, "versions/知识库-v2.md", "设备借用期限是几天？", "U02"))
        t += 2000
    wl["S5"] = build("S5-版本更新",
                     "v1 问 3 次后切到 v2。二轮要求：v2 必须用新事实，且能复用前缀",
                     items)

    # ── S6 跨重启：3 份大文档各 3 次，配合 --restart-first ──
    items, t = [], 0
    for r in range(3):
        for j, d in enumerate(L):
            items.append((t, f"large/{d}",
                          QUESTIONS[(r + j + 1) % len(QUESTIONS)], f"U{j+1:02d}"))
            t += 2500
    wl["S6"] = build("S6-跨重启",
                     "配合 --restart-first 使用：重启后 L2 清空、L3 保留",
                     items)

    for name, w in wl.items():
        p = OUT / f"{name}.json"
        p.write_text(json.dumps(w, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        docs = sorted({r["doc"] for r in w["requests"]})
        span = max(r["at_ms"] for r in w["requests"])
        print(f"  ✅ {p.name:<10} {len(w['requests']):>3} 条请求  "
              f"{len(docs)} 份文档  跨度 {span/1000:.1f}s")

    print(f"\n写入 {OUT}（6 份负载）")


if __name__ == "__main__":
    main()
