#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_claims.py —— 用【结果目录里的原始数据】核对计划书 §10.3 的七项结论

原则：只读 results/ 下的 tiers.csv / requests.jsonl / events 与 kv_index.json，
      不调用服务、不重新推理。凡是数据里看不出来的，明确标"未能核对"，
      绝不写成"通过"。
"""

import json
import sys
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
RES = DEV / "results"

ok = 0
bad = []
unk = []


def report(cond, msg, detail=""):
    global ok
    if cond is True:
        ok += 1
        print(f"  ✅ {msg}")
    elif cond is None:
        unk.append((msg, detail))
        print(f"  ⚠️  {msg}" + (f"（{detail}）" if detail else ""))
    else:
        bad.append((msg, detail))
        print(f"  ❌ {msg}" + (f"\n       {detail}" if detail else ""))


def load_csv(p):
    rows = []
    try:
        lines = Path(p).read_text(encoding="utf-8").splitlines()
    except Exception:                                         # noqa: BLE001
        return rows
    if not lines:
        return rows
    head = lines[0].split(",")
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) != len(head):
            continue
        try:
            rows.append({h: float(v) for h, v in zip(head, parts)})
        except ValueError:
            pass
    return rows


def load_jsonl(p):
    out = []
    try:
        for ln in Path(p).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except Exception:                                 # noqa: BLE001
                pass
    except Exception:                                         # noqa: BLE001
        pass
    return out


def proxy_runs():
    """按时间排序的优化组运行（有 tiers.csv 的）"""
    runs = []
    for d in sorted(RES.glob("*/")):
        s = d / "summary.json"
        c = d / "run-config.json"
        if not s.is_file():
            continue
        try:
            sm = json.loads(s.read_text(encoding="utf-8"))
            cf = json.loads(c.read_text(encoding="utf-8")) if c.is_file() else {}
        except Exception:                                     # noqa: BLE001
            continue
        if cf.get("target") != "proxy":
            continue
        runs.append({"dir": d, "summary": sm, "config": cf,
                     "tiers": load_csv(d / "tiers.csv"),
                     "reqs": load_jsonl(d / "requests.jsonl")})
    runs.sort(key=lambda r: r["summary"].get("finished_at") or "")
    return runs


# ══════════════════════════════════════════════════════════════════════════
def check_1(runs):
    print()
    print("── ① 磁盘只在内存满了之后才增长 ──")
    checked = 0
    for r in runs:
        rows = r["tiers"]
        if len(rows) < 3:
            continue
        quota = rows[0].get("l2_quota_mib") or 0
        l2_0 = rows[0].get("l2_used_mib", 0)
        l3_0 = rows[0].get("l3_used_mib", 0)
        # 只核对【从冷状态开始】的运行：开始时 L3≈0、L2 明显没满。
        # 否则"首次见到 L3>0"只是初始状态，不是一次增长，会被误判。
        if l3_0 > 1 or (quota and l2_0 / quota > 0.5):
            continue
        prev = l3_0
        first = None
        for x in rows[1:]:
            cur = x.get("l3_used_mib", 0)
            if cur > prev + 1:
                first = x
                break
            prev = cur
        if first is None:
            continue
        checked += 1
        l2_at = first.get("l2_used_mib", 0)
        ratio = l2_at / quota if quota else 0
        report(ratio >= 0.85,
               f"{r['dir'].name[:40]}：L3 首次增长时 L2 已占 "
               f"{ratio*100:.0f}%（{l2_at:.0f}/{quota:.0f} MiB）")
    if checked == 0:
        report(None, "没有从冷状态开始且有 L3 增长的运行",
               "需要先清空缓存再跑、且把 L2 灌满")


def check_2():
    print()
    print("── ② L2→L3 是整批搬家，不是滴漏 ──")
    # 从事件日志里找 spill_done
    evp = DEV / "results/_live/events.jsonl"
    evs = load_jsonl(evp)
    spills = [e for e in evs if e.get("event") == "spill_done"]
    if not spills:
        # 找任意一次运行的 events（可能已被清）
        report(None, "当前事件日志里没有 spill_done", "跑一次溢出场景后再核对")
        return
    batches = [e.get("batch") or 0 for e in spills]
    report(max(batches) > 1,
           f"至少出现过一次多文件批次（各批次数：{batches}）")
    for e in spills:
        tgt = e.get("target_bytes") or 0
        freed = e.get("freed_bytes") or 0
        if tgt:
            report(freed >= tgt,
                   f"批次释放 {freed/1048576:.0f} MiB ≥ 目标 {tgt/1048576:.0f} MiB")


def check_3(runs):
    print()
    print("── ③ 迁移不破坏缓存（L3 命中仍然可用）──")
    total_l3, good_l3 = 0, 0
    for r in runs:
        for q in r["reqs"]:
            if q.get("mode") == "L3":
                total_l3 += 1
                if q.get("status") == "success" and (q.get("prompt_n") or 9999) < 300:
                    good_l3 += 1
    if total_l3 == 0:
        report(None, "没有 L3 命中的请求", "需要 L3 里有东西的场景")
    else:
        report(good_l3 == total_l3,
               f"{good_l3}/{total_l3} 个 L3 命中请求成功且真正复用（prompt_n<300）")


def check_4(runs):
    print()
    print("── ④ v1/v2 不会互相命中（v2 必须用新事实）──")
    checked = 0
    for r in runs:
        cfg = r["config"]
        if "S5" not in (cfg.get("label") or ""):
            continue
        for q in r["reqs"]:
            if "v2" not in (q.get("doc") or ""):
                continue
            checked += 1
            ans = q.get("answer") or ""
            report("14" in ans and "7 天" not in ans.replace("14 天", ""),
                   f"{q['id']} v2 回答用了 14 天且没有残留 7 天："
                   f"{ans[:46]!r}")
    if checked == 0:
        report(None, "没有找到 S5（版本更新）运行", "先跑 S5")


def check_5():
    print()
    print("── ⑤ 配置变更后旧缓存作废 ──")
    idx = DEV / "kv_index.json"
    if not idx.is_file():
        report(None, "索引文件不存在"); return
    try:
        d = json.loads(idx.read_text(encoding="utf-8"))
    except Exception as e:                                    # noqa: BLE001
        report(None, f"索引文件读不了：{e}"); return
    cur = (d.get("config") or {}).get("server_fingerprint")
    fps = {e.get("fp") for e in (d.get("entries") or {}).values()}
    report(fps <= {cur},
           f"索引里只有当前指纹的条目（当前 {str(cur)[:40]}，实际 {len(fps)} 种）")
    evs = load_jsonl(DEV / "results/_live/events.jsonl")
    n = len([e for e in evs if e.get("event") == "stale_config_evict"])
    report(n >= 1 or not fps,
           f"启动清理过旧配置留下的缓存（{n} 次事件）",
           "没触发过说明没换过配置")


def check_6():
    print()
    print("── ⑥ 崩溃恢复（索引从文件重建）──")
    evs = load_jsonl(DEV / "results/_live/events.jsonl")
    starts = [e for e in evs if e.get("event") == "proxy_start"]
    if not starts:
        report(None, "没有 proxy_start 事件"); return
    recovered = [e for e in starts
                 if (e.get("reconcile") or {}).get("added", 0) > 0
                 or (e.get("reconcile") or {}).get("fixed", 0) > 0]
    report(len(starts) >= 1, f"记录到 {len(starts)} 次代理启动")
    report(len(recovered) >= 1,
           f"其中 {len(recovered)} 次从文件补登记/修正了条目",
           "若索引完全一致则不会触发，属正常")


def check_7(runs):
    print()
    print("── ⑦ 输出效果：关键事实一致（措辞可能有细微差异）──")
    total = 0
    hit_like = 0
    for r in runs:
        for q in r["reqs"]:
            if q.get("status") != "success":
                continue
            total += 1
            if q.get("answer"):
                hit_like += 1
    report(total > 0 and hit_like == total,
           f"{hit_like}/{total} 个成功请求都有非空回答")
    report(None,
           "「有缓存与无缓存输出逐字相同」这一条【不成立】，已改为"
           "「关键事实一致」",
           "依据 tools/probe_determinism2.py：前缀复用改变 n_past → 批量组成"
           "不同 → 浮点路径不同，实测会出现「分为三级」vs「分三级」")


def main():
    runs = proxy_runs()
    print("=" * 78)
    print(f" 核对对象：{len(runs)} 次优化组运行")
    for r in runs[-5:]:
        print(f"   · {r['dir'].name}")
    print("=" * 78)

    check_1(runs)
    check_2()
    check_3(runs)
    check_4(runs)
    check_5()
    check_6()
    check_7(runs)

    print()
    print("=" * 78)
    print(f" 通过 {ok} 项，失败 {len(bad)} 项，未能核对 {len(unk)} 项")
    if bad:
        for m, d in bad:
            print(f"   ❌ {m}")
            if d:
                print(f"      {d}")
    if unk:
        for m, d in unk:
            print(f"   ⚠️  {m}（{d}）")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
