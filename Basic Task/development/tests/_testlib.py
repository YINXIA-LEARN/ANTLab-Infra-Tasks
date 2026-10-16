#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_testlib.py —— tests/ 下所有测试共用的骨架

═══════════════════════════════════════════════════════════════════════════
 为什么要有这个文件
═══════════════════════════════════════════════════════════════════════════

tests/ 里每个测试都必须做到三件事：
    ① 断言什么        —— 比如 ttft_ms == (t_first - t_submit) * 1000
    ② 独立来源是什么   —— 必须是不经过被测代码的另一条路径
    ③ 判定阈值        —— 精确相等？还是容忍浮点误差？

没有 ② 就不是测试，是"自己证明自己"。这个文件把 ①③ 的公共部分抽出来，
让每个测试只关心 ②（也就是真正有价值的那部分）。

═══════════════════════════════════════════════════════════════════════════
 目录约定
═══════════════════════════════════════════════════════════════════════════

    tests/
    ├─ _testlib.py            本文件（共用骨架，不被自动发现）
    ├─ test_00_paths.py       ...
    └─ README.md              运行方式汇总

本文件以 _ 开头，是为了让"按 test_*.py 收集测试"的做法不会把它当成测试。
"""

import json
import math
import statistics
import sys
from pathlib import Path

# ── 路径 ─────────────────────────────────────────────────────────────────
#   tests/ 在 <development>/tests/，所以：
#       parents[0] = tests   [1] = development
TESTS_DIR = Path(__file__).resolve().parent
DEV_DIR = TESTS_DIR.parent
RESULTS_DIR = DEV_DIR / "results"


# ══════════════════════════════════════════════════════════════════════════
#  测试报告器
# ══════════════════════════════════════════════════════════════════════════

class Report:
    """
    收集断言结果，最后统一给退出码。

    ⚠️ 为什么不直接 assert：
        assert 会在【第一个失败】处中断，看不到其余问题。
        而测试的价值恰恰在于"一次跑完，把所有问题列出来"。

    退出码：0 = 全部通过，1 = 有失败
    """

    def __init__(self, title):
        self.title = title
        self.n_ok = 0
        self.fails = []          # [(断言描述, 实际, 期望, 附注)]
        self.notes = []          # 观察到的、不算失败但值得写下来的东西

    # ── 断言 ─────────────────────────────────────────────────────────

    def check(self, desc, got, want, note=""):
        """精确相等"""
        if got == want:
            self.n_ok += 1
            return True
        self.fails.append((desc, got, want, note))
        return False

    def check_close(self, desc, got, want, tol=1e-6, note=""):
        """浮点近似相等。tol 是【绝对】容差。"""
        try:
            if abs(float(got) - float(want)) <= tol:
                self.n_ok += 1
                return True
        except (TypeError, ValueError):
            pass
        self.fails.append((desc, got, want,
                           (note + " " if note else "") + f"容差 {tol:g}"))
        return False

    def check_true(self, desc, cond, note=""):
        return self.check(desc, bool(cond), True, note)

    def check_le(self, desc, a, b, note=""):
        if a is not None and b is not None and a <= b:
            self.n_ok += 1
            return True
        self.fails.append((desc, a, f"<= {b}", note))
        return False

    def note(self, msg):
        """记一条观察（不算失败，但会打印并可能进报告）"""
        self.notes.append(msg)

    # ── 输出 ─────────────────────────────────────────────────────────

    def finish(self):
        line = "─" * 72
        print()
        print(line)
        print(f" {self.title}")
        print(line)
        print(f"  通过：{self.n_ok} 项")
        if self.notes:
            print(f"  观察：{len(self.notes)} 条")
            for n in self.notes:
                print(f"    · {n}")
        if self.fails:
            print(f"  ❌ 失败：{len(self.fails)} 项")
            for desc, got, want, note in self.fails:
                print(f"    · {desc}")
                print(f"        实际 {got!r}")
                print(f"        期望 {want!r}")
                if note:
                    print(f"        附注 {note}")
            print()
            print("  结果：❌ 未通过")
            print(line)
            return 1
        print()
        print("  结果：✅ 通过")
        print(line)
        return 0


def run(main_fn):
    """测试脚本的统一入口：捕获异常也算失败，并给出明确退出码"""
    try:
        code = main_fn()
    except KeyboardInterrupt:
        print("\n（手动中断）")
        sys.exit(130)
    except Exception as e:                                     # noqa: BLE001
        import traceback
        print()
        print("─" * 72)
        print(" ❌ 测试脚本自身出错")
        print("─" * 72)
        traceback.print_exc()
        sys.exit(2)
    sys.exit(code or 0)


# ══════════════════════════════════════════════════════════════════════════
#  读结果数据
# ══════════════════════════════════════════════════════════════════════════

def list_runs():
    """
    列出 results/ 下【可用】的运行目录名（有 requests.jsonl 的）。

    ⚠️ 判据用 requests.jsonl 而不是"目录存在"：
       中断的运行也会留下目录，但它没有逐请求记录，测不了。
    """
    if not RESULTS_DIR.is_dir():
        return []
    out = []
    for d in sorted(RESULTS_DIR.iterdir()):
        if d.is_dir() and (d / "requests.jsonl").is_file():
            out.append(d.name)
    return out


def load_run(run_id):
    """读一次运行的全部文件，返回 dict。缺哪个文件就是 None。"""
    d = RESULTS_DIR / run_id
    out = {"run_id": run_id, "dir": d}

    def _j(name):
        f = d / name
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    out["meta"] = _j("meta.json")
    out["summary"] = _j("summary.json")
    out["run_config"] = _j("run-config.json")
    out["prompt_index"] = _j("prompt-index.json")
    out["errors"] = _j("errors.json")

    req = d / "requests.jsonl"
    rows = []
    if req.is_file():
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    out["requests"] = rows
    out["n_files"] = len([f for f in d.iterdir() if f.is_file()]) if d.is_dir() else 0
    return out


def successful(rows):
    return [r for r in rows if r.get("status") == "success"]


# ══════════════════════════════════════════════════════════════════════════
#  独立实现：指标计算
#
#  ⚠️ 这一节是整个 tests/ 的核心价值所在。
#
#     被测代码（src/bench/）自己会算中位数、P95、吞吐。
#     如果测试直接调它的函数来验证它自己，那叫循环论证 —— 永远通过。
#
#     所以这里【重新实现一遍】，用不同写法：
#       · 中位数：不调 statistics.median，自己排序取中间
#       · P95：不调被测的 pct()，自己按线性插值写
#     然后比对两者的结果。两条独立路径算出同一个数，才算数。
# ══════════════════════════════════════════════════════════════════════════

def median_of(values):
    """自己实现中位数（偶数个取中间两个的平均）"""
    v = sorted(values)
    n = len(v)
    if n == 0:
        return None
    if n % 2 == 1:
        return v[n // 2]
    return (v[n // 2 - 1] + v[n // 2]) / 2


def percentile_linear(values, p):
    """
    自己实现百分位（线性插值法），p 取 0~100。

    写法说明（故意和被测代码不同，避免"抄一遍"）：
        位置 = (n - 1) * p / 100，在相邻两点间线性插值。
    """
    v = sorted(values)
    n = len(v)
    if n == 0:
        return None
    if n == 1:
        return float(v[0])
    pos = (n - 1) * p / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(v[int(pos)])
    frac = pos - lo
    return v[lo] * (1 - frac) + v[hi] * frac


# ══════════════════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════════════════

def fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def banner(text, ch="═", width=72):
    print(ch * width)
    print(f" {text}")
    print(ch * width)
