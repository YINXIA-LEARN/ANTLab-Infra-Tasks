#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_e2e.py —— 端到端：真的打代理，验证四条路径与输出正确性

需要服务在线（llama-server:8092 + 代理:8091），不在线则整段跳过。

断言什么
  1. 首次请求是 miss，第二次同一文档是命中（L1 或 L2）
  2. 命中的 prompt_n 远小于全量（真正的复用，不是"看起来快"）
  3. v2 的请求**不能**返回 v1 的旧事实（缓存不能污染输出）
  4. 回答了同一个问题时，有缓存与无缓存的输出一致（temperature=0）
  5. 请求取消（客户端提前断开）不会让代理崩溃
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

PROXY = "http://127.0.0.1:8091"
BASE = "http://127.0.0.1:8092"
DEV = T.DEV


def post(url, body, timeout=900):
    r = urllib.request.Request(url, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read()), (time.time() - t0) * 1000


def ask(doc_rel, q, base=PROXY, max_tokens=48):
    text = (DEV / "corpus" / doc_rel).read_text(encoding="utf-8")
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁准确。"},
        {"role": "user", "content": text},
        {"role": "user", "content": q}],
        "max_tokens": max_tokens, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    d, wall = post(base + "/v1/chat/completions", body)
    px = d.get("_proxy") or {}
    return {
        "mode": px.get("mode"), "slot": px.get("slot"),
        "prompt_n": (d.get("timings") or {}).get("prompt_n"),
        "prompt_total": (d.get("usage") or {}).get("prompt_tokens"),
        "reused": px.get("reused_tokens"),
        "prefill_ms": (d.get("timings") or {}).get("prompt_ms"),
        "answer": ((d.get("choices") or [{}])[0].get("message") or {})
        .get("content", ""),
        "wall_ms": wall,
    }


def alive(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as f:
            return f.status == 200
    except Exception:                                         # noqa: BLE001
        return False


def t_hit_paths():
    T.case("端到端：miss → 命中（用一份临时新文档，保证是真正的首次）")
    # 用临时文档，避免受之前跑过什么影响
    import hashlib
    tag = hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]
    tmp = DEV / "corpus" / "_tmp_e2e.md"
    body = [f"# 临时测试资料 {tag}", "",
            "## 1. 目的", "", "这份资料只用于端到端测试，内容随每次运行变化。", ""]
    for i in range(2, 40):
        body.append(f"## {i}. 条款 {tag}-{i:02d}")
        body.append("")
        body.append(f"第 {i} 条规定：临时测试事项需在 {i} 个工作日内登记，"
                    f"由第 {i} 位责任人确认，金额上限 {i * 100} 元。")
        body.append("")
    tmp.write_text("\n".join(body), encoding="utf-8")
    try:
        doc = "_tmp_e2e.md"
        r1 = ask(doc, "这份资料的用途是什么？")
        T.eq(r1["mode"], "miss", "第一次是 miss")
        T.check(r1["prompt_total"] > 500,
                f"确实全量算了 {r1['prompt_total']} token")

        r2 = ask(doc, "条款 05 规定了什么？")
        T.check(r2["mode"] in ("L1", "L2", "L3"),
                f"第二次命中（模式 {r2['mode']}）")
        T.check(r2["prompt_n"] is not None and r2["prompt_n"] < 100,
                f"只算了 {r2['prompt_n']} 个新 token（真正的复用）")
        T.check(r2["prefill_ms"] < max(1.0, r1["prefill_ms"] / 2),
                f"prefill 从 {r1['prefill_ms']:.0f} ms 降到 "
                f"{r2['prefill_ms']:.0f} ms")
    finally:
        tmp.unlink(missing_ok=True)


def t_version_facts():
    T.case("版本切换：v2 必须用新事实，不能被 v1 缓存污染")
    q = "设备借用期限是几天？"
    a1 = ask("versions/知识库-v1.md", q)
    T.check("7" in a1["answer"], f"v1 的回答里有 7 天：{a1['answer'][:60]}")

    a2 = ask("versions/知识库-v2.md", q)
    T.check("14" in a2["answer"], f"v2 的回答里有 14 天：{a2['answer'][:60]}")
    T.check("7 天" not in a2["answer"].replace("14 天", ""),
            "v2 的回答里没有残留 v1 的 7 天")
    T.check(a2["mode"] in ("prefix", "miss", "L1", "L2"),
            f"v2 走了 {a2['mode']} 路径")

    # v2 反复问，答案必须一直是新的
    a3 = ask("versions/知识库-v2.md", q)
    T.check("14" in a3["answer"], f"v2 第二次仍然是 14 天：{a3['answer'][:60]}")


def t_output_consistency():
    T.case("输出一致性：有缓存 vs 无缓存（temperature=0）")
    doc = "small/eta-客户资料分级.md"
    q = "资料分几级？"
    a = ask(doc, q, base=PROXY)
    b = ask(doc, q, base=BASE)
    T.check(b["mode"] is None, "基线请求没有 _proxy 字段（确实是直连）")
    T.check(bool(a["answer"].strip()) and bool(b["answer"].strip()),
            "两条路径都返回了非空回答")

    # ⚠️ 实测结论（见 tools/probe_determinism2.py）：
    #    全量路径自身是稳定的（两次真全量逐字相同）；
    #    但【前缀复用】会改变 n_past → 批量组成不同 → 浮点归约顺序不同，
    #    导致措辞可能差一两个字（实测："资料分为三级" vs "资料分三级"）。
    #    所以断言【关键事实一致】，而不是逐字相同。
    import re

    def facts(s):
        return set(re.findall(r"\d+", s))

    fa, fb = facts(a["answer"]), facts(b["answer"])
    T.check("三" in a["answer"] and "三" in b["answer"],
            f"两条路径都答出「三级」这个关键事实")
    T.check(fa & fb,
            f"数字事实有交集：{sorted(fa)} ∩ {sorted(fb)} = {sorted(fa & fb)}")
    same = a["answer"].strip() == b["answer"].strip()
    T.note(f"逐字相同：{'是' if same else '否（数值路径差异，属已知现象）'}")
    T.note(f"代理：{a['answer'][:46]!r}")
    T.note(f"基线：{b['answer'][:46]!r}")


def t_cancel():
    T.case("请求取消不会让代理崩溃")
    text = (DEV / "corpus/large/proj-a-星桥门户项目全记录.md").read_text(
        encoding="utf-8")
    body = json.dumps({"messages": [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": text},
        {"role": "user", "content": "详细分析这个项目的全部风险。"}],
        "max_tokens": 512, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}).encode()

    # 手工发一半就断开
    try:
        s = socket.create_connection(("127.0.0.1", 8091), timeout=10)
        s.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8091\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            + body)
        time.sleep(0.25)
        s.close()
        T.check(True, "客户端提前断开完成")
    except Exception as e:                                    # noqa: BLE001
        T.check(False, f"断开时出错: {e}")

    time.sleep(1.0)
    T.check(alive(PROXY), "代理仍然健康")
    r = ask("small/alpha-客户对接规范.md", "登记时限是多久？")
    T.check(r["mode"] is not None, f"断开之后仍然能正常服务（模式 {r['mode']}）")


def t_bad_requests():
    T.case("异常请求处理")
    # 空 messages
    try:
        d, _ = post(PROXY + "/v1/chat/completions", {"messages": []})
        T.check(True, "空 messages 有响应（不崩）")
    except urllib.error.HTTPError as e:
        T.check(True, f"空 messages 返回 HTTP {e.code}（可接受）")
    except Exception as e:                                    # noqa: BLE001
        T.check(False, f"空 messages 抛出意外异常: {e}")

    # 坏 JSON
    r = urllib.request.Request(PROXY + "/v1/chat/completions",
                               data=b"{not json",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as f:
            T.check(False, "坏 JSON 居然被接受")
    except urllib.error.HTTPError as e:
        T.eq(e.code, 400, "坏 JSON 返回 400")

    T.check(alive(PROXY), "代理仍然健康")


def main():
    if not alive(PROXY) or not alive(BASE):
        T.skip(f"服务不在线（代理={alive(PROXY)} 模型={alive(BASE)}），"
               f"先跑 tools/start_adv.sh")
        return 0
    for fn in (t_hit_paths, t_version_facts, t_output_consistency,
               t_cancel, t_bad_requests):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
