#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_code_tasks.py —— 对【编程输出】做可运行性验证，产出报告可引用的证据。

═══════════════════════════════════════════════════════════════════════════
 它和 tests/core/test_08_output_code.py 的关系
═══════════════════════════════════════════════════════════════════════════
 test_08 是"测试套件"的一员，只判"我们的检查流程能不能跑通"（通过/失败）。
 本脚本是"取证工具"：同样做提取+编译+真跑，但产出【逐运行逐任务的结构化
 证据】，供报告 §8 直接引用。

⚠️ 为什么另写一个而不是直接用 test_08：test_08 有个判定缺陷 ——
   它把模型输出的【整篇文本】当 Python 编译。于是：
     · C01 是"代码审查报告"（纯说明文字，没有代码围栏）→ 被送去编译 → 必然语法失败
     · P04 是"设计方案"（说明 + 片段）→ 同上
   报出来的"语法失败"里混着【假阳性】，据此写报告会得出错误结论。
   本脚本把"提取代码块"这一步做对：**只编译围栏里的代码**，
   没有围栏就如实说"没给代码"，而不是报"语法错"。

═══════════════════════════════════════════════════════════════════════════
 判定口径（三种，不要混）
═══════════════════════════════════════════════════════════════════════════
   ① 任务类型来自【权威定义】src/common/tasks.py 的 kind 字段，不靠猜：
        P01–P04  kind="prompt"   纯提示词
        D01–D04  kind="doc"      办公文档
        C01–C02  kind="code"     编程任务
   ② 输出形态分四类：
        code_ok        提取出代码且编译通过
        code_syntax    提取出代码但编译失败
        no_code        没提取到代码（纯文字）—— 对 code 任务是发现，不是"失败"
        truncated      输出被 max_tokens 截断（此时语法错要归因于截断）
   ③ "可运行性"只有在 code_ok 时才继续验（import + 跑边界用例）。
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

DEV = Path(__file__).resolve().parents[2]
RESULTS = DEV / "results"
SRC = DEV / "src"

FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)```", re.S)


# ── 任务类型：从权威定义读，不硬编码 ──────────────────────────────────────
def task_kinds():
    text = (SRC / "common" / "tasks.py").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(
            r'\{\s*"id"\s*:\s*"([A-Z]\d+)".*?"kind"\s*:\s*"([a-z]+)"',
            text, re.S):
        out[m.group(1)] = m.group(2)
    # 兜底：有些定义 id 与 kind 顺序相反
    if not out:
        for m in re.finditer(
                r'"kind"\s*:\s*"([a-z]+)".*?"id"\s*:\s*"([A-Z]\d+)"',
                text, re.S):
            out[m.group(2)] = m.group(1)
    return out


def extract_blocks(text):
    """
    提取 markdown 围栏里的代码块。
    返回 [(语言标记, 代码)]；没有围栏则返回 []。

    ⚠️ 只认围栏，不做"整篇当代码"的兜底 —— 那样会把说明文字算成语法错。
       模型没给围栏时，调用方应记成 no_code（对 code 任务是发现），
       而不是 code_syntax（那是冤枉它）。
    """
    return [(m.group(1) or "?", m.group(2)) for m in FENCE.finditer(text or "")]


def pick_best(blocks):
    """从多个代码块里挑最可能"那个完整程序"的一块"""
    if not blocks:
        return None
    py = [b for b in blocks if b[0].lower() in ("python", "py", "python3")]
    pool = py or blocks
    # 最长的那块 = 最可能是完整实现（不是零散片段）
    return max(pool, key=lambda b: len(b[1]))


def try_compile(code, tmpdir, tag):
    f = Path(tmpdir) / f"{tag}.py"
    f.write_text(code, encoding="utf-8")
    p = subprocess.run([sys.executable, "-m", "py_compile", str(f)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode == 0:
        return True, f, ""
    err = ""
    for ln in (p.stderr or "").strip().splitlines():
        if "Error" in ln:
            err = ln.strip()
            break
    return False, f, err


def try_import(f, name="gen_mod"):
    spec = importlib.util.spec_from_file_location(name, str(f))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run_cases(fn, cases):
    out = []
    for name, inp in cases:
        try:
            got = fn(json.loads(json.dumps(inp)))
            out.append((name, json.dumps(got, ensure_ascii=False)[:70], True))
        except Exception as e:                                # noqa: BLE001
            out.append((name, f"{type(e).__name__}: {str(e)[:50]}", False))
    return out


# ── P03 的边界用例（题目要求"至少 5 个测试示例"）──────────────────────────
P03_CASES = [
    ("空输入",     []),
    ("单条 closed", [{"owner": "a", "status": "closed"}]),
    ("大小写混合", [{"owner": "a", "status": "CLOSED"}]),
    ("owner 缺失", [{"status": "open"}]),
    ("owner 空串", [{"owner": "", "status": "open"}]),
    ("未知状态",   [{"owner": "a", "status": "zzz"}]),
]


def evaluate_run(d, kinds, want_tasks):
    cfg = json.loads((d / "run-config.json").read_text(encoding="utf-8"))
    rq = d / "requests.jsonl"
    rows = [json.loads(l) for l in
            rq.read_text(encoding="utf-8").splitlines() if l.strip()]
    max_tok = cfg.get("max_tokens")
    res = []
    for tid in want_tasks:
        recs = [r for r in rows if r.get("task_id") == tid
                and r.get("status") in ("success", "truncated")]
        if not recs:
            res.append({"task": tid, "kind": kinds.get(tid, "?"),
                        "verdict": "no_record"})
            continue
        r = max(recs, key=lambda x: x.get("output_chars") or 0)
        txt = r.get("output_text") or ""
        n_out = r.get("output_tokens") or 0
        truncated = bool(max_tok and n_out >= max_tok) or \
            r.get("finish_reason") == "length"
        blocks = extract_blocks(txt)
        best = pick_best(blocks)
        item = {
            "task": tid, "kind": kinds.get(tid, "?"),
            "status": r.get("status"), "chars": len(txt),
            "output_tokens": n_out, "max_tokens": max_tok,
            "finish_reason": r.get("finish_reason"),
            "truncated": truncated, "has_fence": bool(blocks),
            "n_blocks": len(blocks),
            "langs": sorted({b[0] for b in blocks}),
        }
        if best is None:
            item["verdict"] = "no_code"
            res.append(item)
            continue
        item["code_chars"] = len(best[1])
        with tempfile.TemporaryDirectory() as td:
            ok, f, err = try_compile(best[1], td, f"{d.name}_{tid}")
            item["syntax_ok"] = ok
            item["syntax_err"] = err
            item["verdict"] = "code_ok" if ok else "code_syntax"
            if ok:
                try:
                    m = try_import(f)
                    item["import_ok"] = True
                    item["defs"] = re.findall(r"^def ([A-Za-z_]\w*)\(", best[1], re.M)
                except Exception as e:                        # noqa: BLE001
                    item["import_ok"] = False
                    item["import_err"] = f"{type(e).__name__}: {str(e)[:80]}"
                # P03：真跑边界用例
                if tid == "P03" and item.get("import_ok"):
                    fn = getattr(m, "aggregate_by_owner", None)
                    if fn is None:
                        item["p03_fn_missing"] = True
                    else:
                        cases = run_cases(fn, P03_CASES)
                        item["cases"] = cases
                        item["cases_ok"] = sum(1 for _, _, o in cases if o)
                        item["cases_n"] = len(cases)
        res.append(item)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="把结果写到这个 json")
    args = ap.parse_args()

    kinds = task_kinds()
    code_tasks = sorted([t for t, k in kinds.items() if k == "code"])
    # P03/P04 虽然 kind=prompt，但题目要求它们产出代码，一并评估（单独标注）
    prompt_code_like = ["P03", "P04"]
    want = sorted(set(code_tasks) | set(prompt_code_like))

    print("=" * 96)
    print(" 任务类型（来自 src/common/tasks.py 的 kind 字段，权威定义）")
    print("=" * 96)
    print("   code   :", code_tasks)
    print("   prompt :", sorted([t for t, k in kinds.items() if k == "prompt"]))
    print("   doc    :", sorted([t for t, k in kinds.items() if k == "doc"]))
    print("   本次评估:", want, "（P03/P04 虽属 prompt，但题目要求它们给代码）")

    runs = sorted([d for d in RESULTS.iterdir() if d.is_dir()])
    allres = {}
    for d in runs:
        if not (d / "requests.jsonl").is_file():
            continue
        allres[d.name] = evaluate_run(d, kinds, want)

    print()
    print("=" * 96)
    print(" 判定结果（逐运行）")
    print("=" * 96)
    for rid, items in allres.items():
        cfg = json.loads((RESULTS / rid / "run-config.json").read_text(encoding="utf-8"))
        print("  ── %s   ctx=%s KV=%s/%s %s" % (
            rid, cfg.get("server_ctx"), cfg.get("kv_type_k"),
            cfg.get("kv_type_v"),
            (cfg.get("model_file") or "").replace("Qwen3-", "").replace(
                "-Q8_0.gguf", "")))
        for it in items:
            v = it["verdict"]
            icon = {"code_ok": "✅", "code_syntax": "❌", "no_code": "📄",
                    "no_record": "—"}.get(v, "?")
            extra = ""
            if v == "code_syntax":
                extra = "  " + (it.get("syntax_err") or "")[:60]
            elif v == "no_code":
                extra = "  没有代码围栏（纯文字输出）"
            elif v == "code_ok":
                extra = "  块=%d 语言=%s 定义=%s" % (
                    it["n_blocks"], ",".join(it["langs"]),
                    ",".join(it.get("defs") or [])[:50])
                if it.get("cases_n"):
                    extra += "  边界用例 %d/%d" % (it["cases_ok"], it["cases_n"])
            tag = "★截断" if it.get("truncated") else "    "
            print("     %s %-4s %-11s %-8s %s%s" % (
                icon, it["task"], v, tag, "", extra))
        print()

    # ── 汇总 ──
    print("=" * 96)
    print(" 汇总（跨运行）")
    print("=" * 96)
    cnt = defaultdict(Counter)
    for rid, items in allres.items():
        for it in items:
            cnt[it["task"]][it["verdict"]] += 1
    print("  %-6s %-9s %-8s %-9s %-9s %s" % (
        "任务", "kind", "code_ok", "code_syntax", "no_code", "总运行数"))
    for t in want:
        c = cnt[t]
        n = sum(c.values())
        print("  %-6s %-9s %-8s %-9s %-9s %s" % (
            t, kinds.get(t, "?"), c["code_ok"], c["code_syntax"],
            c["no_code"], n))

    if args.json:
        Path(args.json).write_text(
            json.dumps(allres, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print("  已写入 %s" % args.json)


if __name__ == "__main__":
    main()
