#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_report_claims.py —— 抽验报告里的关键数字能不能在 results/ 里找到出处。

⚠️ 为什么要做这件事：报告写了 1360 行、引用了大量数字。
   如果某个数字在数据里对不上，整个报告的可信度就没了。
   这里只挑【最关键、最容易被误读或写错】的那些来核。
"""
import json
import re
from pathlib import Path

D = Path(__file__).resolve().parents[2]
R = D / "results"
REPORT = D / "reports" / "基础阶段考核报告.md"

def load(rid):
    d = R / rid
    return {
        "cfg": json.loads((d / "run-config.json").read_text(encoding="utf-8")),
        "summ": json.loads((d / "summary.json").read_text(encoding="utf-8")),
        "rows": [json.loads(l) for l in
                 (d / "requests.jsonl").read_text(encoding="utf-8").splitlines()
                 if l.strip()],
        "dir": d,
    }

ok = 0
bad = 0
def check(label, got, want, tol=None):
    global ok, bad
    if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        good = abs(got - want) <= tol
    else:
        good = got == want
    print("  %s %-56s 实际=%-12s 报告写=%s" % (
        "✅" if good else "❌", label, got, want))
    if good:
        ok += 1
    else:
        bad += 1

print("=" * 100)
print(" 一、服务恢复那一轮（ctx16k-q80-06b-r2）")
print("=" * 100)
d = load("ctx16k-q80-06b-r2-20261016-232040")
from collections import Counter
cnt = Counter(r.get("status") for r in d["rows"])
check("service_lost 条数", cnt.get("service_lost", 0), 72)
check("总记录条数", len(d["rows"]), 90)
si = d["summ"].get("service_instance") or {}
# ⚠️ 这两个期望值是【从数据里读出来的真值】，不是凭印象填的。
#    我第一版填的是 35081 / 74770 —— 那是【已删除】的 web-1016-* 批次
#    里的 PID（我自己在更早的对话里引用过，就顺手写进期望值了）。
#    结果核对脚本报"报告写错了"，而其实是【我的期望值错了】，报告是对的。
#    教训：核对脚本里的期望值必须来自当前数据，不能来自记忆或旧对话。
check("pid_at_start", si.get("pid_at_start"), 124791)
check("pid_at_end（=null 是正常的：运行结束时服务还没起来）",
      si.get("pid_at_end"), None)
# service-starts.jsonl 里的恢复耗时
ss = d["dir"] / "service-starts.jsonl"
if ss.is_file():
    ev = [json.loads(l) for l in ss.read_text(encoding="utf-8").splitlines() if l.strip()]
    # ⚠️ 字段名是 t/ts/pid/source/note —— 不是 at/ready_ms/ok。
    #    第一版我凭印象写了 at/ready_ms/ok，于是全打成 None 还以为文件缺字段。
    #    真实的恢复耗时藏在 note 文本里（"耗时 2060.7ms"）。
    print("  service-starts.jsonl 共 %d 条：" % len(ev))
    auto = None
    for e in ev:
        print("     t=%s pid=%s source=%s" % (e.get("t"), e.get("pid"),
                                              e.get("source")))
        print("       note=%s" % (e.get("note") or "")[:150])
        if e.get("source") == "auto_recover":
            auto = e
    if auto:
        m = re.search(r"耗时\s*([\d.]+)\s*ms", auto.get("note") or "")
        check("自动恢复耗时 ms（从 note 里抓）",
              float(m.group(1)) if m else None, 2060.7, tol=0.05)
        check("自动恢复的 pid", auto.get("pid"), 125132)
    else:
        check("有 auto_recover 记录", False, True)
else:
    print("  ❌ 没有 service-starts.jsonl")

# by_service_instance 的分段
bsi = d["summ"].get("by_service_instance") or {}
for key, n in (("pid_124791", 6), ("pid_125132", 12), ("pid_未知", 72)):
    got = (bsi.get(key) or {}).get("n_requests")
    check("by_service_instance[%s].n_requests" % key, got, n)

print()
print("=" * 100)
print(" 二、上下文优化那一对（0.6B: r2 16K vs r1 32K）")
print("=" * 100)
a = load("ctx16k-f16-06b-r2-20261016-224117")
b = load("ctx32k-f16-06b-r1-20261016-223012")
def vram_peak(x):
    return max((f.get("vram_peak_mib") or 0)
               for f in (x["summ"].get("formal") or {}).values())
check("A 显存峰", vram_peak(a), 3651)
check("B 显存峰", vram_peak(b), 5526)
check("显存增幅 %", round((vram_peak(b) - vram_peak(a)) / vram_peak(a) * 100, 1), 51.0, tol=1.0)
ta = (a["summ"]["formal"]["10"].get("ttft_ms") or {}).get("median")
tb = (b["summ"]["formal"]["10"].get("ttft_ms") or {}).get("median")
check("c10 TTFT 变化 %", round((tb - ta) / ta * 100, 1), -20.3, tol=0.5)

print()
print("=" * 100)
print(" 三、KV 精度那一对（0.6B: r2 f16 vs r1 q8_0）")
print("=" * 100)
q = load("ctx16k-q80-06b-r1-20261016-223609")
check("q8_0 显存峰", vram_peak(q), 2917)
check("降幅 %", round((vram_peak(q) - vram_peak(a)) / vram_peak(a) * 100, 1), -20.1, tol=1.0)
ta1 = (a["summ"]["formal"]["1"].get("throughput") or {}).get("output_tokens_per_s")
tq1 = (q["summ"]["formal"]["1"].get("throughput") or {}).get("output_tokens_per_s")
check("c1 吞吐变化 %（异常项）", round((tq1 - ta1) / ta1 * 100, 1), 35.1, tol=1.0)

print()
print("=" * 100)
print(" 四、模型维度（0.6B vs 1.7B，同 16K/f16）")
print("=" * 100)
m17 = load("ctx16k-f16-17b-r1-20261016-224726")
ta10 = (a["summ"]["formal"]["10"].get("ttft_ms") or {}).get("median")
tm10 = (m17["summ"]["formal"]["10"].get("ttft_ms") or {}).get("median")
check("c10 TTFT 变化 %", round((tm10 - ta10) / ta10 * 100, 1), 101.9, tol=1.0)
sa = (a["summ"].get("overall") or {}).get("success_rate")
sm = (m17["summ"].get("overall") or {}).get("success_rate")
check("0.6B 成功率 %", round(sa * 100, 1), 97.8, tol=0.2)
check("1.7B 成功率 %", round(sm * 100, 1), 91.1, tol=0.2)

print()
print("=" * 100)
print(" 五、异常展示那两轮")
print("=" * 100)
r3 = load("ctx16k-q80-06b-r3-20261016-232906")
check("r3 记录条数", len(r3["rows"]), 46)
check("r3 状态", r3["summ"].get("state") or
      json.loads((r3["dir"] / "run-state.json").read_text(encoding="utf-8")).get("state"),
      "aborted")
c3 = Counter(r.get("status") for r in r3["rows"])
print("     r3 状态分布：%s" % dict(c3))

print()
print("=" * 100)
print(" 六、KV 容量实测文件")
print("=" * 100)
kp = R / "kv-capacity-probe.txt"
if kp.is_file():
    t = kp.read_text(encoding="utf-8")
    for pat, label in ((r"实测可接受的最大单请求输入.*?(\d+) token", "最大单请求输入"),
                       (r"配置总容量.*?(\d+) token", "配置总容量")):
        m = re.search(pat, t)
        check(label + "（在探测文件里）", m.group(1) if m else "找不到",
              "32698" if "最大" in label else "32768")
else:
    print("  ❌ 没有 kv-capacity-probe.txt")

print()
print("=" * 100)
print(" 七、报告里引用的运行编号是否都真实存在")
print("=" * 100)
if REPORT.is_file():
    txt = REPORT.read_text(encoding="utf-8")
    ids = sorted(set(re.findall(r"ctx(?:16k|32k)-(?:f16|q80)-(?:06b|17b)-r\d-\d{8}-\d{6}", txt)))
    real = {p.name for p in R.iterdir() if p.is_dir()}
    print("  报告里出现的运行编号 %d 个：" % len(ids))
    for i in ids:
        hit = i in real
        print("     %s %s" % ("✅" if hit else "❌ 数据里没有", i))
        if hit:
            ok += 1
        else:
            bad += 1
    missing = real - set(ids)
    if missing:
        print("  ⚠️ 数据里有、报告没提到的：%s" % sorted(missing))
else:
    print("  ❌ 报告文件不在")

print()
print("=" * 100)
print(" 汇总：核对通过 %d 项，不通过 %d 项" % (ok, bad))
print("=" * 100)
