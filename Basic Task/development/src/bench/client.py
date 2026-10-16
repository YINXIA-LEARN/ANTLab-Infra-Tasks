#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client.py —— 单个请求的执行与计时

这是整套测试程序最需要精确的部分。计时口径直接决定报告里的
TTFT / TPOT / 吞吐 是否可信，所以口径在这里写死并注释清楚。

────────────────────────────────────────────────────────────────────────
计时口径（对应题目对指标的定义）
────────────────────────────────────────────────────────────────────────
    t_submit ──────────────── t_first ──────────────── t_done
        │                        │                        │
        │<────── TTFT ──────────>│                        │
        │                        │<──── 生成阶段 ────────>│
        │<────────────── 完成耗时 ──────────────────────>│

  · t_submit  发出 HTTP 请求的前一刻（客户端可观察的起点，
              因此 TTFT 里【包含排队与传输】——题目明确要求如此）
  · t_first   收到【首个含内容的块】的时刻。
              ⚠️ 不是"收到第一个块"：流式第一个块通常只声明 role、content 为 null，
                 若把它算作首字，TTFT 会被系统性低估几十毫秒。
  · t_done    收到 [DONE] 或流结束

  TPOT 两个口径都给，报告里要说明用了哪个：
    tpot_excl_first = (t_done - t_first) / (n_out - 1)   排除首 token
    tpot_incl_first = (t_done - t_first) / n_out         不排除

  token 数一律取【服务端】的 timings：
    ⚠️ 不能数 SSE 块数 —— 一个块可能包含多个 token（实测见过一次给 5 个字）。
       题目也专门提醒了这一点。
────────────────────────────────────────────────────────────────────────
"""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid


class RequestResult:
    """一次请求的完整记录"""

    def __init__(self, req_id, task_id):
        self.d = {
            "request_id": req_id,
            "task_id": task_id,
            "status": None,          # success / failed / timeout / cancelled
            "error_type": None,
            "error_message": None,

            # ── 时间点（都是相对本次请求开始的秒数，便于对齐）──
            "t_submit": None,
            "t_first": None,
            "t_done": None,

            # ── 耗时（毫秒；报告里统一用 ms）──
            "ttft_ms": None,
            "total_ms": None,
            "tpot_excl_first_ms": None,
            "tpot_incl_first_ms": None,

            # ── token 数（服务端口径）──
            "prompt_tokens": None,
            "output_tokens": None,
            "prompt_ms": None,
            "predicted_ms": None,
            "cache_hit_tokens": None,     # 前缀缓存命中的 token 数

            # ── 输出 ──
            "output_text": None,
            "output_chars": None,
            "finish_reason": None,        # stop / length
            "n_chunks": None,             # SSE 块数（仅供诊断，不当 token 用）

            # ── 取消（题目要求的「取消」状态）──
            #    True 表示这条请求被主动掐断。
            #    注意它与 status="cancelled" 是两回事：
            #    status 是分类结果，cancelled 是原因标记 ——
            #    因为异常路径（HTTPError/Timeout）不会有 cancelled 字段，
            #    单看 status 无法区分"被取消"和"因其他原因没跑完"。
            "cancelled": False,

            # ── HTTP ──
            "http_status": None,
        }

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v

    def get(self, k, default=None):
        """
        补一个 get()，避免和 dict 混用时踩坑。

        踩过的坑：我在判定逻辑里写了 res["stream_error"]（对）和
        res.get("stream_error")（错）两种写法混用。
        这个类只实现了 __getitem__，没有 get()，
        于是"请求真的被中断"这条分支一执行就抛
        AttributeError: 'RequestResult' object has no attribute 'get'，
        被外层 except 接住后记成了 failed ——
        修复代码一遇到需要修复的情况就自己崩了。
        """
        return self.d.get(k, default)

    def to_dict(self):
        return self.d


def make_request_id():
    """请求 ID：时间前缀 + 随机后缀，便于按时间排序又不会重复"""
    return time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:8]


def run_one(base_url, messages, max_tokens=2048, temperature=0.0,
            timeout=600, client_id=None, cancel_event=None):
    """
    执行一次流式请求，返回 RequestResult。

    ⚠️ 用流式而不是非流式：
       非流式拿不到"首个内容"的时刻，也就无法测量 TTFT。
       题目要求 TTFT 是"提交至首次收到有效生成内容"，必须流式。

    ── cancel_event：主动取消（对应题目要求的「取消」状态）──────────────
    threading.Event。被 set 时立刻停止读取，这条请求记为 status="cancelled"。

    为什么需要它（题目原文）：
        「成功、失败、超时、★取消等状态，错误类型与错误信息。」
        没有取消能力的话，cancelled 就只是注释里的一个词 —— 声明了却做不到。

    ⚠️ 判定优先级：取消 > 成功。
       即使取消那一刻已经收到 finish_reason，仍然记为取消 ——
       因为请求是被【主动掐断】的，不是自然跑完。
       这个顺序搞反了，会让"取消数"恒为 0。

    ⚠️ 取消【不算失败】：它是主动行为，与"服务出错"性质不同。
       汇总里单独计数（n_cancelled），也不计入 token 吞吐分子
       （取消时产出的 token 是残缺的）。
    """
    res = RequestResult(make_request_id(), None)
    if client_id is not None:
        res["client_id"] = client_id

    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        # ⭐ 关键：llama.cpp 默认【不在流式响应里发 usage】，
        #    必须显式要。没有它就拿不到"实际输入 token 数"——
        #    而 timings.prompt_n 在前缀缓存命中时只是"新算的 token 数"（可能为 1），
        #    不是输入总数。实测：prompt_n(24) + cache_n(3) = prompt_tokens(27)。
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST",
    )

    t0 = time.perf_counter()
    res["t_submit"] = 0.0
    text_parts = []
    n_chunks = 0
    timings = {}
    saw_done = False          # 是否收到 data: [DONE]
    cancelled = False         # 是否被主动取消
    cancel_at_chunk = None    # 取消发生在第几个块

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res["http_status"] = resp.status
            try:
                for raw in resp:
                    # ⭐ 取消检查放在读每一行【之前】——
                    #    这样"取消后还能读到多少"由轮询间隔决定，
                    #    而不会把整条流读完。
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        cancel_at_chunk = n_chunks
                        break

                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        saw_done = True
                        break

                    n_chunks += 1
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue

                    ch = (obj.get("choices") or [{}])[0]
                    delta = (ch.get("delta") or {})
                    piece = delta.get("content")

                    if piece:
                        # ⭐ 首个【含内容】的块才记为 t_first
                        if res["t_first"] is None:
                            res["t_first"] = time.perf_counter() - t0
                        text_parts.append(piece)

                    if ch.get("finish_reason"):
                        res["finish_reason"] = ch["finish_reason"]

                    # 服务端在最后一个块里给出精确统计
                    if obj.get("timings"):
                        timings = obj["timings"]
                    if obj.get("usage"):
                        u = obj["usage"]
                        res["prompt_tokens"] = u.get("prompt_tokens")
                        res["output_tokens"] = u.get("completion_tokens")
            except Exception as e:                            # noqa: BLE001
                # ⚠️ 读到一半连接断了（服务端中途关闭）。
                #    不能当成失败丢掉 —— 已经收到的内容是有效数据，
                #    但也不能算成功（没答完）。统一走 incomplete 判定。
                res["stream_error"] = f"{type(e).__name__}: {e}"

        res["t_done"] = time.perf_counter() - t0

        # ── ⭐ 关键：判定"这条请求到底完成了没有" ──────────────────────
        #
        # 这里是我踩过的坑，务必理解：
        #   服务端在 KV 池被撑爆时会 send_error 并【终止整个批次】。
        #   同一批里，响应头还没发出去的请求，客户端会收到 HTTP 500（能正确判失败）；
        #   而响应头已经发出去的请求，客户端只看到"连接被关闭"，
        #   for 循环正常结束、不抛异常、HTTP 状态是 200。
        #   我原来在这里【无条件】写 status="success"，
        #   结果 16 条被中途掐断的请求全被记成成功，
        #   汇总出来的成功率 97% 是假的（实际约 82%）。
        #
        #   正确判据：服务端必须给出结束标记才算完成。
        #     finish_reason 有值（stop / length）  → 正常完成
        #     收到 [DONE]                          → 也算正常完成（兜底）
        #     两者都没有                            → incomplete（被中断）
        # ⚠️ 取消优先于成功：取消是被主动掐断的，即使已经拿到 finish_reason
        #    也不算自然跑完。顺序搞反会让"取消数"恒为 0。
        if cancelled:
            res["status"] = "cancelled"
            res["error_type"] = "Cancelled"
            res["error_message"] = (
                f"请求被主动取消（读到第 {cancel_at_chunk} 个块时）")
        elif res["finish_reason"] is None and not saw_done:
            res["status"] = "incomplete"
            res["error_type"] = "NoFinishReason"
            res["error_message"] = (
                "流在收到结束标记前中断（没有 finish_reason，也没有 [DONE]）。"
                "常见原因：同批次请求把共享的 KV 池撑爆，"
                "服务端 send_error 后终止了这一批。"
                + (f" 底层错误：{res['stream_error']}"
                   if res.get("stream_error") else ""))
        else:
            res["status"] = "success"
        res["saw_done"] = saw_done
        res["cancelled"] = cancelled
        # 取消时已收到的内容仍然保留 —— 是有效数据，只是不完整
        res["output_text"] = "".join(text_parts)
        res["n_chunks"] = n_chunks

    except urllib.error.HTTPError as e:
        res["t_done"] = time.perf_counter() - t0
        res["status"] = "failed"
        res["http_status"] = e.code
        res["error_type"] = f"HTTP{e.code}"
        try:
            detail = e.read().decode("utf-8", "replace")
            m = json.loads(detail)
            res["error_message"] = (m.get("error") or {}).get("message") \
                or detail[:300]
        except Exception:                                     # noqa: BLE001
            res["error_message"] = "HTTP 错误（无法解析响应体）"
        return res

    except TimeoutError:
        res["t_done"] = time.perf_counter() - t0
        res["status"] = "timeout"
        res["error_type"] = "Timeout"
        res["error_message"] = f"超过 {timeout} 秒未完成"
        return res

    except Exception as e:                                    # noqa: BLE001
        res["t_done"] = time.perf_counter() - t0
        res["status"] = "failed"
        res["error_type"] = type(e).__name__
        res["error_message"] = str(e)[:300]
        return res

    # ── 汇总 ───────────────────────────────────────────────────────────────
    #
    # ⚠️ 输入 token 数的口径（这里我绕过一次弯，记下来）：
    #
    #    timings.prompt_n 是【本次实际参与计算的 token 数】，
    #    前缀缓存命中时它可能只有 1（只有新增的那个 token 要算），
    #    所以它【不是】"实际输入 token 数"。我一开始用了它，
    #    结果所有记录都写成输入 1 token，直接违反题目要求。
    #
    #    正确来源是 usage.prompt_tokens（llama.cpp 需要流式里显式
    #    传 stream_options.include_usage 才会给）。
    #    实测印证：prompt_n(24) + cache_n(3) = prompt_tokens(27)。
    #
    #    timings 仍然保留：它给的是服务端生成耗时与速率，算 TPOT 的对账依据。
    text = "".join(text_parts)
    res["output_text"] = text
    res["output_chars"] = len(text)
    res["n_chunks"] = n_chunks

    # usage.prompt_tokens 是唯一正确的输入总数
    usage_prompt = res["prompt_tokens"]                 # 来自 usage
    res["prompt_tokens"] = usage_prompt
    if usage_prompt is None:
        # 兜底并标注，避免静默写错
        pn = timings.get("prompt_n")
        cn = timings.get("cache_n") or 0
        res["prompt_tokens"] = (pn + cn) if pn is not None else None
        res["prompt_tokens_source"] = "timings_fallback"
    else:
        res["prompt_tokens_source"] = "usage"

    # 输出 token 数用 predicted_n（本次真正生成的个数，权威）
    res["output_tokens"] = timings.get("predicted_n") or res["output_tokens"]
    res["output_tokens_source"] = ("timings.predicted_n"
                                   if timings.get("predicted_n") else "usage")
    res["predicted_ms"] = timings.get("predicted_ms")
    res["cache_hit_tokens"] = timings.get("cache_n")
    res["prompt_n_computed"] = timings.get("prompt_n")   # 本次新算的 token 数
    res["prompt_ms"] = timings.get("prompt_ms")

    if res["t_first"] is not None:
        res["ttft_ms"] = res["t_first"] * 1000.0
    if res["t_done"] is not None:
        res["total_ms"] = res["t_done"] * 1000.0

    n_out = res["output_tokens"]
    if res["t_first"] is not None and res["t_done"] is not None \
            and n_out and n_out > 0:
        gen_s = res["t_done"] - res["t_first"]
        if n_out > 1:
            res["tpot_excl_first_ms"] = gen_s / (n_out - 1) * 1000.0
        res["tpot_incl_first_ms"] = gen_s / n_out * 1000.0

    return res
