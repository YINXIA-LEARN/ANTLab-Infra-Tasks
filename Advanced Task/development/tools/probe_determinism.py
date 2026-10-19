#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_determinism.py —— 同一个请求，输出到底会不会变？

背景：端到端测试发现"走缓存"和"走全量计算"两条路径的同题回答不一样。
      需要查清是【缓存改变了输出】，还是【模型本身就不是逐字确定的】。

做法：
  A. 连续问 6 次（第 1 次 miss，之后都是命中）→ 看命中之间是否一致
  B. 清掉这份文档的缓存，再问一次（强制全量）→ 和命中的答案比
  C. 把同一个提示词直接打给模型服务 6 次（不经过代理）→ 看纯模型的不确定性
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
PROXY = "http://127.0.0.1:8091"
BASE = "http://127.0.0.1:8092"
DOC = "small/eta-客户资料分级.md"
Q = "资料分几级？"


def post(url, body, timeout=900):
    r = urllib.request.Request(url, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read())


def ask(base=PROXY):
    text = (DEV / "corpus" / DOC).read_text(encoding="utf-8")
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁准确。"},
        {"role": "user", "content": text},
        {"role": "user", "content": Q}],
        "max_tokens": 48, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    d = post(base + "/v1/chat/completions", body)
    px = d.get("_proxy") or {}
    t = d.get("timings") or {}
    ans = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return {"mode": px.get("mode"), "prompt_n": t.get("prompt_n"),
            "ans": ans.strip()}


def clear_doc_cache():
    """把这个文档的索引条目和文件都删掉，强制下次全量"""
    idx_p = DEV / "kv_index.json"
    idx = json.loads(idx_p.read_text(encoding="utf-8"))
    text = (DEV / "corpus" / DOC).read_text(encoding="utf-8")
    # 用代理同样的规则算键（前缀 + 配置指纹）
    sys.path.insert(0, str(DEV / "proxy"))
    import proxy as P
    obj = object.__new__(P.Proxy)
    cfg = json.loads((DEV / "configs/cache.json").read_text(encoding="utf-8"))
    obj.fp = cfg["server_fingerprint"]
    prefix = ("你是澄禾工作室的资料助手，回答要简洁准确。" + "\n\n" + text)
    key = obj.cache_key(prefix)
    e = idx["entries"].get(key)
    if not e:
        return None, "索引里没有这个键"
    for p in (Path("/dev/shm/antlab_kv/L2") / f"{key}.bin",
              DEV / "kv_cache/L3" / f"{key}.bin"):
        if p.is_file():
            p.unlink()
    del idx["entries"][key]
    idx_p.write_text(json.dumps(idx, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return key, "已清除"


def main():
    print("=" * 78)
    print(" A. 连续 6 次（经过代理）")
    print("=" * 78)
    a = []
    for i in range(6):
        r = ask()
        a.append(r)
        print(f"  #{i+1} mode={str(r['mode']):<7} prompt_n={str(r['prompt_n']):<5} "
              f"答案长度={len(r['ans']):>3}  {r['ans'][:52]!r}")
        time.sleep(0.2)

    hit_ans = [x["ans"] for x in a if x["mode"] in ("L1", "L2", "L3")]
    print()
    print(f"  命中次数 {len(hit_ans)}；命中的答案有 {len(set(hit_ans))} 种")
    if len(set(hit_ans)) == 1:
        print("  → 命中之间【完全一致】")
    else:
        print("  → 命中之间【就不一致】！说明模型本身不确定，不是缓存的问题")

    print()
    print("=" * 78)
    print(" B. 清掉这份缓存，强制全量，再比")
    print("=" * 78)
    key, msg = clear_doc_cache()
    print(f"  {msg}（key={key}）")
    time.sleep(0.5)
    r = ask()
    print(f"  mode={r['mode']} prompt_n={r['prompt_n']} "
          f"答案长度={len(r['ans'])}  {r['ans'][:52]!r}")
    base_ans = a[0]["ans"]
    if r["ans"] == base_ans:
        print("  → 与最初那次（全量）完全一致")
    else:
        print("  → 与最初那次（全量）不同")

    hit_first = hit_ans[0] if hit_ans else None
    if hit_first:
        print(f"  命中答案 vs 全量答案：{'一致' if hit_first == r['ans'] else '不同'}")

    print()
    print("=" * 78)
    print(" C. 绕过代理，直接打模型服务 6 次（同提示词，temp=0）")
    print("=" * 78)
    c = []
    for i in range(6):
        r = ask(base=BASE)
        c.append(r["ans"])
        print(f"  #{i+1} prompt_n={r['prompt_n']:<5} 长度={len(r['ans']):>3}  "
              f"{r['ans'][:52]!r}")
        time.sleep(0.2)
    print()
    print(f"  直连 6 次，答案有 {len(set(c))} 种")

    print()
    print("=" * 78)
    print(" 结论")
    print("=" * 78)
    if len(set(c)) > 1:
        print("  ❗ 直连就已经不一致 → 模型在 temp=0 下也不是逐字确定的")
        print("     （批量大小随缓存命中情况变化，浮点归约顺序不同）")
        print("     → 「有缓存与无缓存输出逐字相同」这个断言本身不成立，")
        print("       应改为「关键事实一致」")
    else:
        print("  ✅ 直连 6 次完全一致 → 模型是确定的")
        if len(set(hit_ans)) > 1:
            print("     ❗ 但经过代理命中时不一致 → 缓存影响了输出，需要排查")
        else:
            print("     ✅ 命中路径也一致")


if __name__ == "__main__":
    main()
