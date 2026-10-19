#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_spill.py —— 验证「L2 满了才搬到 L3，而且是一口气整批搬」

═══════════════════════════════════════════════════════════════════════════
 要验证的四件事
═══════════════════════════════════════════════════════════════════════════
 ① 磁盘【只在 L2 超配额后】才增长
 ② 搬家是【整批】，不是搬一条
 ③ 搬家【不破坏缓存】—— 搬过去的文件 restore 回来仍然可用
 ④ 搬家后 L2 回落到配额以下，并留出余量（一次搬 30%）

 做法：L2 配额临时改成 1 GiB，灌 13 份文档两轮；
       第一轮观察用量曲线与搬家批次，第二轮看命中情况。跑完还原配置。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEV = Path(os.environ.get(
    "ADV_DEV",
    str(Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development")))
PROXY = "http://127.0.0.1:8091"
CFG = DEV / "configs" / "cache.json"
TEST_L2 = 1024 * 1024 * 1024
TEST_L3 = 8 * 1024 * 1024 * 1024


def sh(cmd, timeout=700):
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def req(path, body=None, timeout=900):
    url = PROXY + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            t = f.read().decode("utf-8", "replace")
            try:
                return f.status, json.loads(t)
            except Exception:                                 # noqa: BLE001
                return f.status, t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def status():
    c, d = req("/admin/status")
    return d if isinstance(d, dict) else {}


def events(limit=600):
    c, ev = req(f"/admin/events?limit={limit}")
    out = []
    if isinstance(ev, str):
        for line in ev.splitlines():
            try:
                out.append(json.loads(line))
            except Exception:                                 # noqa: BLE001
                pass
    return out


def ask(doc, question):
    text = Path(doc).read_text(encoding="utf-8")
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁。"},
        {"role": "user", "content": text},
        {"role": "user", "content": question}],
        "max_tokens": 48, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.time()
    code, d = req("/v1/chat/completions", body)
    wall = (time.time() - t0) * 1000
    if code != 200 or not isinstance(d, dict):
        return {"mode": f"ERR{code}", "prompt_n": None, "wall": wall}
    px = d.get("_proxy") or {}
    t = d.get("timings") or {}
    return {"mode": px.get("mode"), "slot": px.get("slot"),
            "reused": px.get("reused_tokens"),
            "prompt_n": t.get("prompt_n"), "wall": wall}


def mb(n):
    return f"{n/1048576:.0f}M"


def main():
    orig = CFG.read_text(encoding="utf-8")
    cfg = json.loads(orig)
    print("=" * 90)
    print(f" 准备：L2 配额临时改成 {TEST_L2//1048576} MiB，L3 {TEST_L3//1073741824} GiB")
    print("=" * 90)
    cfg["l2_quota_bytes"] = TEST_L2
    cfg["l3_quota_bytes"] = TEST_L3
    cfg["l2_spill_ratio"] = 0.30
    cfg["spill_cooldown_s"] = 0.0
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    sh(f'bash "{DEV}/tools/stop_adv.sh"', 120)
    sh(f'rm -rf /dev/shm/antlab_kv "{DEV}/kv_cache/L3" "{DEV}/kv_index.json" '
       f'"{DEV}/results/_live"', 120)
    rc, out = sh(f'bash "{DEV}/tools/start_adv.sh"', 700)
    if "代理入口" not in out:
        print(out[-1200:])
        CFG.write_text(orig, encoding="utf-8")
        print("❌ 启动失败，已还原配置")
        return

    docs = (sorted((DEV / "corpus" / "small").glob("*.md"))
            + sorted((DEV / "corpus" / "large").glob("*.md"))
            + sorted((DEV / "corpus" / "versions").glob("*.md")))

    print()
    print("=" * 90)
    print(f" 第一轮：灌 {len(docs)} 份文档")
    print("=" * 90)
    print(f"  {'#':>2} {'文档':<28}{'模式':<8}{'prompt_n':>8}{'L2':>8}{'L3':>8}"
          f"{'ΔL3':>8}")
    prev_l3 = 0
    curve = []
    for i, p in enumerate(docs, 1):
        r = ask(p, "用一句话概括这份资料。")
        s = status()
        t = s.get("tiers") or {}
        l2, l3 = t.get("l2_used", 0), t.get("l3_used", 0)
        curve.append({"i": i, "doc": p.name, "mode": r.get("mode"),
                      "l2": l2, "l3": l3})
        print(f"  {i:>2} {p.name[:28]:<28}{str(r.get('mode')):<8}"
              f"{str(r.get('prompt_n')):>8}{mb(l2):>8}{mb(l3):>8}"
              f"{(mb(l3-prev_l3) if l3>prev_l3 else '—'):>8}")
        prev_l3 = l3

    s = status()
    t = s.get("tiers") or {}
    st = s.get("stat") or {}
    print()
    print(f"  L2 {mb(t.get('l2_used',0))} / {mb(TEST_L2)}    "
          f"L3 {mb(t.get('l3_used',0))} / {mb(TEST_L3)}    "
          f"条目 {s.get('entries')}")
    print(f"  spills={st.get('spills')} evicts={st.get('evicts')} "
          f"skip_cache={st.get('skipped_cache')}")

    # ── 搬家批次 ──
    migs = [e for e in events() if e.get("event") == "migration"
            and e.get("from") == "L2"]
    print()
    print("=" * 90)
    print(f" 搬家明细（L2→L3 共 {len(migs)} 条）")
    print("=" * 90)
    batches, cur = [], []
    for e in migs:
        if cur and (e.get("ts", 0) - cur[-1].get("ts", 0) > 1.5):
            batches.append(cur)
            cur = []
        cur.append(e)
    if cur:
        batches.append(cur)

    multi = 0
    for i, b in enumerate(batches, 1):
        tot = sum(x.get("bytes") or 0 for x in b)
        span = (b[-1].get("ts", 0) - b[0].get("ts", 0)) * 1000
        flag = "★整批" if len(b) > 1 else ""
        if len(b) > 1:
            multi += 1
        print(f"  批次 {i}: {len(b)} 条  {mb(tot):>8}  耗时 {span:.0f} ms  {flag}")
        for x in b:
            print(f"       {x.get('key')}  {mb(x.get('bytes') or 0):>8}  "
                  f"{x.get('ms')} ms")
    print(f"\n  共 {len(batches)} 批，其中 {multi} 批超过 1 条")

    # ── 第二轮：看命中 ──
    print()
    print("=" * 90)
    print(" 第二轮：再问一遍全部文档，看三层命中分布与数据是否完好")
    print("=" * 90)
    counts = {}
    for i, p in enumerate(docs, 1):
        r = ask(p, "再问一次：核心要点是什么？")
        m = r.get("mode")
        counts[m] = counts.get(m, 0) + 1
        ok = (r.get("prompt_n") or 99999) < 300
        flag = "复用" if ok else "重算"
        print(f"  {i:>2} {p.name[:28]:<28}{str(m):<8}"
              f"prompt_n={str(r.get('prompt_n')):<7}{flag}")
    print()
    print(f"  命中分布: {counts}")
    s2 = status()
    st2 = s2.get("stat") or {}
    print(f"  总命中率 {s2.get('hit_rate',0)*100:.0f}%  "
          f"复用 token {st2.get('reused_tokens')}/{st2.get('total_prompt_tokens')}")

    # ── 文件完整性 ──
    print()
    print("=" * 90)
    print(" 文件完整性（逐个读 L3 文件头）")
    print("=" * 90)
    sys.path.insert(0, str(DEV / "proxy"))
    import slotfile                                          # noqa: E402
    l3dir = DEV / "kv_cache" / "L3"
    files = sorted(l3dir.glob("*.bin")) if l3dir.is_dir() else []
    bad = 0
    for f in files:
        ok, msg = slotfile.validate(f)
        bad += 0 if ok else 1
        print(f"  {'✅' if ok else '❌'} {f.name:<22} {msg}")
    print(f"\n  {len(files)} 个文件，坏 {bad} 个")

    print()
    print("=" * 90)
    print(" 还原配置并重启")
    print("=" * 90)
    CFG.write_text(orig, encoding="utf-8")
    sh(f'bash "{DEV}/tools/stop_adv.sh"', 120)
    rc, out = sh(f'bash "{DEV}/tools/start_adv.sh"', 700)
    print("  " + ("✅ 已还原并重启" if "代理入口" in out else "⚠️ 重启异常"))


if __name__ == "__main__":
    main()
