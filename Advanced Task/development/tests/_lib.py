#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_lib.py —— 极简测试工具（零依赖）

原则（和基础项目一致）：
  1. 每个测试要说明【断言什么】
  2. 期望值必须有【独立来源】（手算、协议规定、外部事实）
  3. 不拿系统自己的输出验证系统自己
"""

import sys
import traceback
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV / "proxy"))

_ok = 0
_fail = []
_name = ""


def case(name):
    global _name
    _name = name
    print(f"\n── {name} ──")


def check(cond, msg, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ✅ {msg}")
    else:
        _fail.append((_name, msg, detail))
        print(f"  ❌ {msg}" + (f"\n       {detail}" if detail else ""))
    return bool(cond)


def eq(got, want, msg):
    return check(got == want, msg, f"got={got!r}  want={want!r}")


def close(got, want, tol, msg):
    try:
        ok = abs(float(got) - float(want)) <= tol
    except Exception:                                         # noqa: BLE001
        ok = False
    return check(ok, msg, f"got={got!r}  want={want!r}±{tol}")


def note(msg):
    print(f"  ·  {msg}")


def skip(msg):
    print(f"  ⏭  {msg}（跳过）")


def run(fn):
    try:
        fn()
    except Exception as e:                                    # noqa: BLE001
        _fail.append((_name, f"测试抛异常: {e}", traceback.format_exc()[:900]))
        print(f"  ❌ 抛异常: {type(e).__name__}: {e}")
        traceback.print_exc()


def summary():
    print()
    print("=" * 64)
    if _fail:
        print(f"❌ 失败 {len(_fail)} 项 / 通过 {_ok} 项")
        for name, msg, detail in _fail:
            print(f"\n  [{name}] {msg}")
            if detail:
                for line in detail.splitlines()[:8]:
                    print("      " + line)
        return 1
    print(f"✅ 全部通过（{_ok} 项断言）")
    return 0
