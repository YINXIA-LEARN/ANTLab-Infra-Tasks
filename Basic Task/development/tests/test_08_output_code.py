#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_08_output_code.py —— 编程输出的运行验证

═══════════════════════════════════════════════════════════════════════════
 对应题目（原文）
═══════════════════════════════════════════════════════════════════════════
 任务介绍·规定输入与输出检查：
     「编程任务检查问题定位、修复正确性、★可执行性和边界覆盖。
       对失败输出给出具体样例，说明其对应用景的影响。」

 tests/README.md：
     「办公输出检查与编程★输出运行验证可以分别组织。
       记录所检查的模型输出文件及提示词编号，使结果与正式测试记录对应。」

 报告模板 §8：
     「办公任务核对事实与条款，编程任务检查★最小复现、代码执行和边界条件。」

═══════════════════════════════════════════════════════════════════════════
 什么叫"运行验证"
═══════════════════════════════════════════════════════════════════════════
 ★ 不是"读一遍代码觉得写得对"，而是【真的把它跑起来】：

     ① 从记录里取出模型生成的代码原文
     ② 剥掉 markdown 围栏，存成 .py
     ③ py_compile 看语法
     ④ 真的 import 进来
     ⑤ 喂【边界输入】看行为
     ⑥ ★ 检查是不是被 max_tokens 截断了

═══════════════════════════════════════════════════════════════════════════
 ★ 第 ⑥ 条是本测试的重点，也是最容易误判的地方
═══════════════════════════════════════════════════════════════════════════
 实测发现：P03 的输出 output_tokens = 2048，恰好等于 max_tokens 上限，
 代码在 line 173 断在字符串中间 → SyntaxError。

 如果只看"语法错"，会得出「模型写得差」的结论 —— ★ 那是错的。
 真实原因是【输出被截断】。这两种结论对应用场景的影响完全不同：

     模型写得差       → 要换更强的模型
     输出被截断       → 调大 max_tokens 就行

 所以本测试【先查截断，再查语法】，并在报告里区分。

═══════════════════════════════════════════════════════════════════════════
 判定说明
═══════════════════════════════════════════════════════════════════════════
 ⚠️ 本测试对"模型输出质量"不做通过/失败判定 ——
    小模型答不好是常态，判它"失败"没有意义。

 它判的是【我们自己的检查流程是否正确】：
     · 能不能把代码从输出里提取出来
     · 能不能发现截断
     · 能不能真跑起来
 然后把每个任务的质量结论列出来，供报告 §8 引用。

 退出码：0 流程正常完成 / 1 检查流程本身出错 / 2 脚本自身出错
"""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _testlib import Report, load_run, list_runs, run, successful   # noqa: E402

# 需要运行验证的编程类任务（题目规定的 10 个里，编程相关的）
CODE_TASKS = {
    "P03": "实现 aggregate_by_owner(records) + 至少 5 个测试示例",
    "P04": "设计任务处理方案（去重/重试上限/持久化/重启恢复）+ 核心代码",
    "C01": "代码审查：按问题逐项说明触发条件、当前行为、预期行为、最小复现",
    "C02": "修复统计逻辑与边界处理 + 补充 7 类边界测试",
}


def strip_fence(text):
    """剥掉 markdown 代码围栏，取出纯代码"""
    lines = (text or "").splitlines()
    # 去掉开头的 ``` 或 ```python
    while lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    # 去掉结尾的 ```
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def try_compile(code, tmpdir, tag):
    """存文件 + py_compile。返回 (成功?, 文件路径, 错误信息)"""
    f = Path(tmpdir) / f"{tag}.py"
    f.write_text(code, encoding="utf-8")
    p = subprocess.run([sys.executable, "-m", "py_compile", str(f)],
                       capture_output=True, text=True, timeout=60)
    err = ""
    if p.returncode != 0:
        for ln in p.stderr.strip().splitlines():
            if "Error" in ln or "error" in ln or "line" in ln:
                err = ln.strip()
    return p.returncode == 0, f, err


def run_boundary_cases(fn, cases):
    """
    真跑边界输入。cases = [(名字, 输入, 期望)]
    返回 [(名字, 结果描述, 通过?)]
    """
    out = []
    for name, inp, want in cases:
        try:
            got = fn(json.loads(json.dumps(inp)))
            ok = _match(got, want)
            out.append((name, json.dumps(got, ensure_ascii=False)[:70], ok))
        except Exception as e:                            # noqa: BLE001
            out.append((name, f"{type(e).__name__}: {str(e)[:50]}", False))
    return out


def _match(got, want):
    """宽松匹配：允许结构相同但键顺序/嵌套差异"""
    if got == want:
        return True
    # 允许任务给出的实现把 total 写成 count 之类的同义键
    if isinstance(got, dict) and isinstance(want, dict):
        return set(got.keys()) == set(want.keys())
    return False


def main():
    runs = list_runs()
    rep = Report("test_08_output_code —— 编程输出的运行验证")

    if not runs:
        rep.check_true("results/ 下有可测运行", False, "没有数据")
        return rep.finish()

    for rid in runs:
        _check_one(rid, rep)
    return rep.finish()


def _check_one(rid, rep):
    d = load_run(rid)
    rows = successful(d["requests"])
    cfg = d["run_config"] or {}
    max_tok = cfg.get("max_tokens")

    print("═" * 72)
    print(f" 运行 {rid}")
    print(f"  max_tokens 上限 = {max_tok}")
    print("═" * 72)

    for tid, desc in CODE_TASKS.items():
        recs = [r for r in rows if r.get("task_id") == tid]
        if not recs:
            rep.note(f"{rid}/{tid}: 没有记录，跳过")
            continue
        r = max(recs, key=lambda x: x.get("output_chars") or 0)
        code = r.get("output_text") or ""
        n_out = r.get("output_tokens") or 0

        print()
        print(f"── {tid} ── {desc}")
        print(f"   output_tokens={n_out}  output_chars={len(code)}  "
              f"finish_reason={r.get('finish_reason')}")

        # ══ ⑥ 先查截断（★ 最重要，因为它会误导结论）══════════════════
        truncated = False
        if max_tok and n_out >= max_tok:
            truncated = True
            rep.note(f"{rid}/{tid}: ★输出被截断 —— output_tokens({n_out}) "
                     f"达到 max_tokens 上限({max_tok})，"
                     f"结论应记为「输出上限不足」而非「模型能力差」")
            print(f"  ⚠️ 达到 max_tokens 上限 → 【被截断】")
        elif r.get("finish_reason") == "length":
            truncated = True
            rep.note(f"{rid}/{tid}: finish_reason=length，输出被截断")
            print(f"  ⚠️ finish_reason=length → 【被截断】")
        else:
            print(f"  ✅ 未被截断（finish_reason={r.get('finish_reason')}）")

        # ══ ② 提取代码 ═══════════════════════════════════════════════
        has_fence = "```" in code
        clean = strip_fence(code)
        rep.check_true(f"{rid}/{tid}: 能从输出里提取出代码块",
                       len(clean) > 0, "输出为空")
        print(f"  提取：{'有 markdown 围栏' if has_fence else '无围栏'} → "
              f"{len(clean)} 字符")

        with tempfile.TemporaryDirectory() as td:
            # ══ ③ 语法 ═══════════════════════════════════════════════
            ok, f, err = try_compile(clean, td, f"{rid}_{tid}")
            if ok:
                print(f"  ✅ 语法通过（py_compile）")
            else:
                print(f"  ❌ 语法失败：{err}")
                if truncated:
                    print(f"     ⚠️ 但根因是截断，不是语法能力")
                rep.note(f"{rid}/{tid}: 语法{'通过' if ok else '失败'}"
                         f"{'（因截断）' if truncated and not ok else ''}")

            # ══ ④⑤ 真跑（只有编译过才有意义）════════════════════════
            if ok:
                _run_it(rid, tid, f, clean, rep, truncated)

    print()


def _run_it(rid, tid, f, clean, rep, truncated):
    """把代码真跑起来，分任务类型喂不同的输入"""
    # 找出里面定义了哪些函数
    fns = re.findall(r"^def ([a-zA-Z_][\w]*)\(", clean, re.M)
    print(f"  定义的函数：{fns if fns else '（没有顶层 def）'}")

    if tid == "P03":
        target = "aggregate_by_owner"
        if target not in fns:
            print(f"  ❌ 没找到要求的函数 {target}()")
            rep.note(f"{rid}/{tid}: 未实现要求的函数 {target}")
            return
        # ★ 边界用例（题目原文点名的那几类）
        cases = [
            ("空输入",       [],                                 {}),
            ("单条 closed",  [{"owner": "a", "status": "closed"}], None),
            ("大小写混合",   [{"owner": "a", "status": "CLOSED"}], None),
            ("owner 缺失",   [{"status": "open"}],                 None),
            ("owner 空串",   [{"owner": "", "status": "open"}],    None),
            ("未知状态",     [{"owner": "a", "status": "zzz"}],    None),
        ]
        try:
            spec = importlib.util.spec_from_file_location("gen_p03", str(f))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            fn = getattr(m, target)
        except Exception as e:                            # noqa: BLE001
            print(f"  💥 import 失败：{type(e).__name__}: {str(e)[:70]}")
            rep.note(f"{rid}/{tid}: 代码无法 import —— "
                     f"{'（因截断）' if truncated else '（实现有问题）'}")
            return
        res = run_boundary_cases(fn, cases)
        for name, got, ok in res:
            print(f"    {'✅' if ok else '·'} {name:12s} → {got}")
        # 空输入这一条是题目硬要求，必须过
        empty_ok = next((ok for n, _, ok in res if n == "空输入"), False)
        rep.check_true(f"{rid}/{tid}: 空输入返回空字典（题目硬要求）",
                       empty_ok, "这是题目明确要求的行为")

    elif tid in ("P04", "C01", "C02"):
        # 这三个任务输出里【不一定】有可直接执行的完整程序：
        #   P04 是设计方案（可能只有片段）
        #   C01 是审查报告（说明问题，不是代码）
        #   C02 要求完整修改后的程序（应该可执行）
        if tid == "C02":
            rep.check_true(f"{rid}/{tid}: 输出包含完整程序（可编译）", True,
                           "（编译结果见上）")
            # 题目要求补 7 类边界测试
            wants = ["空输入", "缺失字段", "未知状态", "大小写",
                     "未关闭", "重复", "无效时间"]
            src = clean
            hit = [w for w in wants if w in src]
            print(f"  题目要求的 7 类边界测试，代码/注释里提到 {len(hit)}/7：{hit}")
            rep.note(f"{rid}/{tid}: 边界覆盖 {len(hit)}/7 类（{hit}）")
        else:
            print(f"  （{tid} 是{'设计方案' if tid == 'P04' else '审查报告'}，"
                  f"不要求可执行 —— 只做语法检查）")


if __name__ == "__main__":
    run(main)
