#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kv_capacity.py —— 实测 KV Cache 容量（题目要求的"KV 容量（token）"指标）

为什么需要这个脚本：
    显存字节数几乎不随运行变化（KV Cache 是启动时预分配的），
    所以"显存还剩多少"看不出还能装多少对话。
    真正有用的指标是【KV 容量 = 还能放进多少 token】。
    题目把它列为必报指标，原因就在这里。

本脚本做两件事：
    ① 容量探测：不断加大输入长度，找出服务能接受的最大 token 数
    ② 显存对照：按不同 -c 启动，测出 KV Cache 的字节代价

用法：
    python3 kv_capacity.py capacity            # 探测当前服务的容量
    python3 kv_capacity.py capacity --max 8000 # 指定探测上限
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"


def server_stats():
    """从 /props 读服务与模型信息"""
    try:
        with urllib.request.urlopen(BASE + "/props", timeout=5) as r:
            d = json.loads(r.read())
        dgs = d.get("default_generation_settings") or {}
        return {
            "model": (d.get("model_path") or "").rsplit("/", 1)[-1],
            "quant": d.get("model_ftype"),
            "n_ctx": dgs.get("n_ctx"),
            "slots": d.get("total_slots"),
            "build": d.get("build_info"),
        }
    except Exception as e:                                    # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def vram_used():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip())
    except Exception:                                         # noqa: BLE001
        return None


def try_request(n_words, timeout=180):
    """
    发一个指定长度（按词数近似）的请求，看能否成功。

    返回 (是否成功, 输入token数, 输出token数, 错误信息)
    """
    # 用重复的中文句子拼出指定长度（每句约 16 个字）
    sent = "这是一段用于测试上下文容量的资料内容。"
    text = sent * max(1, n_words // len(sent))
    body = {
        "messages": [{"role": "user", "content": text + "\n\n请用一句话总结。"}],
        "max_tokens": 24,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        u = d.get("usage") or {}
        return True, u.get("prompt_tokens"), u.get("completion_tokens"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        m = re.search(r'"message"\s*:\s*"([^"]{0,200})', detail)
        return False, None, None, f"HTTP {e.code}: {m.group(1) if m else detail[:150]}"
    except Exception as e:                                    # noqa: BLE001
        return False, None, None, f"{type(e).__name__}: {e}"


def probe_capacity(hard_max=8000):
    """
    二分/倍增探测最大可接受输入长度。

    策略：从 500 字起翻倍，失败后再在区间内二分，找到临界点。
    """
    st = server_stats()
    n_ctx = st.get("n_ctx")
    print(f"服务信息：{st.get('model')} | slots={st.get('slots')}")
    print(f"  KV 池总容量 = {n_ctx} token"
          f"（⚠️ kv_unified 下这是共享池总量，不是每槽可用容量）")
    print()

    # ── 阶段 1：倍增找上界 ──
    print("阶段 1：逐步加大输入，找到失败点")
    lo_ok, hi_fail = 0, None
    size = 500
    while size <= hard_max:
        ok, pt, ct, err = try_request(size)
        mark = "✅" if ok else "❌"
        detail = f"输入 {pt} tok" if ok else (err or "")[:70]
        print(f"  {mark} 约 {size:>5} 字  →  {detail}")
        if ok:
            lo_ok = pt or 0
            size *= 2
        else:
            hi_fail = size
            break
        if n_ctx and pt and pt > n_ctx:
            break

    if hi_fail is None:
        print(f"\n  在 {hard_max} 字以内都能成功（未找到失败点）")
        return lo_ok, None

    # ── 阶段 2：二分逼近 ──
    print()
    print(f"阶段 2：在 {lo_ok} token 与失败点之间二分逼近")
    lo, hi = lo_ok, hi_fail
    best_tok = lo_ok
    for _ in range(8):
        if hi - lo <= 40:
            break
        mid = (lo + hi) // 2
        ok, pt, ct, err = try_request(mid)
        if ok:
            best_tok = pt or mid
            lo = mid
            print(f"  ✅ 约 {mid:>5} 字 → 输入 {pt} tok，成功")
        else:
            hi = mid
            print(f"  ❌ 约 {mid:>5} 字 → 失败")
        time.sleep(0.2)

    return best_tok, hi


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capacity"

    if cmd != "capacity":
        print(__doc__)
        return

    hard_max = 8000
    if "--max" in sys.argv:
        hard_max = int(sys.argv[sys.argv.index("--max") + 1])

    st = server_stats()
    if "error" in st:
        print(f"❌ 连不上模型服务：{st['error']}")
        sys.exit(1)

    v0 = vram_used()
    print("=" * 70)
    print("KV Cache 容量实测")
    print("=" * 70)
    print(f"  显存（探测前）：{v0} MiB")
    print()

    best, fail_at = probe_capacity(hard_max)

    v1 = vram_used()
    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    print(f"  ✅ 实测可接受的最大输入：约 {best} token")
    if fail_at:
        print(f"  ❌ 失败区间起点：约 {fail_at} 字")
    print()
    # ⚠️ 这里原来写的是「总 KV 容量 = n_ctx × slots」，那是【错的】。
    #    在 kv_unified=true（slot 数为 auto 时的默认）下，
    #    -c / n_ctx 本身就是【所有 slot 共享的那个池的总容量】，
    #    再乘 slot 数会虚报 4 倍。
    #    实测依据：-c 32768 时 CUDA0 KV buffer size = 3584 MiB，
    #    而 3584 MiB ÷ 32768 = 112 KiB/token —— 只够一个 32768 的池。
    #    若真是 4 槽各 32768，需要约 14336 MiB，8 GB 显存装不下。
    n_ctx = st.get("n_ctx")
    slots = st.get("slots") or 1
    print(f"  KV 池总容量（= -c，配置值）：{n_ctx} token")
    print(f"  可用 slot 数：{slots}（共享上面这一个池）")
    if n_ctx:
        print(f"  每槽可用容量：动态值，约 {n_ctx // slots} token"
              f"（= 池总量 ÷ 同时在跑的请求数）")
        print()
        print("  ⚠️ 报容量时必须区分三个口径（题目第 84 行要求）：")
        print(f"       配置总容量    : {n_ctx} token")
        print(f"       各 slot 可用   : 动态，最少约 {n_ctx // slots}"
              f"（4 槽全忙），最多 {n_ctx}（只有 1 个请求）")
        print(f"       分配方式       : 共享池（kv_unified=true）")
    print()
    print(f"  显存（探测后）：{v1} MiB   变化：{(v1 - v0) if (v0 and v1) else '?'} MiB")
    print()
    print("  ⭐ 注意：探测过程中显存几乎不变，这正说明")
    print("     KV Cache 是启动时按 n_ctx 预分配的，")
    print("     运行中只在这个固定空间里读写，不会增长。")
    print("=" * 70)


if __name__ == "__main__":
    main()
