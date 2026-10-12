#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inventory_writes.py —— 列出源码里【所有】落盘操作

为什么要这个：
    改测试脚本时最容易踩的坑不是"读不到文件"（那会报错），
    而是"写到别的地方去了"（静默成功，事后找不着数据）。
    所以先把每一处写操作列成清单，改的时候逐个对照。

判据（扫这几类）：
    *.write_text(...)          Path 的文本写
    *.write_bytes(...)         Path 的字节写
    open(..., "w"/"a"/"x")     内建 open 写
    mkdir(...)                 建目录
    csv.writer(f)              写 CSV
    unlink() / rmtree()        删除（也列出来，因为破坏性）
    shutil.copy / move         拷贝移动

用法：python3 tools/inventory_writes.py
"""

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]

PATTERNS = [
    (re.compile(r"\.write_text\s*\("),        "Path.write_text"),
    (re.compile(r"\.write_bytes\s*\("),       "Path.write_bytes"),
    (re.compile(r"\.open\s*\(\s*[\"']([wax])"), "open(写模式)"),
    (re.compile(r"\bopen\s*\([^)]*[\"']([wax])"),  "内建 open(写模式)"),
    (re.compile(r"\.mkdir\s*\("),             "mkdir"),
    (re.compile(r"csv\.writer\s*\("),         "csv.writer"),
    (re.compile(r"\.unlink\s*\("),            "unlink 删除"),
    (re.compile(r"rmtree\s*\("),              "rmtree 删除"),
    (re.compile(r"shutil\.(copy|move|copytree)"), "shutil 拷贝/移动"),
    (re.compile(r">>?\s*[\"']"),              "shell 重定向(字符串里)"),
]

hits = {}
for py in sorted(SRC.rglob("*.py")):
    if "__pycache__" in str(py):
        continue
    rel = py.relative_to(SRC)
    for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
        for pat, kind in PATTERNS:
            if pat.search(line):
                hits.setdefault(str(rel), []).append((i, kind, line.strip()))
                break

print("=" * 96)
print(" 源码里所有落盘 / 破坏性操作")
print("=" * 96)
print(f" 扫描目录：{SRC}")
print(f" 命中文件：{len(hits)} 个")
print()

total = 0
for rel in sorted(hits):
    print(f"── {rel}  ({len(hits[rel])} 处)")
    for i, kind, line in hits[rel]:
        total += 1
        print(f"   {i:5d}  [{kind}]")
        print(f"          {line[:110]}")
    print()

print("=" * 96)
print(f" 合计 {total} 处写/删除操作")
print("=" * 96)
