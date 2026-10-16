#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_run_labels.py —— 按【真实配置】重命名运行，并同步所有内部引用。

═══════════════════════════════════════════════════════════════════════════
 为什么必须改
═══════════════════════════════════════════════════════════════════════════
 这批运行的 label 是随手起的，与真实配置大面积错配。实测：

     kv_baseline_0.6-223609   label 说 baseline，实际是 KV q8_0（那是"优化"）
     kv_opt_0.6-224117        label 说 opt，      实际是 KV f16 （那是"基线"）
     ctx_oopt_0.6-223012      "oopt" 是拼写错误
     kv_opt_1.7-232040        label 说 1.7，      实际跑的是 0.6B

 后果：按 label 配对会把【f16 vs q8_0】这对搞反，结论直接是错的。

═══════════════════════════════════════════════════════════════════════════
 新命名规则（由真实配置推导，不手填）
═══════════════════════════════════════════════════════════════════════════
     <系列>-<模型>-<时间戳>
   系列：
     ctx16k-f16    上下文 16384 + KV f16        —— 上下文实验的基线
     ctx32k-f16    上下文 32768 + KV f16        —— 上下文实验的优化
     ctx16k-q8     上下文 16384 + KV q8_0       —— KV 精度实验的优化
     （模型名统一用 0.6b / 1.7b，从 model_file 推导）

 ⚠️ 同一配置可能有多次运行（重复测量），加 -r1 / -r2 区分。

⚠️ 重命名要同步的东西（漏一个就有一个地方指向不存在的目录）：
     · 目录名
     · 目录内 meta.json / summary.json / run-state.json 的 run_id
     · results/runs.json 登记表的 run_id
     · 各运行 logs/bench-<label>-<时间戳>.log 里的文字（只改注释，不改文件名）
"""
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

DEV = Path(__file__).resolve().parents[2]
RESULTS = DEV / "results"


def derive(cfg):
    """由真实配置推导系列名"""
    ctx = cfg.get("server_ctx")
    kvk = (cfg.get("kv_type_k") or "").lower()
    model = (cfg.get("model_file") or "").replace("Qwen3-", "").replace(
        "-Q8_0.gguf", "").replace(".", "").lower()
    ctxs = {16384: "ctx16k", 32768: "ctx32k"}.get(ctx, "ctx%s" % ctx)
    kvs = "f16" if kvk.startswith("f16") else kvk.replace("_", "")
    return "%s-%s-%s" % (ctxs, kvs, model)


def main():
    apply = "--apply" in sys.argv
    dirs = sorted([d for d in RESULTS.iterdir() if d.is_dir()],
                  key=lambda d: d.name)
    info = []
    for d in dirs:
        f = d / "run-config.json"
        if not f.is_file():
            continue
        cfg = json.loads(f.read_text(encoding="utf-8"))
        info.append((d, cfg, derive(cfg)))

    # 同系列多次运行 → 加 -r1/-r2
    #
    # ⚠️ run_id 必须【唯一】，所以除了 -rN 还要保留原时间戳：
    #    系列里可能有多次"重复测量"，比如
    #        ctx16k-f16-06b-r1-20261016-222516
    #        ctx16k-f16-06b-r2-20261016-224117
    #    只写 -rN 而不带时间戳的话，两次运行的 id 会不可区分
    #    （而且 r1/r2 谁先谁后也看不出来）。
    seen = defaultdict(int)
    plan = []
    for d, cfg, base in info:
        seen[base] += 1
        # 原目录名形如 <label>-YYYYMMDD-HHMMSS，取日期时间部分
        m = re.search(r"(\d{8}-\d{6})$", d.name)
        stamp = m.group(1) if m else d.name
        newid = "%s-r%d-%s" % (base, seen[base], stamp)
        plan.append((d, cfg, base, newid))

    print("=" * 100)
    print(" 重命名计划（由真实配置推导）")
    print("=" * 100)
    print("  %-36s → %-30s %s" % ("旧", "新", "真实配置"))
    for d, cfg, base, newid in plan:
        print("  %-36s → %-30s ctx=%s KV=%s/%s %s" % (
            d.name, newid, cfg.get("server_ctx"), cfg.get("kv_type_k"),
            cfg.get("kv_type_v"),
            (cfg.get("model_file") or "").replace("Qwen3-", "").replace(
                "-Q8_0.gguf", "")))

    if not apply:
        print()
        print("  （预演模式。加 --apply 真正执行）")
        return

    print()
    print("=" * 100)
    print(" 执行")
    print("=" * 100)

    ren = {}
    for d, cfg, base, newid in plan:
        # ① 目录
        nd = RESULTS / newid
        if nd.exists():
            print("  ⚠️ %s 已存在，跳过" % newid)
            continue
        shutil.move(str(d), str(nd))
        ren[d.name] = newid
        # ② 目录内引用 run_id 的字段
        for fn in ("meta.json", "summary.json", "run-state.json"):
            p = nd / fn
            if not p.is_file():
                continue
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            changed = False
            for k in ("run_id",):
                if j.get(k) == d.name:
                    j[k] = newid
                    changed = True
            # run-config.json 里的 label 也顺手改成新系列名
            if fn == "meta.json" and isinstance(j.get("run_config"), dict):
                pass
            if changed:
                p.write_text(json.dumps(j, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        # ③ run-config.json 的 label
        p = nd / "run-config.json"
        if p.is_file():
            j = json.loads(p.read_text(encoding="utf-8"))
            j["label"] = newid.rsplit("-", 1)[0]
            j["_label_说明"] = ("本 label 由真实配置推导（原 label %r 与配置不符，"
                                "已修正）" % j.get("label"))
            p.write_text(json.dumps(j, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        print("  ✅ %-36s → %s" % (d.name, newid))

    # ④ 登记表
    p = RESULTS / "runs.json"
    if p.is_file():
        reg = json.loads(p.read_text(encoding="utf-8"))
        n = 0
        for r in reg:
            if r.get("run_id") in ren:
                r["run_id"] = ren[r["run_id"]]
                n += 1
        p.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print("  ✅ 登记表更新 %d 条" % n)

    print()
    print("  最终：")
    for d in sorted([x for x in RESULTS.iterdir() if x.is_dir()],
                    key=lambda x: x.name):
        print("    %s" % d.name)


if __name__ == "__main__":
    main()
