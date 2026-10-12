#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署并检查结果页（语法、括号配对、id 齐全性）"""
import re
import subprocess
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parents[1]
WEB = DEST / "web"


def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)
    return (r.stdout or "") + (r.stderr or "")


print("=== 1. 部署前端 ===")
for f in ["app.js", "index.html", "style.css"]:
    print("  %-12s %6d 字节" % (f, (WEB / f).stat().st_size))

print()
print("=== 2. app.js 括号配对（去掉字符串与注释后统计）===")
src = (WEB / "app.js").read_text(encoding="utf-8")
s = src
s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)      # 块注释
s = re.sub(r"//[^\n]*", "", s)                   # 行注释
s = re.sub(r"`(?:[^`\\]|\\.)*`", "``", s, flags=re.S)   # 模板串
s = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", s)
s = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', s)
ok = True
for op, cl in [("{", "}"), ("(", ")"), ("[", "]")]:
    a, b = s.count(op), s.count(cl)
    good = a == b
    ok = ok and good
    print("  %s%s : %4d / %4d  %s" % (op, cl, a, b, "✅" if good else "❌ 不配对"))
print("  app.js 行数: %d" % (src.count("\n") + 1))
print("  style.css 行数: %d" % (WEB / "style.css").read_text(encoding="utf-8").count("\n"))
print("  index.html 行数: %d" % (WEB / "index.html").read_text(encoding="utf-8").count("\n"))

print()
print("=== 3. HTML 里结果页用到的 id 是否齐全 ===")
h = (WEB / "index.html").read_text(encoding="utf-8")
ids = set(re.findall(r'id="([^"]+)"', h))
want = ["btn-refresh-runs", "run-filter-label", "run-filter-conc",
        "run-filter-status", "runs-meta", "runs-list", "runs-compare-bar",
        "compare-picked", "btn-do-compare", "btn-clear-pick",
        "results-tabs", "results-body", "panel-results"]
missing = [w for w in want if w not in ids]
print("  缺失:", missing if missing else "无 ✅")

print()
print("=== 4. JS 里 $('xxx') 引用的 id 是否都存在 ===")
used = set(re.findall(r"\$\('([^']+)'\)", src))
miss2 = sorted(u for u in used if u not in ids)
print("  引用了但 HTML 没有:", miss2 if miss2 else "无 ✅")

sys.exit(0 if ok and not missing and not miss2 else 1)
