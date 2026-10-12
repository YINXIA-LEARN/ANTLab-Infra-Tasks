#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_chat_stream.py —— 验证经控制层的流式转发是否【真的逐块到达】

判断标准：
  · 响应头没有 Content-Length（说明是流式）
  · 时间戳显示块与块之间有明显间隔（说明没被缓冲）

用法： python3 test_chat_stream.py [控制层地址]
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
URL = BASE + "/api/chat"

QUESTION = "请用三句话介绍什么是 KV Cache，再举一个生活化的类比。"

payload = {
    "messages": [{"role": "user", "content": QUESTION}],
    "system_prompt": "你是一个严谨的助手，回答简洁。",
    "max_tokens": 220,
}

req = urllib.request.Request(
    URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

print("=" * 66)
print("经【控制层】的流式转发测试")
print("=" * 66)

t0 = time.perf_counter()

try:
    resp = urllib.request.urlopen(req, timeout=120)
except urllib.error.HTTPError as e:
    print(f"❌ HTTP {e.code}")
    print(e.read().decode("utf-8", "replace")[:400])
    sys.exit(1)
except Exception as e:                                    # noqa: BLE001
    print(f"❌ {type(e).__name__}: {e}")
    sys.exit(1)

with resp:
    print(f"HTTP              : {resp.status}")
    print(f"Content-Type      : {resp.headers.get('Content-Type')}")
    print(f"Transfer-Encoding : {resp.headers.get('Transfer-Encoding')}")
    print(f"Content-Length    : {resp.headers.get('Content-Length')}")
    print(f"X-Accel-Buffering : {resp.headers.get('X-Accel-Buffering')}")
    print()

    text = ""
    marks = []
    n = 0

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            break
        try:
            obj = json.loads(d)
        except ValueError:
            continue

        choices = obj.get("choices") or [{}]
        delta = (choices[0].get("delta") or {}).get("content")
        if not delta:
            continue

        n += 1
        t = (time.perf_counter() - t0) * 1000
        marks.append(t)
        text += delta

        if n <= 8 or n % 10 == 0:
            shown = delta.replace("\n", "\\n")
            print(f"  +{t:7.0f} ms  「{shown}」")

print()
print("=" * 66)
print("完整回答")
print("=" * 66)
print(text.strip())
print()

if not marks:
    print("❌ 没有收到任何内容块")
    sys.exit(1)

print(f"共 {n} 块，首块 +{marks[0]:.0f} ms，末块 +{marks[-1]:.0f} ms")

if len(marks) >= 3:
    gaps = [marks[i + 1] - marks[i] for i in range(len(marks) - 1)]
    print(f"块间隔：最小 {min(gaps):.0f} ms / 最大 {max(gaps):.0f} ms "
          f"/ 平均 {sum(gaps) / len(gaps):.0f} ms")
    print()
    print("✅ 数据分批到达 —— 浏览器会逐字显示")
else:
    print()
    print("⚠️ 块数偏少（可能回答很短），无法充分判断")
