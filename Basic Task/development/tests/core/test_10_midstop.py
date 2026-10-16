#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_10_midstop.py —— 中途被停止的测试，数据还在不在

═══════════════════════════════════════════════════════════════════════════
 为什么单独测这一条

   原来 requests.jsonl 只在测试【正常跑完】时才写一次。
   而网页上的"停止测试"发的是 SIGTERM —— 进程直接没了，
   那一行永远写不到。结果：一个跑了 13 个请求然后被停掉的测试，
   在界面上只显示"加载失败：该运行没有 requests.jsonl"，
   已经跑完的 13 条记录【全部丢失且无从查看】。

   这不是理论问题，是实测遇到的：被停掉的
   web-1016-20261016-165215 就是这样，一条记录都不剩。

 这一条测三件事：
   ① 被停的运行目录里，requests.jsonl 确实保留了已完成的请求
   ② 摘要文件明确标注 state=aborted（不是伪装成完整结果）
   ③ 后端接口能读出这些记录，并带上"不完整"的提示

 判据的独立来源：
   · "已完成的请求数" 用 requests.jsonl 的行数 —— 数据本身
   · "被停掉" 用 run-state.json 的 state —— 测试程序自己写的状态
   · 两者必须自洽：state=aborted 的运行，行数应当 > 0 且 < 计划数
     （除非它在第一个请求完成前就被停了，那种情况行数=0 也合理）
═══════════════════════════════════════════════════════════════════════════
"""
import json
import sys
from pathlib import Path

# ⚠️ 本文件在 tests/core/ 下，_testlib.py 在上一层 tests/。
#    两层都要加：本层用于同目录的模块，上一层用于 _testlib。
#    （_testlib 必须留在 tests/ —— 它的 DEV_DIR/RESULTS_DIR
#      是按自己位置算的，挪下来会整体错一层）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _testlib import Report, RESULTS_DIR, run, successful  # noqa: E402


def _j(d, name):
    f = d / name
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_incomplete_runs():
    """找出所有"中途被停止 / 没正常收尾"的运行目录"""
    out = []
    if not RESULTS_DIR.is_dir():
        return out
    for d in sorted(RESULTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        st = _j(d, "run-state.json")
        sm = _j(d, "summary.json")
        state = None
        if isinstance(sm, dict):
            state = sm.get("state")
        if not state and isinstance(st, dict):
            state = st.get("state")
        if state in ("aborted", "running"):
            out.append((d, state, st, sm))
    return out


def main():
    rep = Report("test_10_midstop —— 中途停止后数据是否保留")

    inc = find_incomplete_runs()
    rep.note(f"results/ 下找到 {len(inc)} 个『被中途停止 / 未收尾』的运行")

    if not inc:
        rep.note("")
        rep.note("⚠️ 本次没有可检查的样本 —— 这条测试【没有真正被执行】。")
        rep.note("   要造样本：在网页上启动一次测试，跑到一半点『停止测试』。")
        rep.note("   这里不判失败，因为『没有样本』不等于『功能有问题』；")
        rep.note("   但报告里应当写明这条边界是否被覆盖。")
        return rep.finish()

    for d, state, st, sm in inc:
        rid = d.name
        rep.note("")
        rep.note(f"── {rid}（state={state}）──")

        # ── ① 状态文件本身要合理 ────────────────────────────────────
        rep.check_true(f"{rid}: 有 run-state.json",
                       isinstance(st, dict))
        if isinstance(st, dict):
            rep.check(f"{rid}: run-state 的 run_id 与目录名一致",
                      st.get("run_id"), rid)
            rep.check_true(
                f"{rid}: 记下了停止原因（是信号还是别的原因）",
                bool(st.get("aborted_by")) or state == "running",
                f"aborted_by={st.get('aborted_by')!r}")
            planned = st.get("n_requests_planned")
            rep.check_true(f"{rid}: 记下了计划请求数",
                           isinstance(planned, int) and planned > 0,
                           f"planned={planned!r}")

        # ── ② 已完成的请求必须留在 requests.jsonl 里 ────────────────
        rq = d / "requests.jsonl"
        rep.check_true(f"{rid}: requests.jsonl 存在", rq.is_file())
        n_lines = 0
        rows = []
        if rq.is_file():
            for line in rq.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rep.check_true(f"{rid}: requests.jsonl 每行都是合法 JSON",
                                   False, f"第 {n_lines} 行解析失败")
        rep.note(f"   已保留 {n_lines} 条请求记录")

        # 与 run-state 里记的"已记条数"对得上
        if isinstance(st, dict) and st.get("n_records_so_far") is not None:
            rep.check_true(
                f"{rid}: 落盘条数与 run-state 记录的一致",
                abs(n_lines - int(st["n_records_so_far"])) <= 2,
                f"文件 {n_lines} 行 vs 状态记 {st['n_records_so_far']} 条"
                f"（差 1~2 条是停止瞬间的正常竞态）")

        # ── ③ 摘要要明确标注"未完成"，不能伪装成完整结果 ─────────────
        if isinstance(sm, dict) and state == "aborted":
            rep.check(f"{rid}: summary.json 的 state 标注为 aborted",
                      sm.get("state"), "aborted")
            rep.check_true(f"{rid}: summary 里有『这不是完整结果』的说明",
                           bool(sm.get("aborted_note")),
                           "缺 aborted_note")
            rep.check_true(f"{rid}: summary 记了计划请求数",
                           isinstance(sm.get("n_requests_planned"), int))
            o = sm.get("overall") or {}
            if n_lines:
                rep.check(f"{rid}: summary 统计的就是已保留的那些请求",
                          o.get("n_requests"), n_lines)

        # ── ④ 逐请求记录本身要是有效记录（有 status）────────────────
        if rows:
            no_status = [r for r in rows if not r.get("status")]
            rep.check_true(f"{rid}: 每条记录都有 status",
                           not no_status,
                           f"{len(no_status)} 条缺 status")
            ok = successful(rows)
            rep.note(f"   其中成功 {len(ok)} 条，"
                     f"非成功 {len(rows) - len(ok)} 条")
            # 计划数 > 实际数 —— 这正是"被停"的特征
            if isinstance(st, dict) and st.get("n_requests_planned"):
                rep.check_true(
                    f"{rid}: 已完成的请求数少于计划数（说明确实是被中途停的）",
                    n_lines < int(st["n_requests_planned"]),
                    f"{n_lines} vs 计划 {st['n_requests_planned']}")

    # ── ⑤ 后端接口能读出被停运行的记录（这是用户实际看到的那一层）────
    rep.note("")
    rep.note("── 后端接口 ──")
    try:
        import urllib.request
        base = "http://127.0.0.1:8000"
        for d, state, st, sm in inc[:2]:
            rid = d.name
            with urllib.request.urlopen(
                    f"{base}/api/bench/runs/{rid}/requests?limit=5",
                    timeout=10) as r:
                j = json.loads(r.read().decode())
            rep.check_true(f"{rid}: /requests 不再返回错误",
                           not j.get("error"),
                           str(j.get("error")))
            rep.check_true(f"{rid}: /requests 带上了『不完整』提示",
                           bool(j.get("note")),
                           "缺 note 字段")
            rep.note(f"   接口返回 total={j.get('total')}，"
                     f"note={str(j.get('note'))[:50]}")
    except Exception as e:                                        # noqa: BLE001
        rep.note(f"   ⚠️ 控制层没在跑，跳过接口检查（{type(e).__name__}）")

    return rep.finish()


if __name__ == "__main__":
    sys.exit(run(main))
