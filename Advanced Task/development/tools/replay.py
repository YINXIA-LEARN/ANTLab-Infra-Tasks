#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay.py —— 按负载定义回放请求，采集原始数据

═══════════════════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════════════════
  # 优化组（经过代理）
  python3 tools/replay.py --workload configs/workloads/S1.json

  # 基线组（直连模型服务，无缓存）
  python3 tools/replay.py --workload configs/workloads/S1.json --target baseline

  # 跑 3 轮
  python3 tools/replay.py --workload configs/workloads/S1.json --rounds 3

  # 重启服务后再跑（测 L3 冷启动）
  python3 tools/replay.py --workload configs/workloads/S6.json --restart-first

═══════════════════════════════════════════════════════════════════════════
 输出（每次运行一个目录）
═══════════════════════════════════════════════════════════════════════════
  results/<label>-<时间戳>/
    ├─ requests.jsonl   逐请求：计划/实际到达、模式、耗时、token
    ├─ tiers.csv        三层容量时间序列（250ms 采样）
    ├─ summary.json     汇总：命中率、复用比例、分模式耗时
    ├─ run-config.json  本次运行的全部参数（便于复现）
    └─ run.log          人可读的过程日志
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
PROXY_URL = "http://127.0.0.1:8091"
BASE_URL = "http://127.0.0.1:8092"


# ══════════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════════
def http_json(url, body=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            raw = f.read().decode("utf-8", "replace")
            return {"status": f.status, "body": json.loads(raw),
                    "wall_ms": (time.time() - t0) * 1000, "error": None}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", "replace"),
                "wall_ms": (time.time() - t0) * 1000,
                "error": f"HTTP {e.code}"}
    except Exception as e:                                    # noqa: BLE001
        return {"status": None, "body": None,
                "wall_ms": (time.time() - t0) * 1000,
                "error": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════════════════
#  工作负载
# ══════════════════════════════════════════════════════════════════════════
def load_workload(path):
    w = json.loads(Path(path).read_text(encoding="utf-8"))
    reqs = w.get("requests") or []
    for i, r in enumerate(reqs):
        r.setdefault("id", f"r{i+1:02d}")
        r.setdefault("at_ms", i * 500)
        r.setdefault("max_tokens", w.get("max_tokens", 96))
        r.setdefault("user", "U00")
    reqs.sort(key=lambda r: r["at_ms"])
    return w, reqs


def doc_text(rel):
    p = Path(rel)
    if not p.is_absolute():
        p = DEV / "corpus" / rel
    return p.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
#  三层采样
# ══════════════════════════════════════════════════════════════════════════
class TierSampler(threading.Thread):
    def __init__(self, csv_path, interval=0.25):
        super().__init__(daemon=True)
        self.csv_path = Path(csv_path)
        self.interval = interval
        self.stop_flag = threading.Event()
        self.rows = []

    def run(self):
        with self.csv_path.open("w", encoding="utf-8") as f:
            f.write("elapsed_s,l1_slots_used,l2_used_mib,l2_quota_mib,"
                    "l3_used_mib,l3_quota_mib,entries,requests\n")
            t0 = time.time()
            while not self.stop_flag.is_set():
                try:
                    r = HTTP_status()
                    if r:
                        slots = r.get("slots") or {}
                        used_slots = sum(1 for v in slots.values() if v)
                        t = r.get("tiers") or {}
                        e = r.get("entries") or {}
                        st = r.get("stat") or {}
                        f.write(f"{time.time()-t0:.2f},{used_slots},"
                                f"{t.get('l2_used',0)/1048576:.1f},"
                                f"{t.get('l2_quota',0)/1048576:.1f},"
                                f"{t.get('l3_used',0)/1048576:.1f},"
                                f"{t.get('l3_quota',0)/1048576:.1f},"
                                f"{sum(e.values())},{st.get('requests',0)}\n")
                        f.flush()
                        self.rows.append(r)
                except Exception:                             # noqa: BLE001
                    pass
                self.stop_flag.wait(self.interval)

    def stop(self):
        self.stop_flag.set()


def HTTP_status():
    r = http_json(PROXY_URL + "/admin/status", timeout=5)
    b = r.get("body")
    return b if isinstance(b, dict) else None


# ══════════════════════════════════════════════════════════════════════════
#  单请求
# ══════════════════════════════════════════════════════════════════════════
def run_one(base, req, plan_t0):
    """等到计划到达时刻，发一次请求，返回记录"""
    target_at = plan_t0 + req["at_ms"] / 1000.0
    now = time.time()
    if target_at > now:
        time.sleep(target_at - now)
    actual_at = time.time()

    rec = {
        "id": req["id"], "user": req.get("user"), "doc": req["doc"],
        "at_ms": req["at_ms"],
        "planned_at": round(target_at - plan_t0, 3),
        "actual_at": round(actual_at - plan_t0, 3),
        "arrival_dev_ms": round((actual_at - target_at) * 1000, 1),
        "wall_ms": 0.0,
        "status": None, "mode": None, "slot": None,
        "prompt_n": None, "prompt_total": None, "reused_tokens": None,
        "prefill_ms": None, "gen_ms": None, "gen_n": None,
        "answer": "", "error": None,
    }

    try:
        body = {
            "messages": [
                {"role": "system",
                 "content": "你是澄禾工作室的资料助手，回答要简洁准确。"},
                {"role": "user", "content": doc_text(req["doc"])},
                {"role": "user", "content": req.get("q", "请概括要点。")},
            ],
            "max_tokens": req.get("max_tokens", 96),
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    except Exception as e:                                    # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"读文档失败: {type(e).__name__}: {e}"
        rec["wall_ms"] = round((time.time() - actual_at) * 1000, 1)
        return rec

    r = http_json(base + "/v1/chat/completions", body)
    done_at = time.time()
    rec["wall_ms"] = round((done_at - actual_at) * 1000, 1)

    if r.get("error"):
        rec["status"] = "error"
        rec["error"] = str(r.get("error"))[:200]
        return rec

    d = r.get("body")
    if not isinstance(d, dict):
        rec["status"] = "bad_body"
        rec["error"] = str(d)[:200]
        return rec

    px = d.get("_proxy") or {}
    tim = d.get("timings") or {}
    use = d.get("usage") or {}
    rec["status"] = "success"
    rec["mode"] = px.get("mode") or "baseline"
    rec["slot"] = px.get("slot")
    rec["prompt_n"] = tim.get("prompt_n")
    rec["prompt_total"] = use.get("prompt_tokens")
    rec["reused_tokens"] = px.get("reused_tokens")
    if rec["reused_tokens"] is None and rec["prompt_total"] and rec["prompt_n"]:
        rec["reused_tokens"] = max(0, rec["prompt_total"] - rec["prompt_n"])
    rec["prefill_ms"] = round(tim.get("prompt_ms") or 0, 1)
    rec["gen_ms"] = round(tim.get("predicted_ms") or 0, 1)
    rec["gen_n"] = tim.get("predicted_n")
    rec["answer"] = ((d.get("choices") or [{}])[0].get("message") or {}) \
        .get("content", "") or ""
    return rec


# ══════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", required=True)
    ap.add_argument("--target", choices=["proxy", "baseline"], default="proxy")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--gap-s", type=float, default=2.0,
                    help="两轮之间的间隔（给容量维护留时间）")
    ap.add_argument("--label", default=None)
    ap.add_argument("--restart-first", action="store_true",
                    help="跑之前重启服务（L2 清空，L3 保留 → 测冷启动）")
    ap.add_argument("--restart-between-rounds", action="store_true")
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    w, reqs = load_workload(args.workload)
    base = PROXY_URL if args.target == "proxy" else BASE_URL
    label = args.label or w.get("name") or Path(args.workload).stem
    run_id = f"{label}-{args.target}-{time.strftime('%Y%m%d-%H%M%S')}"
    out = DEV / "results" / run_id
    out.mkdir(parents=True, exist_ok=True)

    logf = (out / "run.log").open("w", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log(f"运行编号 {run_id}")
    log(f"负载 {w.get('name')}  {w.get('desc','')}")
    log(f"目标 {args.target} → {base}")
    log(f"请求 {len(reqs)} 条 × {args.rounds} 轮，max_tokens={w.get('max_tokens')}")

    (out / "run-config.json").write_text(json.dumps({
        "run_id": run_id, "label": label, "target": args.target,
        "base_url": base, "workload": str(args.workload),
        "workload_name": w.get("name"), "rounds": args.rounds,
        "n_requests": len(reqs), "gap_s": args.gap_s,
        "restart_first": args.restart_first,
        "restart_between_rounds": args.restart_between_rounds,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "corpus_manifest": json.loads(
            (DEV / "corpus" / "manifest.json").read_text(encoding="utf-8")),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 前置检查
    r = http_json(base + "/health", timeout=5)
    if r.get("status") != 200:
        log(f"❌ 目标服务不可用: {r.get('error') or r.get('status')}")
        logf.close()
        return 1

    def restart_services():
        log("  重启进阶服务（L2 清空，L3 保留）…")
        subprocess.run(["bash", str(DEV / "tools" / "stop_adv.sh")],
                       capture_output=True, timeout=180)
        p = subprocess.run(["bash", str(DEV / "tools" / "start_adv.sh")],
                           capture_output=True, text=True, timeout=700)
        ok = "代理入口" in (p.stdout + p.stderr)
        log("  " + ("✅ 已重启" if ok else "⚠️ 重启异常"))
        time.sleep(2)
        return ok

    if args.restart_first:
        restart_services()

    # 预热（不计入统计）
    sampler = None
    if not args.no_warmup and args.target == "proxy":
        log("预热：发一条与本负载无关的短请求，不影响缓存统计")
        wm = None
        # ⚠️ 不要用负载里的文档预热 —— 那会把它提前塞进缓存，
        #    导致第一条正式请求就"命中"，统计失真。
        r = http_json(base + "/v1/chat/completions", {
            "messages": [
                {"role": "system", "content": "你是助手。"},
                {"role": "user", "content": "预热请求：请回答「好」。"}],
            "max_tokens": 8, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        wm = {"status": "success" if r.get("status") == 200 else "error",
              "mode": ((r.get("body") or {}).get("_proxy") or {}).get("mode")
              if isinstance(r.get("body"), dict) else None}
        log(f"  预热结果 status={wm['status']} mode={wm['mode']}")

    all_recs = []
    summaries = []

    for rnd in range(1, args.rounds + 1):
        if rnd > 1 and args.restart_between_rounds:
            restart_services()
        log("")
        log(f"══ 第 {rnd}/{args.rounds} 轮 开始 ══")

        if args.target == "proxy" and sampler is None:
            sampler = TierSampler(out / "tiers.csv")
            sampler.start()
            log("  三层采样已启动（250 ms）")

        plan_t0 = time.time() + 0.3
        threads, results = [], {}
        done_ids = []

        def worker(rq):
            try:
                results[rq["id"]] = run_one(base, rq, plan_t0)
            except Exception as e:                            # noqa: BLE001
                results[rq["id"]] = {
                    "id": rq["id"], "doc": rq.get("doc"), "at_ms": rq["at_ms"],
                    "status": "error", "mode": None, "error":
                        f"{type(e).__name__}: {e}"}
            done_ids.append(rq["id"])
            if len(done_ids) % 5 == 0 or len(done_ids) == len(reqs):
                log(f"    进度 {len(done_ids)}/{len(reqs)}")

        for req in reqs:
            t = threading.Thread(target=worker, args=(req,), daemon=True)
            threads.append(t)
            t.start()
        # 看门狗：给整轮一个上限，超时就记错并继续，不要让整个跑批卡死
        deadline = plan_t0 + max(r["at_ms"] for r in reqs) / 1000.0 + 600
        for t in threads:
            remain = max(1.0, deadline - time.time())
            t.join(timeout=remain)
        alive = [t for t in threads if t.is_alive()]
        if alive:
            log(f"  ⚠️ 有 {len(alive)} 个请求线程超过本轮上限仍未返回，记为超时")
            for rq in reqs:
                if rq["id"] not in results:
                    results[rq["id"]] = {
                        "id": rq["id"], "doc": rq.get("doc"),
                        "at_ms": rq["at_ms"], "status": "timeout",
                        "mode": None, "error": "watchdog_timeout"}

        recs = [results[r["id"]] for r in reqs if r["id"] in results]
        for x in recs:
            x["round"] = rnd
            all_recs.append(x)

        # 本轮小结
        ok = [x for x in recs if x["status"] == "success"]
        modes = {}
        for x in ok:
            modes[x["mode"]] = modes.get(x["mode"], 0) + 1
        reuse = sum(x.get("reused_tokens") or 0 for x in ok)
        tot = sum(x.get("prompt_total") or 0 for x in ok)
        pf = [x["prefill_ms"] for x in ok if x["prefill_ms"] is not None]
        log(f"  完成 {len(ok)}/{len(recs)} 条；模式 {modes}")
        if pf:
            log(f"  prefill 合计 {sum(pf):.0f} ms，中位 {statistics.median(pf):.0f} ms")
        if tot:
            log(f"  复用 token {reuse}/{tot} = {reuse/tot*100:.1f}%")
        summaries.append({"round": rnd, "n": len(recs), "ok": len(ok),
                          "modes": modes, "reuse_tokens": reuse,
                          "total_prompt_tokens": tot,
                          "prefill_ms_sum": round(sum(pf), 1) if pf else 0})

        if rnd < args.rounds:
            time.sleep(args.gap_s)

    if sampler:
        sampler.stop()
        sampler.join(timeout=3)

    # 写逐请求
    with (out / "requests.jsonl").open("w", encoding="utf-8") as f:
        for x in all_recs:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    # 汇总
    ok = [x for x in all_recs if x["status"] == "success"]
    modes = {}
    for x in ok:
        modes[x["mode"]] = modes.get(x["mode"], 0) + 1
    reuse = sum(x.get("reused_tokens") or 0 for x in ok)
    tot = sum(x.get("prompt_total") or 0 for x in ok)
    pf = sorted(x["prefill_ms"] for x in ok if x["prefill_ms"] is not None)
    walls = sorted(x["wall_ms"] for x in ok)
    devs = [abs(x["arrival_dev_ms"]) for x in all_recs
            if x.get("arrival_dev_ms") is not None]

    def pct(v, q):
        if not v:
            return None
        i = min(len(v) - 1, int(len(v) * q))
        return round(v[i], 1)

    by_doc = {}
    for x in ok:
        d = by_doc.setdefault(x["doc"], {"n": 0, "prefill_ms": 0,
                                         "reuse": 0, "total": 0, "modes": {}})
        d["n"] += 1
        d["prefill_ms"] += x["prefill_ms"] or 0
        d["reuse"] += x.get("reused_tokens") or 0
        d["total"] += x.get("prompt_total") or 0
        m = x["mode"]
        d["modes"][m] = d["modes"].get(m, 0) + 1

    summary = {
        "run_id": run_id, "label": label, "target": args.target,
        "workload": w.get("name"), "rounds": args.rounds,
        "n_total": len(all_recs), "n_success": len(ok),
        "n_error": len(all_recs) - len(ok),
        "modes": modes,
        "hit_rate": round((len(ok) - modes.get("miss", 0) - modes.get("baseline", 0))
                          / max(1, len(ok)), 4),
        "reuse_tokens": reuse, "total_prompt_tokens": tot,
        "reuse_ratio": round(reuse / max(1, tot), 4),
        "prefill_ms": {"sum": round(sum(pf), 1), "p50": pct(pf, 0.5),
                       "p95": pct(pf, 0.95), "max": pct(pf, 0.999)},
        "wall_ms": {"p50": pct(walls, 0.5), "p95": pct(walls, 0.95)},
        "arrival_dev_ms": {"max": round(max(devs), 1) if devs else None,
                           "mean": round(statistics.mean(devs), 1) if devs else None},
        "per_round": summaries,
        "per_doc": by_doc,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("")
    log("══ 汇总 ══")
    log(f"  成功 {len(ok)}/{len(all_recs)}  模式 {modes}")
    log(f"  命中率 {summary['hit_rate']*100:.1f}%  "
        f"复用 token {summary['reuse_ratio']*100:.1f}%")
    log(f"  prefill 合计 {summary['prefill_ms']['sum']:.0f} ms  "
        f"p50 {summary['prefill_ms']['p50']} ms  p95 {summary['prefill_ms']['p95']} ms")
    log(f"  输出目录 {out}")
    logf.close()
    print(f"\n输出目录: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
