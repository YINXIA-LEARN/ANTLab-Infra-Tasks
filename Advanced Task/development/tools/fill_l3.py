#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_l3.py —— 往 L2 里塞新的大文档，把 L2 挤爆，让 L3 动起来（给可视化造数据）

做法：
  1. 生成 2 份新的大文档（约 5600 token，各约 620 MiB KV）
  2. 各请求一次 → 代理 save 到 L2
  3. L2 只有 3.5 GiB 且已满 → 触发整批搬家到 L3
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
CORPUS = DEV / "corpus/large"
PROXY = "http://127.0.0.1:8091"

NAMES = ["林悦", "周宁", "陈珂", "宋岚", "许舟", "沈遥", "唐禾"]


def make_large(tag, title, n_chars):
    lines = [f"# {title}", "",
             f"资料版本：2026-10-16-v1。本文为虚构项目资料。", "",
             "## 1. 项目背景", "",
             f"本项目由澄禾工作室承接，代号 {tag.upper()}。"
             f"客户为虚构的海岳数据服务有限公司。项目周期 9 周，"
             f"正式交付日期为 2026 年 12 月 5 日。", ""]
    i = 2
    while sum(len(x) for x in lines) < n_chars:
        d = 10 + (i % 18)
        who = NAMES[i % len(NAMES)]
        lines.append(f"## {i}. 第 {i} 次评审（10 月 {d} 日）")
        lines.append("")
        lines.append(f"参会：{who}、{NAMES[(i+2) % len(NAMES)]}。")
        lines.append("")
        lines.append(f"本次确定：模块 N{i:02d} 于 10 月 {d} 日前完成联调，"
                     f"由 {who} 负责；覆盖率 {60 + i % 35}% 方可验收；"
                     f"遗留缺陷 {i % 5} 个。")
        lines.append("")
        i += 1
    return "\n".join(lines)


def ask(doc_text, q):
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁。"},
        {"role": "user", "content": doc_text},
        {"role": "user", "content": q}],
        "max_tokens": 48, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    r = urllib.request.Request(PROXY + "/v1/chat/completions",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=900) as f:
        return json.loads(f.read())


def status():
    with urllib.request.urlopen(PROXY + "/admin/status", timeout=10) as f:
        return json.loads(f.read())


def main():
    new = [("proj-d", "海岳数据平台项目全记录"),
           ("proj-e", "客户门户改版项目全记录")]
    for tag, title in new:
        p = CORPUS / f"{tag}-{title}.md"
        if not p.exists():
            p.write_text(make_large(tag, title, 7000), encoding="utf-8")
            print(f"  生成 {p.name}  {p.stat().st_size} 字节")

    for tag, title in new:
        p = CORPUS / f"{tag}-{title}.md"
        t0 = time.time()
        d = ask(p.read_text(encoding="utf-8"), "这个项目的交付日期是哪天？")
        px = d.get("_proxy") or {}
        tm = d.get("timings") or {}
        s = status()
        t = s.get("tiers") or {}
        print(f"  {p.name[:26]:<26} mode={px.get('mode'):<7} "
              f"prompt_n={tm.get('prompt_n')}  "
              f"L2={t.get('l2_used',0)/1048576:.0f}M  "
              f"L3={t.get('l3_used',0)/1048576:.0f}M  "
              f"({(time.time()-t0):.1f}s)")
        time.sleep(1)

    s = status()
    st = s.get("stat") or {}
    t = s.get("tiers") or {}
    print()
    print(f"  最终 L2={t.get('l2_used',0)/1048576:.0f} / {t.get('l2_quota',0)/1048576:.0f} MiB")
    print(f"  最终 L3={t.get('l3_used',0)/1048576:.0f} / {t.get('l3_quota',0)/1048576:.0f} MiB")
    print(f"  spills={st.get('spills')} entries={s.get('entries')}")


if __name__ == "__main__":
    main()
