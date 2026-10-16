#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把办公类任务（D01–D04）的模型输出导出来，供人工评判。"""
import json
import sys
from pathlib import Path

RD = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("results/kv_baseline_0.6-20261016-223609")
WANT = sys.argv[2].split(",") if len(sys.argv) > 2 else ["D01", "D02", "D03", "D04"]

cfg = json.loads((RD / "run-config.json").read_text(encoding="utf-8"))
rows = [json.loads(l) for l in
        (RD / "requests.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()]
idx = json.loads((RD / "prompt-index.json").read_text(encoding="utf-8"))

print("=" * 96)
print(" 运行 %s" % RD.name)
print("   ctx=%s  KV=%s/%s  max_tokens=%s  模型=%s" % (
    cfg.get("server_ctx"), cfg.get("kv_type_k"), cfg.get("kv_type_v"),
    cfg.get("max_tokens"), cfg.get("model_file")))
print("=" * 96)

for tid in WANT:
    recs = [r for r in rows if r.get("task_id") == tid]
    if not recs:
        continue
    r = max(recs, key=lambda x: x.get("output_chars") or 0)
    print()
    print("─" * 96)
    print("  %s   status=%s  out_tokens=%s  chars=%s  finish=%s" % (
        tid, r.get("status"), r.get("output_tokens"),
        r.get("output_chars"), r.get("finish_reason")))
    print("─" * 96)
    print("【模型输出原文】")
    print(r.get("output_text") or "（空）")
    print()
