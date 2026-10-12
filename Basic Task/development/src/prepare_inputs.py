#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_inputs.py —— 组装 10 个规定任务的模型输入，并核算 token 预算

用法：
    # 看单个任务
    python3 prepare_inputs.py --task D01

    # 看全部 10 个任务的汇总表
    python3 prepare_inputs.py --all

    # 导出 JSON（给测试程序用）
    python3 prepare_inputs.py --all --export /path/to/inputs.json

    # 不连服务端（只做本地精确计数）
    python3 prepare_inputs.py --all --no-probe

为什么要有这个工具：
    题目要求"记录聊天模板处理后的实际输入 token 数"，
    并且在测试前就要确认「合计 ≤ 50,000 tokens」且「放得进上下文」。
    先离线把 10 个任务的账算清楚，再跑正式测试，
    避免测试跑到一半才发现某个任务超限。
"""

import argparse
import json
import sys
from pathlib import Path

# 允许直接以脚本方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import file_input, tasks  # noqa: E402

DEFAULT_INPUT_DIR = (
    Path.home() / "antlab/ANTLab-Infra-Tasks/Basic Task/提示词与输入文档/输入文档"
)


def main():
    ap = argparse.ArgumentParser(
        description="组装规定任务的模型输入并核算 token 预算")
    ap.add_argument("--task", help="单个任务编号，如 D01")
    ap.add_argument("--all", action="store_true", help="处理全部 10 个任务")
    ap.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR),
                    help=f"输入文档目录（默认 {DEFAULT_INPUT_DIR}）")
    ap.add_argument("--model", default="Qwen3-0.6B-Q8_0.gguf",
                    help="用于 token 计数的模型文件（默认 0.6B，词表与 1.7B 相同）")
    ap.add_argument("--ctx", type=int, default=16384,
                    help="KV 池总容量，即 -c（默认 16384）。"
                         "⚠️ kv_unified 下这是所有 slot 共享的池，不是每槽值")
    ap.add_argument("--max-output", type=int, default=2048,
                    help="为输出预留的 token 数（默认 2048）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--no-probe", action="store_true",
                    help="不连服务端探测，只做本地精确计数")
    ap.add_argument("--export", help="把结果导出为 JSON 文件")
    args = ap.parse_args()

    if not args.task and not args.all:
        ap.error("请指定 --task <编号> 或 --all")

    ids = tasks.all_ids() if args.all else [args.task]
    for tid in ids:
        if tid not in tasks.TASKS_BY_ID:
            print(f"❌ 未知任务编号 {tid}；可用：{', '.join(tasks.all_ids())}")
            sys.exit(1)

    print(f"输入文档目录：{args.input_dir}")
    print(f"计数用模型　：{args.model}")
    print(f"KV 池总容量　：{args.ctx}（共享池，非每槽）    输出预留：{args.max_output}")
    print(f"服务端探测　：{'关闭' if args.no_probe else args.base_url}")
    print()

    results = []
    for tid in ids:
        try:
            info = file_input.prepare(
                tid, args.input_dir, args.model, args.ctx, args.max_output,
                base_url=args.base_url, want_probe=not args.no_probe)
        except file_input.InputBuildError as e:
            print(f"❌ {tid} 组装失败：{e}")
            continue

        if args.all:
            # 汇总模式：一行一个任务
            t, b = info["tokens"], info["budget"]
            flag = "✅" if b["ok"] else "❌"
            srv = t["server_prompt_tokens"]
            results.append(info)
            print(f"  {flag} {tid}  {info['task_kind']:<6} "
                  f"系统{t['system']:>5} + 资料{t['doc']:>5} + 任务{t['task']:>4} "
                  f"+ 模板{t['template_overhead']:>3} = {b['total_input_tokens']:>6}"
                  f"  服务端 {str(srv):>6}  "
                  f"余量 {b['headroom_tokens']:>6}")
        else:
            print(file_input.describe_prepared(info))
            results.append(info)

    if args.all and results:
        print()
        worst = max(results, key=lambda r: r["budget"]["total_input_tokens"])
        allok = all(r["budget"]["ok"] for r in results)
        total_note = (f"最长任务 {worst['task_id']}："
                      f"{worst['budget']['total_input_tokens']} token")
        print(f"  最长任务：{total_note}")
        print(f"  全部通过：{'✅ 是' if allok else '❌ 否'}")
        svr = [r["tokens"]["server_prompt_tokens"] for r in results
               if r["tokens"]["server_prompt_tokens"] is not None]
        if svr:
            print(f"  服务端实测最长：{max(svr)} token")
        print()
        print(f"  三个输入文件合计 token（题目要求同时控制这套总量）：")
        seen = {}
        for r in results:
            d = r.get("doc")
            if d:
                seen[d["path"]] = r["tokens"]["doc"]
        for p, n in seen.items():
            print(f"    {Path(p).name:<44} {n:>6}")

    if args.export:
        # messages 里有大段文本，导出时会很大；这里导出结构化摘要 + messages
        out = []
        for r in results:
            out.append({
                "task_id": r["task_id"],
                "task_kind": r["task_kind"],
                "task_title": r["task_title"],
                "doc": r["doc"],
                "tokens": r["tokens"],
                "budget": r["budget"],
                "messages": r["messages"],
            })
        Path(args.export).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  已导出：{args.export}（{len(out)} 个任务）")


if __name__ == "__main__":
    main()
