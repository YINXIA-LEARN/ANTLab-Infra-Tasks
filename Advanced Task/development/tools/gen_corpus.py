#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_corpus.py —— 生成进阶项目的测试语料

依据计划书 §10.1：
  8 份小文档（约 800 token）
  3 份大文档（约 4000 token）
  1 对 v1/v2（用于测"轻微改动"）

每份文档都带【可核查的事实】（编号、数字、人名），
这样后面能检查"有缓存和没缓存的输出是否一致""v2 是否用了新事实"。
"""

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
OUT = Path(__file__).resolve().parents[1] / "corpus"


def count_tokens(text):
    """用服务端 tokenizer 精确计数（与推理时一致）"""
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(BASE + "/tokenize", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return len(d.get("tokens") or [])


SMALL_TOPICS = [
    ("alpha", "客户对接规范"),
    ("beta", "代码评审流程"),
    ("gamma", "设备借用登记"),
    ("delta", "数据备份策略"),
    ("epsilon", "会议纪要模板"),
    ("zeta", "远程办公申请"),
    ("eta", "客户资料分级"),
    ("theta", "交付验收清单"),
]

LARGE_TOPICS = [
    ("proj-a", "星桥门户项目全记录"),
    ("proj-b", "知识库迁移项目全记录"),
    ("proj-c", "工单系统重构项目全记录"),
]


def make_small(tag, title, n_chars):
    """小文档：一条制度，含具体条款编号与数字"""
    lines = [f"# {title}（{tag}）", "",
             f"资料版本：2026-10-{10 + len(tag) % 10:02d}-v1。本文为虚构办公测试资料。", ""]
    i = 1
    while sum(len(x) for x in lines) < n_chars:
        lines.append(f"## {i}. 条款 {tag.upper()}-{i:02d}")
        lines.append(f"本条款规定：{title}相关事项第 {i} 项，"
                     f"需在 {i + 2} 个工作日内完成登记，责任人需在系统内确认。"
                     f"金额上限为 {i * 1000} 元，超出部分需项目负责人单独审批。")
        lines.append(f"例外情形：当 {i % 3 + 1} 人以上同时申请时，"
                     f"可由值班主任先行批准，事后于 {i + 1} 个工作日内补录。")
        lines.append("")
        i += 1
    return "\n".join(lines)


def make_large(tag, title, n_chars):
    """大文档：项目全记录，多章节、多日期、多人名"""
    names = ["林悦", "周宁", "陈珂", "宋岚", "许舟", "沈遥", "唐禾"]
    lines = [f"# {title}", "",
             f"资料版本：2026-10-15-v1。本文为虚构项目资料，会议日期属于项目场景。", "",
             f"## 1. 项目背景", "",
             f"本项目由澄禾工作室承接，代号 {tag.upper()}。"
             f"客户为虚构的星桥设计服务有限公司。项目周期 12 周，"
             f"正式交付日期为 2026 年 11 月 20 日。", ""]
    i = 2
    while sum(len(x) for x in lines) < n_chars:
        d = 10 + (i % 18)
        who = names[i % len(names)]
        lines.append(f"## {i}. 第 {i} 次周会纪要（10 月 {d} 日）")
        lines.append("")
        lines.append(f"参会：{who}、{names[(i+1) % len(names)]}、{names[(i+2) % len(names)]}。")
        lines.append("")
        lines.append(f"本次确定：模块 M{i:02d} 于 10 月 {d} 日前完成接口联调，"
                     f"由 {who} 负责；测试覆盖率达到 {70 + i % 25}% 方可进入验收；"
                     f"遗留缺陷 {i % 7} 个，优先级 P{1 + i % 3}。")
        lines.append("")
        lines.append(f"风险：客户规则尚未书面确认，影响 {i % 4 + 1} 个功能点。"
                     f"应对：由 {names[(i+3) % len(names)]} 在 10 月 {d + 2} 日前取得书面回复。")
        lines.append("")
        i += 1
    return "\n".join(lines)


def make_vpair(n_chars):
    """
    v1 / v2 成对：前面大段【完全相同】，只在末尾改关键事实。

    ⚠️ 第一版写错了：把版本号放在开头（2026-10-05-v1 vs 2026-10-18-v2），
       结果公共前缀只有 20 token（0.6%），完全测不出"轻微改动"。
       现在改成：头部完全一致（含"版本号见文末"），版本号与事实都放末尾。
    """
    head = [f"# 工作室项目知识库", "",
            "资料版本：见文末「关键事实」。本文为虚构办公测试资料。", "",
            "## 1. 设备借用", "",
            "员工借用工作室设备需填写登记表，注明设备编号、用途与预计归还日期。", ""]
    i = 2
    while sum(len(x) for x in head) < n_chars - 900:
        head.append(f"## {i}. 通用条款")
        head.append("")
        head.append(f"第 {i} 条规定：所有内部流程须在系统内留痕，"
                    f"审批链不超过 {i % 3 + 2} 级，超时未处理自动升级。")
        head.append("")
        i += 1

    v1_tail = [
        "## 关键事实",
        "",
        "资料版本：2026-10-05-v1。",
        "",
        "- 设备借用期限：**7 天**",
        "- 客户回访期限：**3 个工作日**",
        "- 数据备份保留期：**30 天**",
        "- 值班负责人：唐禾",
        "",
    ]
    v2_tail = [
        "## 关键事实",
        "",
        "资料版本：2026-10-18-v2。",
        "",
        "- 设备借用期限：**14 天**",
        "- 客户回访期限：**5 个工作日**",
        "- 数据备份保留期：**90 天**",
        "- 值班负责人：沈遥",
        "",
    ]
    v1 = "\n".join(head) + "\n".join(v1_tail)
    v2 = "\n".join(head) + "\n".join(v2_tail)
    return v1, v2


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--no-token":
        need_tokens = False
    else:
        need_tokens = True

    if need_tokens:
        try:
            urllib.request.urlopen(BASE + "/health", timeout=5).close()
        except Exception:
            print(f"❌ 模型服务不在线（{BASE}/health）")
            print("   先启动 llama-server，或加 --no-token 跳过 token 计数")
            return 1

    (OUT / "small").mkdir(parents=True, exist_ok=True)
    (OUT / "large").mkdir(parents=True, exist_ok=True)
    (OUT / "versions").mkdir(parents=True, exist_ok=True)

    manifest = []

    def emit(path, text):
        path.write_text(text, encoding="utf-8")
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        n = count_tokens(text) if need_tokens else None
        manifest.append({
            "file": str(path.relative_to(OUT)),
            "bytes": len(text.encode("utf-8")),
            "sha256": h,
            "tokens": n,
        })
        print(f"  ✅ {path.name:28s} {len(text.encode('utf-8')):6d} 字节  "
              + (f"{n:5d} token" if n else "     - token"))
        return n

    print("=" * 72)
    print(" 小文档（目标约 800 token）")
    print("=" * 72)
    for tag, title in SMALL_TOPICS:
        emit(OUT / "small" / f"{tag}-{title}.md", make_small(tag, title, 1400))

    print()
    print("=" * 72)
    print(" 大文档（目标约 4000 token）")
    print("=" * 72)
    for tag, title in LARGE_TOPICS:
        emit(OUT / "large" / f"{tag}-{title}.md", make_large(tag, title, 7000))

    print()
    print("=" * 72)
    print(" v1 / v2 成对（用于测轻微改动）")
    print("=" * 72)
    v1, v2 = make_vpair(5000)
    emit(OUT / "versions" / "知识库-v1.md", v1)
    emit(OUT / "versions" / "知识库-v2.md", v2)

    # v1/v2 的公共前缀统计
    print()
    print("=" * 72)
    print(" v1 → v2 的公共前缀（这就是「轻微改动」能复用多少）")
    print("=" * 72)
    if need_tokens:
        def toks(t):
            body = json.dumps({"content": t}).encode()
            req = urllib.request.Request(BASE + "/tokenize", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read()).get("tokens") or []
        a, b = toks(v1), toks(v2)
        lcp = 0
        for x, y in zip(a, b):
            if x != y:
                break
            lcp += 1
        print(f"  v1 = {len(a)} token，v2 = {len(b)} token")
        print(f"  公共前缀 = {lcp} token  = {lcp/max(1,len(a))*100:.1f}% of v1")
        print(f"  需要重算 = {len(b)-lcp} token")
        print(f"  → 若走「轻微改动」路径，只需重算 {len(b)-lcp} 个 token")

    (OUT / "manifest.json").write_text(
        json.dumps({"generated_at": "2026-10-18", "docs": manifest},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"清单已写入 {OUT/'manifest.json'}（{len(manifest)} 份文档）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
