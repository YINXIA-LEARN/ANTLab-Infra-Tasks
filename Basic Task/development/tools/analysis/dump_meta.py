#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 meta.json 里的硬件/服务信息，供报告第 2 章用。"""
import json
from pathlib import Path

D = Path(__file__).resolve().parents[2]
d = D / "results" / "ctx16k-f16-06b-r1-20261016-222516"
m = json.loads((d / "meta.json").read_text(encoding="utf-8"))

print("=== meta.json 顶层键 ===")
print(list(m.keys()))
print()
for k, v in m.items():
    if isinstance(v, (dict, list)):
        print("### %s" % k)
        print(json.dumps(v, ensure_ascii=False, indent=2)[:1600])
    else:
        print("### %s = %s" % (k, v))
    print()
