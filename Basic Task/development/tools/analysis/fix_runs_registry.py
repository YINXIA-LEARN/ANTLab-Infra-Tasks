#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_runs_registry.py —— 把 results/runs.json 里的派生字段对齐真实数据。

═══════════════════════════════════════════════════════════════════════════
 为什么要修（实测发现的三个错）
═══════════════════════════════════════════════════════════════════════════
 runs.json 是【派生索引】—— 每次跑测试由 run_bench.py 的
 update_runs_registry() 追加一条。它记的是"当时"的情况。
 后来目录按真实配置重命名了，但只有 run_id 被同步，其余字段留着旧值：

   label              "ctx_baseline_0.6"          → 与目录名不符
   dir                "ctx_baseline_0.6-222516…"   → 指向【不存在的目录】
   server_args.ctx    32768                        → ★ 真实跑的是 16384，错的

 ⚠️ 其中 server_args.ctx 最危险：它是【服务启动参数的注册表默认值】，
    不是【本次实际用的值】。看 runs.json 会以为那一轮跑的是 32K，
    而实际跑的是 16K —— 按它配对，上下文实验的结论会完全反过来。

 修法：一律从运行目录里的权威数据重算
   · run-config.json  → server_ctx / kv_type_k / kv_type_v / max_tokens …
   · meta.json        → server_config.server_flags（进程命令行读的，最权威）
   · 目录名           → label / dir

 ⚠️ 修完仍要记住：runs.json 是派生索引，权威数据永远是运行目录本身。
    这一点已写进 results/README.md。
"""
import json
import re
from pathlib import Path

D = Path(__file__).resolve().parents[2]
R = D / "results"
REG = R / "runs.json"


def main():
    reg = json.loads(REG.read_text(encoding="utf-8"))
    print("=" * 96)
    print(" 对齐 runs.json（%d 条）" % len(reg))
    print("=" * 96)

    fixed = 0
    out = []
    for r in reg:
        rid = r.get("run_id")
        d = R / rid
        if not d.is_dir():
            print("  ⚠️ %s 目录不存在，保留原样（可能是被删掉的运行）" % rid)
            out.append(r)
            continue
        cfg = json.loads((d / "run-config.json").read_text(encoding="utf-8"))
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        sc = meta.get("server_config") or {}
        flags = sc.get("server_flags") or {}
        if isinstance(flags, dict):
            ctx_real = flags.get("ctx")
            ctk, ctv = flags.get("cache_type_k"), flags.get("cache_type_v")
            cache_ram = flags.get("cache_ram")
        else:
            ctx_real, ctk, ctv, cache_ram = None, None, None, None

        before = dict(r)
        # label：用运行编号去掉时间戳部分
        m = re.match(r"^(.*)-r\d-\d{8}-\d{6}$", rid)
        r["label"] = m.group(1) if m else rid
        r["dir"] = rid
        # ★ 关键：server_args.ctx 改成【服务实际跑的 ctx】
        r["server_args"] = {
            "ngl": (sc.get("server_args") or {}).get("ngl"),
            "ctx": int(ctx_real) if ctx_real else None,
            "_说明": ("本字段原记的是注册表默认值，2026-10-16 起改为"
                      "【本次服务实际启动时的值】（从进程命令行读）；"
                      "权威来源是同目录 meta.json 的 server_config.server_flags"),
        }
        r["server_ctx"] = int(ctx_real) if ctx_real else None
        r["kv_type_k"] = ctk
        r["kv_type_v"] = ctv
        r["cache_ram"] = cache_ram
        r["_derived_index_note"] = (
            "本文件是【派生索引】，由 run_bench.py 在每次运行结束时追加。"
            "权威数据是运行目录本身（run-config.json / meta.json / summary.json）。"
            "字段与目录不一致时，一律以目录里的文件为准。")

        if before != r:
            fixed += 1
            print("  ✅ %-42s ctx %s → %s" % (
                rid, (before.get("server_args") or {}).get("ctx"),
                r["server_args"]["ctx"]))

        # ⚠️⚠️ 这一行是【必须】的，而且我第一版漏了它。
        #
        #   漏掉的后果特别阴：循环里改的是 r、日志照样打印 "✅ 修正 N 条"，
        #   但 out 始终是空列表 —— 于是把 runs.json 写成 `[]`，
        #   **把 9 条登记记录整个抹掉，而输出看起来完全成功**。
        #   要不是落盘后顺手 import 一下看条数，这个错会一直留着。
        #
        #   教训：凡是"读一份 → 逐条改 → 写回"的脚本，
        #         写完必须【读回来数一遍】，不能只看日志。
        out.append(r)

    # ⚠️ 写回前先自检：修完的条数必须和读进来的一致。
    #    这条保护就是为上面那个漏 append 的 bug 加的 —— 有它在，
    #    那种"日志说成功、实际写空"的情况会当场被拦住。
    if len(out) != len(reg):
        print()
        print("  ❌ 修完 %d 条 ≠ 读入 %d 条 —— 拒绝写回（避免把登记表写坏）"
              % (len(out), len(reg)))
        raise SystemExit(1)

    REG.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print()
    print("  修正 %d 条" % fixed)

    # ⚠️ 写完【读回来数一遍】—— 不看日志，看文件。
    back = json.loads(REG.read_text(encoding="utf-8"))
    print("  ✅ 写回后读回校验：%d 条" % len(back))
    if len(back) != len(reg):
        print("  ❌ 写回后条数不对！")
        raise SystemExit(1)

    # 复核
    print()
    print("=" * 96)
    print(" 复核：拿目录名与 runs.json 对齐")
    print("=" * 96)
    dirs = sorted([p.name for p in R.iterdir() if p.is_dir()])
    reg_ids = sorted([x.get("run_id") for x in out])
    print("  目录 %d 个 / 登记表 %d 条" % (len(dirs), len(reg_ids)))
    only_dir = set(dirs) - set(reg_ids)
    only_reg = set(reg_ids) - set(dirs)
    if only_dir:
        print("  ⚠️ 只在目录里有：%s" % sorted(only_dir))
    if only_reg:
        print("  ⚠️ 只在登记表里有：%s" % sorted(only_reg))
    if not only_dir and not only_reg:
        print("  ✅ 完全一致")
    print()
    for x in out:
        sa = x.get("server_args") or {}
        print("  %-42s label=%-20s ctx=%-6s KV=%s/%s  dir=%s" % (
            x.get("run_id"), x.get("label"), sa.get("ctx"),
            x.get("kv_type_k"), x.get("kv_type_v"), x.get("dir")))


if __name__ == "__main__":
    main()
