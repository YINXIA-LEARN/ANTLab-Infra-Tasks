#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_models.py —— 同一批问题分别问两个模型，并排对比

目的：
  · 验证新模型（1.7B）确实能正常工作
  · 为"模型选择依据"提供实际数据（题目要求"根据办公与编程表现选择模型"）

用法： python3 compare_models.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

MODELS = [
    ("Qwen3-0.6B", "http://127.0.0.1:8080"),
    ("Qwen3-1.7B", "http://127.0.0.1:8081"),
]

SYS = "你为小型工作室处理办公文档和编程任务。请按任务要求组织输出。"

# 题目里的任务类型各取一个代表
PROBES = [
    ("条款引用（考准确性）",
     "一份制度规定：设备借用期限为 14 天，可续借 7 天；已关闭工单须在 3 个工作日内回访；"
     "每日备份保留 30 天。请回答：设备最长可借用多少天？回访期限是几个工作日？"
     "按问题逐条简短回答，并注明依据的条款原文。"),

    ("代码问题定位",
     "下面这段 Python 有什么问题？\n"
     "```\n"
     "def avg(values):\n"
     "    return sum(values) / len(values)\n"
     "```\n"
     "指出触发条件、当前行为、预期行为，并给出最小复现输入。"),

    ("跨信息整合",
     "资料：\n"
     "- 3月1日会议：决定先做搜索功能，4月上线。\n"
     "- 3月15日会议：搜索功能推迟到5月，先做权限。\n"
     "- 3月20日会议：权限功能完成，搜索功能仍按3月15日的决定。\n"
     "问：搜索功能何时上线？依据哪次会议？"),
]


def ask(base, question, max_tokens=400, timeout=180):
    """返回 (答案文本, 统计)"""
    payload = {
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": question},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.read().decode('utf-8','replace')[:200]}", {}
    except Exception as e:                                    # noqa: BLE001
        return f"[{type(e).__name__}] {e}", {}

    dt = time.perf_counter() - t0
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text and msg.get("reasoning_content"):
        text = "[思考模式未关]\n" + msg["reasoning_content"].strip()

    u = d.get("usage") or {}
    t = d.get("timings") or {}
    return text, {
        "elapsed": dt,
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "decode_tps": t.get("predicted_per_second"),
        "finish": (d.get("choices") or [{}])[0].get("finish_reason"),
    }


def main():
    # 先确认两个服务都在
    for name, base in MODELS:
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                ok = b'"ok"' in r.read()
        except Exception:                                     # noqa: BLE001
            ok = False
        if not ok:
            print(f"❌ {name} 不可用（{base}）")
            print("   请确认两个 llama-server 都在运行")
            sys.exit(1)
        print(f"✅ {name} 可用")

    print()
    for i, (title, question) in enumerate(PROBES, 1):
        print("=" * 78)
        print(f"【测试 {i}】{title}")
        print("=" * 78)
        print(f"问题：{question[:90]}...")
        print()

        for name, base in MODELS:
            text, st = ask(base, question)
            print(f"── {name} ──")
            if st:
                print(f"   [{st['completion_tokens']} tok, {st['elapsed']:.2f}s, "
                      f"{st.get('decode_tps') or 0:.0f} tok/s, finish={st['finish']}]")
            for line in text.splitlines():
                print("   " + line)
            print()
        print()


if __name__ == "__main__":
    main()
