#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_run_note.py —— 就地校正某次运行的备注（不重跑测试）

为什么需要它：
    备注（note）只是配置里的一个【标签】，不影响任何测量结果。
    但它会被前端和报告引用，写错了会误导人。
    重跑一次要 5 分钟且会多出一份数据，不值得 —— 所以做这个工具。

安全性：
    · 只改 run-config.json 和 runs.json 里的 note 字段
    · 不碰 summary / requests / meta 等任何测量数据
    · 改之前先备份成 *.bak

用法：
    python3 tools/fix_run_note.py <run_id> "新的备注"
    python3 tools/fix_run_note.py --list          # 看所有运行的备注
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "results"


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(p, obj):
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                       encoding="utf-8")


def cmd_list():
    reg = load(ROOT / "runs.json", default=[]) or []
    notes = {r.get("run_id"): r.get("note") for r in reg}
    dirs = sorted(d for d in ROOT.iterdir() if d.is_dir()) if ROOT.is_dir() else []
    if not dirs:
        print("还没有任何运行")
        return 0
    for d in dirs:
        cfg = load(d / "run-config.json", default={}) or {}
        print("%s" % d.name)
        print("   run-config.note : %s" % (cfg.get("note") or "(空)"))
        print("   runs.json 的 note: %s" % (notes.get(d.name) or "(无记录)"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="就地校正运行的备注")
    ap.add_argument("run_id", nargs="?", help="运行编号（目录名）")
    ap.add_argument("note", nargs="?", help="新的备注")
    ap.add_argument("--list", action="store_true", help="列出所有运行及其备注")
    args = ap.parse_args()

    if args.list or not args.run_id:
        return cmd_list()

    d = ROOT / args.run_id
    if not d.is_dir():
        print("❌ 找不到运行目录：%s" % args.run_id)
        print("   现有：", [x.name for x in ROOT.iterdir() if x.is_dir()])
        return 1
    if args.note is None:
        print("❌ 请给出新的备注文本")
        return 1

    # ① run-config.json
    cfgp = d / "run-config.json"
    cfg = load(cfgp)
    if cfg is None:
        print("❌ run-config.json 读不出来，放弃（不乱写）")
        return 1
    shutil.copy2(cfgp, str(cfgp) + ".bak")
    old = cfg.get("note")
    cfg["note"] = args.note
    save(cfgp, cfg)
    print("✅ run-config.json")
    print("   旧: %s" % old)
    print("   新: %s" % args.note)

    # ② runs.json 登记表
    regp = ROOT / "runs.json"
    reg = load(regp, default=[])
    hit = False
    for r in reg:
        if r.get("run_id") == args.run_id:
            r["note"] = args.note
            hit = True
    if hit:
        shutil.copy2(regp, str(regp) + ".bak")
        save(regp, reg)
        print("✅ runs.json 登记表已同步")
    else:
        print("⚠️ 登记表里没有这条运行，未改动登记表")

    print()
    print("提示：备份留在同目录的 *.bak，确认无误后可删。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
