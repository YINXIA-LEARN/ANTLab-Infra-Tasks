#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_determinism2.py —— 决定性对照：真全量 vs 缓存命中，输出到底差在哪

第一版的问题：清掉缓存文件后再问，文档其实还在【显存槽】里，所以并没有
真正全量重算。要做真全量，必须让这份文档同时不在显存、不在 L2、不在 L3。

本脚本做三组：
  组 1：真全量（清文件 + 重启模型服务，槽也空了）
  组 2：从 L2 恢复（重新算一次存进 L2，然后重启模型服务清空槽，再问）
  组 3：同槽连续命中（prompt_n 应该只有 1~2）

比对三组的答案与 prompt_n。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
PROXY = "http://127.0.0.1:8091"
L2 = Path("/dev/shm/antlab_kv/L2")
L3 = DEV / "kv_cache/L3"
DOC = "small/eta-客户资料分级.md"
Q = "资料分几级？"


def post(url, body, timeout=900):
    r = urllib.request.Request(url, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read())


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as f:
        return json.loads(f.read())


def ask():
    text = (DEV / "corpus" / DOC).read_text(encoding="utf-8")
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁准确。"},
        {"role": "user", "content": text},
        {"role": "user", "content": Q}],
        "max_tokens": 48, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    d = post(PROXY + "/v1/chat/completions", body)
    px = d.get("_proxy") or {}
    t = d.get("timings") or {}
    ans = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return {"mode": px.get("mode"), "prompt_n": t.get("prompt_n"),
            "ans": ans.strip().replace("\n", " ")[:70]}


def doc_key():
    sys.path.insert(0, str(DEV / "proxy"))
    import proxy as P
    obj = object.__new__(P.Proxy)
    cfg = json.loads((DEV / "configs/cache.json").read_text(encoding="utf-8"))
    obj.fp = cfg["server_fingerprint"]
    text = (DEV / "corpus" / DOC).read_text(encoding="utf-8")
    return obj.cache_key("你是澄禾工作室的资料助手，回答要简洁准确。\n\n" + text)


def drop_files(key):
    n = 0
    for p in (L2 / f"{key}.bin", L3 / f"{key}.bin"):
        if p.is_file():
            p.unlink(); n += 1
    return n


def drop_index(key):
    p = DEV / "kv_index.json"
    if not p.is_file():
        return
    d = json.loads(p.read_text(encoding="utf-8"))
    if key in (d.get("entries") or {}):
        del d["entries"][key]
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def restart_proxy():
    subprocess.run(["bash", "-lc",
                    "pkill -f 'proxy/proxy.py.*--dev-dir'"], capture_output=True)
    time.sleep(1.5)
    subprocess.run(["bash", "-lc",
                    f'setsid --fork python3 -u "{DEV}/proxy/proxy.py" '
                    f'--config "{DEV}/configs/cache.json" --dev-dir "{DEV}" '
                    f'> "{DEV}/logs/proxy.log" 2>&1 < /dev/null'],
                   capture_output=True)
    for _ in range(80):
        try:
            if get(PROXY + "/health", 2).get("ok"):
                return True
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def flush_slots():
    """把所有 slot 清空，保证下一次是"没有显存 KV"的状态"""
    for s in range(4):
        r = urllib.request.Request(
            f"http://127.0.0.1:8092/slots/{s}?action=erase",
            data=b'{"_":1}', method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(r, timeout=20).close()
        except Exception:                                     # noqa: BLE001
            pass
    time.sleep(0.4)


def main():
    key = doc_key()
    print("=" * 80)
    print(f" 文档 {DOC}   问题 {Q!r}")
    print(f" 缓存键 {key}")
    print("=" * 80)

    # ── 组 1：真全量 ──
    print()
    print("组 1：真全量（缓存文件删掉 + 重启代理 + 清空所有 slot）")
    n = drop_files(key)
    drop_index(key)
    restart_proxy()
    flush_slots()
    r1 = ask()
    print(f"  mode={r1['mode']}  prompt_n={r1['prompt_n']}  {r1['ans']!r}")

    # ── 组 2：从 L2 恢复 ──
    print()
    print("组 2：从 L2 恢复（组 1 已经把它存进 L2 了；清空 slot，保留文件）")
    flush_slots()
    r2 = ask()
    print(f"  mode={r2['mode']}  prompt_n={r2['prompt_n']}  {r2['ans']!r}")

    # ── 组 3：同槽连续命中 ──
    print()
    print("组 3：同槽连续命中（不清任何东西）")
    r3 = ask()
    print(f"  mode={r3['mode']}  prompt_n={r3['prompt_n']}  {r3['ans']!r}")
    r4 = ask()
    print(f"  mode={r4['mode']}  prompt_n={r4['prompt_n']}  {r4['ans']!r}")

    # ── 组 4：再来一次真全量，看全量本身稳不稳 ──
    print()
    print("组 4：再做一次真全量（验证全量路径自身的稳定性）")
    drop_files(key)
    drop_index(key)
    restart_proxy()
    flush_slots()
    r5 = ask()
    print(f"  mode={r5['mode']}  prompt_n={r5['prompt_n']}  {r5['ans']!r}")

    # ── 结论 ──
    print()
    print("=" * 80)
    print(" 结论")
    print("=" * 80)
    print(f"  真全量 #1：{r1['ans']!r}")
    print(f"  真全量 #2：{r5['ans']!r}")
    print(f"  从L2恢复 ：{r2['ans']!r}")
    print(f"  同槽命中 ：{r3['ans']!r}")
    print()
    full_stable = r1["ans"] == r5["ans"]
    hit_same = r2["ans"] == r3["ans"] == r4["ans"]
    print(f"  全量自身稳定      : {'是' if full_stable else '否 ← 模型本身不确定'}")
    print(f"  命中之间一致      : {'是' if hit_same else '否'}")
    print(f"  全量 vs 命中一致  : "
          f"{'是' if r1['ans'] == r2['ans'] else '否'}")
    print()
    if not full_stable:
        print("  → 模型在 temp=0 下也不是逐字确定的（批量大小不同导致浮点路径不同）")
        print("     所以「有缓存与无缓存输出逐字相同」不能作为断言；")
        print("     应改为断言「关键事实一致」。")
    elif r1["ans"] != r2["ans"]:
        print("  → 全量自身是稳的，但【命中路径】和【全量路径】输出不同。")
        print("     原因：n_past 不同 → 批量组成不同 → 浮点归约顺序不同。")
        print("     这是 llama.cpp 前缀复用的固有性质，不是本项目的 bug。")
    else:
        print("  → 三条路径输出完全一致，最理想。")


if __name__ == "__main__":
    main()
