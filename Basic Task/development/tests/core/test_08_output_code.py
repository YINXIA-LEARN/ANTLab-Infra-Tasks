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

# ⚠️ 本文件在 tests/core/ 下，_testlib.py 在上一层 tests/。
#    两层都要加：本层用于同目录的模块，上一层用于 _testlib。
#    （_testlib 必须留在 tests/ —— 它的 DEV_DIR/RESULTS_DIR
#      是按自己位置算的，挪下来会整体错一层）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _testlib import (                                          # noqa: E402
    DEV_DIR, Report, load_run, list_runs, run, successful)

# ══════════════════════════════════════════════════════════════════════════
#  任务类型来自【权威定义】，不在这里硬编码
#
#  ⚠️ 这个集合原来是手写的 {P03, P04, C01, C02}，而 authoritative 的定义是
#     src/common/tasks.py 的 kind 字段：
#         P01–P04  kind="prompt"   纯提示词（P03/P04 类型上属于提示词，
#                                  但题目要求它们产出代码）
#         D01–D04  kind="doc"      办公文档
#         C01–C02  kind="code"     编程任务
#     手写集合的问题是：任务定义改了这里不会跟着改，而且看不出
#     "P03 是 prompt 却要求给代码"这个区别。
# ══════════════════════════════════════════════════════════════════════════
FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)```", re.S)

# 题目的"输出检查"一节点名要看代码的任务（P03/P04 属 prompt 但要求给码）
PROMPT_CODE_LIKE = {"P03", "P04"}


def task_kind_map():
    """从 src/common/tasks.py 读每个任务的 kind（权威定义）"""
    f = DEV_DIR / "src" / "common" / "tasks.py"
    if not f.is_file():
        return {}
    text = f.read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(
            r'\{\s*"id"\s*:\s*"([A-Z]\d+)".*?"kind"\s*:\s*"([a-z]+)"',
            text, re.S):
        out[m.group(1)] = m.group(2)
    return out


def extract_fenced(text):
    """
    只提取 markdown 围栏里的代码块。返回 [(语言, 代码)]。

    ⚠️⚠️ 这里修掉一个【判定缺陷】——原来的 strip_fence 是"剥掉首尾的围栏，
        把中间全部当代码"，于是模型输出一篇说明文字时，
        那篇文字会被送去 py_compile，必然报语法错。

        实测后果：C01（题目要的是"代码审查报告"，输出本来就是说明文字）
        在 9 次运行里有 6 次被判"语法失败"——**全是假阳性**。
        P04（设计方案）同理。

        修法：只认围栏。没有围栏就如实说"没提取到代码"，
        而不是报"语法错"——这两件事的性质完全不同：
            · 语法错   → 模型写出来的代码有问题
            · 没给代码 → 模型没按要求给代码（对 code 任务是发现）
    """
    return [(m.group(1) or "?", m.group(2)) for m in FENCE_RE.finditer(text or "")]


def pick_code_block(blocks):
    """从多个代码块里挑最可能是"那个完整实现"的一块"""
    if not blocks:
        return None
    py = [b for b in blocks if b[0].lower() in ("python", "py", "python3")]
    pool = py or blocks
    return max(pool, key=lambda b: len(b[1]))



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

    kinds = task_kind_map()
    code_tasks = {t for t, k in kinds.items() if k == "code"}
    rep.check_true("能从 src/common/tasks.py 读到任务类型（权威定义）",
                   bool(kinds),
                   "读不到就只能靠硬编码，任务定义改了这里不会跟着改")
    want = code_tasks | PROMPT_CODE_LIKE
    print(f"任务类型（来自 src/common/tasks.py）：")
    print(f"  kind=code   : {sorted(code_tasks)}")
    print(f"  kind=prompt : "
          f"{sorted(t for t, k in kinds.items() if k == 'prompt')}")
    print(f"  kind=doc    : "
          f"{sorted(t for t, k in kinds.items() if k == 'doc')}")
    print(f"本次检查: {sorted(want)}"
          f"（P03/P04 属 prompt，但题目要求它们给代码）")

    # ⚠️ 跳过"服务中途重启过"的运行 —— 它们的数据是两段混的。
    #    实测踩过：ctx16k-q80-06b-r2 那轮服务被杀过，
    #    它的 P03 输出被判"空输入不返回空字典"。
    #    那【不是】模型的缺陷，是中途重启把上下文/输出截断造成的。
    #    拿它做断言会得出错误结论。（这类运行由 test_10 与
    #    结果页的 interrupted 状态负责展示，不在这里判质量。）
    skipped = []
    for rid in runs:
        d = load_run(rid)
        if (d["dir"] / "service-starts.jsonl").is_file():
            skipped.append(rid)
    for rid in skipped:
        rep.note(f"{rid}: 服务中途重启过 → 跳过（数据是两段混的，"
                 f"判质量会得出错误结论）")
        print(f"⏭  {rid}: 服务中途重启过，跳过")
    runs = [r for r in runs if r not in skipped]

    for rid in runs:
        _check_one(rid, rep, kinds, want)
    return rep.finish()


def _check_one(rid, rep, kinds, want):
    d = load_run(rid)
    rows = successful(d["requests"])
    cfg = d["run_config"] or {}
    max_tok = cfg.get("max_tokens")

    print("═" * 72)
    print(f" 运行 {rid}")
    print(f"  max_tokens 上限 = {max_tok}")
    print("═" * 72)

    for tid in sorted(want):
        kind = kinds.get(tid, "?")
        recs = [r for r in rows if r.get("task_id") == tid]
        if not recs:
            rep.note(f"{rid}/{tid}: 没有记录，跳过")
            continue
        r = max(recs, key=lambda x: x.get("output_chars") or 0)
        text = r.get("output_text") or ""
        n_out = r.get("output_tokens") or 0

        print()
        print(f"── {tid} ── kind={kind}")
        print(f"   output_tokens={n_out}  output_chars={len(text)}  "
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

        # ══ ② 提取代码（只认围栏）═════════════════════════════════════
        blocks = extract_fenced(text)
        best = pick_code_block(blocks)
        print(f"  提取：围栏代码块 {len(blocks)} 个"
              + (f"，语言 {sorted({b[0] for b in blocks})}" if blocks else "")
              + (f"，选中最长一块 {len(best[1])} 字符" if best else ""))

        if best is None:
            # ⚠️ 分两种说法，不要混：
            #    kind=code 的任务没给代码 → 是【发现】（未按要求产出）
            #    kind=prompt 的任务没给代码 → 只记录，不算问题
            if kind == "code":
                rep.note(f"{rid}/{tid}: ★该任务是 code 类型，但输出里"
                         f"【没有代码围栏】—— 只给了说明文字。"
                         f"这不是语法错，是未按要求产出代码")
                print(f"  📄 没有代码围栏（纯文字输出）—— 对 code 任务是发现")
            else:
                rep.note(f"{rid}/{tid}: 没有代码围栏（kind={kind}，属正常）")
                print(f"  📄 没有代码围栏（kind={kind}，不要求给代码）")
            continue

        clean = best[1]
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

        # ── 题目硬要求的三条，逐条判 ──────────────────────────────
        #
        # ⚠️ 断言口径要精确。原来写的是"空输入返回空字典"，
        #    但字面 {} 与"键齐全、值全 0"在语义上都能算合理实现 ——
        #    用字面相等去判会把后者冤枉成失败。
        #    实测：0.6B 返回 {'total':0,'open':0,...}，确实【是】空结果，
        #    所以这里查"有没有任何计数"而不是"是不是字面空字典"。
        #
        # ⚠️ 下面这两条（空输入、大小写）是【流水线正确性】相关的口径检查，
        #    判失败；而"缺失 owner 归 unassigned"查的是模型输出质量，
        #    只记录不判失败 —— 取舍理由写在那一处。
        try:
            out_empty = fn([])
            if isinstance(out_empty, dict):
                empty_ok = all((v or 0) == 0
                               for v in out_empty.values() if isinstance(v, int))
            else:
                empty_ok = not out_empty
        except Exception:                                     # noqa: BLE001
            empty_ok = False
            out_empty = "抛异常"
        rep.check_true(f"{rid}/{tid}: 空输入返回空结果（题目硬要求）",
                       empty_ok, f"实际 {out_empty!r}")

        # 题目还明确要求"状态匹配忽略大小写" —— 这条能独立断言
        try:
            up = fn([{"owner": "a", "status": "CLOSED"}])
            low = fn([{"owner": "a", "status": "closed"}])
            rep.check_true(
                f"{rid}/{tid}: 状态匹配忽略大小写（题目硬要求）",
                json.dumps(up, sort_keys=True) == json.dumps(low, sort_keys=True),
                f"CLOSED → {up!r}；closed → {low!r}")
        except Exception as e:                                # noqa: BLE001
            rep.check_true(f"{rid}/{tid}: 状态匹配忽略大小写（题目硬要求）",
                           False, f"抛异常 {type(e).__name__}")

        # 以及"缺失或为空的 owner 归为 unassigned"
        #
        # ⚠️ 这里第一版写法有 bug，记下来：我用
        #       "unassigned" in json.dumps(miss)
        #    去判 —— 而 unassigned 是【外层字典的键】，不是值里的字符串。
        #    实测模型返回 {'unassigned': {...}} 时，
        #    dumps 出来的文本里确实有 "unassigned"，能蒙对；
        #    但若实现写成 {'total':..., 'open':...}（按状态统计、没有按人分组），
        #    也照样匹配不到 —— 这个判据恰好不出错，但它判的不是我想判的东西。
        #    正确判法：直接看返回字典的【键】里有没有 unassigned。
        def _has_unassigned(out):
            if not isinstance(out, dict) or not out:
                return False
            keys = {str(k).lower() for k in out.keys()}
            if "unassigned" in keys:
                return True
            # 也可能嵌一层：{'unassigned': {...}}
            for v in out.values():
                if isinstance(v, dict) and \
                        "unassigned" in {str(k).lower() for k in v.keys()}:
                    return True
            return False

        try:
            miss = fn([{"status": "open"}])
            blank = fn([{"owner": "", "status": "open"}])
            unassigned_ok = _has_unassigned(miss) and _has_unassigned(blank)
            # ⚠️ 这条【记录但不判失败】——这是个刻意的取舍，理由：
            #
            #    它查的是【模型输出的质量】，不是【我们流水线的正确性】。
            #    实测 8 个干净运行里有 1 个（ctx16k-q80-06b-r1）的实现
            #    把 owner 信息整个丢了（只按 status 统计），这确实是
            #    没满足题目的硬要求 —— 但那是【模型的缺陷】，
            #    不是我们系统的 bug。
            #
            #    测试套件的作用是"流水线自检"：它红了应该意味着
            #    "我们的代码/路径/统计有问题，去修"。把模型输出质量
            #    混进来，会让套件长期红着，真出 bug 时反而看不出来。
            #
            #    所以：发现的缺陷用 rep.note 记录（报告里引用），
            #    但不让退出码变红。质量结论写进报告。
            if unassigned_ok:
                rep.check_true(
                    f"{rid}/{tid}: 缺失/空 owner 归为 unassigned（题目硬要求）",
                    True, "")
            else:
                rep.note(f"{rid}/{tid}: ★模型输出缺陷（不算系统失败）—— "
                         f"缺失/空 owner 未归为 unassigned，"
                         f"实际返回 {miss!r}。题目硬要求，记入报告。")
                print(f"    ⚠️ 模型输出缺陷：缺失/空 owner 未归 unassigned")
        except Exception as e:                                # noqa: BLE001
            rep.note(f"{rid}/{tid}: 缺失 owner 时抛异常 "
                     f"{type(e).__name__}: {str(e)[:60]}（记入报告）")

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
