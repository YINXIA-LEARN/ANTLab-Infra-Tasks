#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chat.py —— 对话流式转发（阶段 S2 的核心）

职责：
  接收浏览器发来的对话请求 → 组装成 llama-server 要的格式
  → 以流式请求上游 → 【逐块、不缓冲】转发回浏览器

为什么不能"收完再发"（这是本模块存在的全部理由）：
  llama-server 的流式响应是 text/event-stream + chunked，
  一个 SS E块通常只含 1~2 个汉字，块与块之间间隔几毫秒到几百毫秒。
  如果控制层先把整个响应读完再回给浏览器，用户看到的就是
  "等半天、然后所有字一起蹦出来" —— 流式的意义就没了。
  所以必须：readline() 读一块 → write() 一块 → flush() 一块。

对应计划书：第五节 5.2（含 7 条流式实现要求）
"""

import json
import urllib.error
import urllib.request

from . import config
from .logutil import log


class ChatError(Exception):
    """请求在【发送响应头之前】就失败了（可以返回正常的 HTTP 错误码）"""


def _build_payload(body):
    """
    把浏览器的请求整理成 llama-server 的 payload。

    body 期望：
        {
          "messages": [{"role": "...", "content": "..."}],
          "system_prompt": "..."   (可选)
          "max_tokens": 512        (可选)
          "temperature": 0.7       (可选)
        }
    """
    if not isinstance(body, dict):
        raise ChatError("请求体必须是 JSON 对象")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ChatError("messages 必须是非空数组")

    # 校验每条消息的结构
    cleaned = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise ChatError(f"messages[{i}] 必须是对象")
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant", "system"):
            raise ChatError(f"messages[{i}].role 不合法：{role!r}")
        if not isinstance(content, str):
            raise ChatError(f"messages[{i}].content 必须是字符串")
        cleaned.append({"role": role, "content": content})

    # 系统提示词：如果前端给了，且消息里还没有 system，就插到最前面
    sys_prompt = body.get("system_prompt")
    if isinstance(sys_prompt, str) and sys_prompt.strip():
        if not any(m["role"] == "system" for m in cleaned):
            cleaned.insert(0, {"role": "system", "content": sys_prompt.strip()})

    max_tokens = body.get("max_tokens", config.DEFAULT_MAX_TOKENS)
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        raise ChatError("max_tokens 必须是整数")
    if not (1 <= max_tokens <= 32768):
        raise ChatError("max_tokens 必须在 1~32768 之间")

    temperature = body.get("temperature", config.DEFAULT_TEMPERATURE)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        raise ChatError("temperature 必须是数字")
    if not (0.0 <= temperature <= 2.0):
        raise ChatError("temperature 必须在 0~2 之间")

    return {
        "messages": cleaned,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,                                   # ⭐ 必须
        # ⭐ Qwen3 必须关思考模式，否则 content 为空
        "chat_template_kwargs": dict(config.CHAT_TEMPLATE_KWARGS),
    }


def open_upstream(payload):
    """
    向上游发起流式请求，返回 response 对象。

    失败时抛 ChatError（此时还没给浏览器发任何东西，可以正常返回错误码）。
    """
    url = config.MODEL_SERVER_BASE + "/v1/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=config.UPSTREAM_CHAT_TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:                                  # noqa: BLE001
            pass
        raise ChatError(f"上游返回 HTTP {e.code}：{detail}")
    except urllib.error.URLError as e:
        raise ChatError(
            f"连不上模型服务（{e.reason}）。"
            f"请确认 llama-server 已在 {config.MODEL_SERVER_BASE} 运行"
        )
    except Exception as e:                                  # noqa: BLE001
        raise ChatError(f"{type(e).__name__}: {e}")


def stream_to_client(handler, body):
    """
    完整的转发流程：组装 → 连接上游 → 逐块转发。

    handler 是 http.server 的请求处理器，用它来写响应。

    返回：本次转发的统计（给日志用）
    """
    # ── 1. 组装（失败可以直接回错误码）────────────────────────────────────
    payload = _build_payload(body)
    n_msgs = len(payload["messages"])

    # 记录本次请求的生效参数。
    # 为什么要把 temperature 也记下来：
    #   做基线/优化对比实验时，必须保证"输出条件一致"。
    #   如果日志里只有 max_tokens，就没法核实温度用的是多少 ——
    #   到时候只能靠行为反推（"两次答案一样，说明温度是 0"），
    #   这在报告里属于"不充分的证据"。记下来就能直接引用。
    n_sys = sum(1 for m in payload["messages"] if m["role"] == "system")
    param_desc = (
        f"max_tokens={payload['max_tokens']}, "
        f"temperature={payload['temperature']:g}, "
        f"system_prompt={'有' if n_sys else '无'}"
    )

    # ── 2. 连接上游 ────────────────────────────────────────────────────────
    upstream = open_upstream(payload)

    # ── 3. 发送响应头 ──────────────────────────────────────────────────────
    # ⚠️ 关键：不设 Content-Length！
    #    设了长度，浏览器就必须等够那么多字节才能用，流式就废了。
    #
    # ⚠️⚠️ 另一个关键：必须声明 Connection: close，并在结束时真的关闭连接。
    #    原因：BaseHTTPRequestHandler 在 HTTP/1.1 下默认保持连接（keep-alive），
    #    而它又不会自动发送 chunked 的结束标记（0\r\n\r\n）。
    #    结果就是浏览器读流时永远等不到 EOF —— fetch 的 reader 一直挂着，
    #    前端 while(!done) 不退出，收尾代码（解锁按钮）永远不执行。
    #    这个 bug 的实际症状是：回答已经显示完了，但"发送"按钮一直是"生成中…"，
    #    必须手动点一下"停止"（触发 abort）才能继续。
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    # 防中间层（nginx 等）缓冲。llama-server 自己也发这个头，这里保持一致
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    # 告诉 http.server：这次响应结束后关闭 socket，让浏览器收到 EOF
    handler.close_connection = True

    log(f"/api/chat 开始转发（{n_msgs} 条消息 | {param_desc}）")

    # ── 4. 逐块转发 ────────────────────────────────────────────────────────
    n_chunks = 0
    n_content = 0          # 含正文的块数（≈生成的 token 数）
    n_done = 0
    finish_reason = None   # "stop" = 自然结束；"length" = 被 max_tokens 截断
    client_gone = False

    try:
        while True:
            line = upstream.readline()          # ⭐ 阻塞读一行，不等整个响应
            if not line:
                break                            # 上游结束

            n_chunks += 1

            # ── 顺带统计（不改动转发内容）────────────────────────────────
            # 只对本地这一份 line 做解析，写回浏览器的仍是原始字节。
            s = line.strip()
            if s.startswith(b"data:") and s != b"data: [DONE]":
                try:
                    obj = json.loads(s[5:].strip().decode("utf-8"))
                    ch = (obj.get("choices") or [{}])[0]
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        n_content += 1
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                except Exception:                # noqa: BLE001
                    pass                          # 解析失败不影响转发
            elif s == b"data: [DONE]":
                n_done += 1

            try:
                handler.wfile.write(line)
                handler.wfile.flush()            # ⭐ 立刻推给浏览器，不能攒
            except (BrokenPipeError, ConnectionResetError):
                # 客户端提前断开（点了停止 / 关了页面）
                client_gone = True
                log(f"/api/chat 客户端已断开，在第 {n_chunks} 块时中止")
                break

    except Exception as e:                       # noqa: BLE001
        log(f"/api/chat 转发中断：{type(e).__name__}: {e}")
        # 已经发过响应头了，只能用 SSE 事件告诉前端
        try:
            err = json.dumps({"error": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False)
            handler.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            handler.wfile.flush()
        except Exception:                        # noqa: BLE001
            pass
    finally:
        # ⭐ 无论怎么结束，都要关掉上游连接
        #    否则上游可能继续生成，白白占着算力和 slot
        try:
            upstream.close()
        except Exception:                        # noqa: BLE001
            pass

        state = "客户端断开" if client_gone else "正常结束"

        # finish_reason 的含义（做实验时很有用）：
        #   stop   → 模型自己说完了
        #   length → 撞上了 max_tokens 上限，回答被截断
        # 如果做"回答完整性"判断时看到 length，就说明 max_tokens 给小了。
        if finish_reason == "length":
            end_reason = "命中 max_tokens 上限（回答被截断）"
        elif finish_reason == "stop":
            end_reason = "模型自然结束"
        elif finish_reason:
            end_reason = f"结束原因={finish_reason}"
        else:
            end_reason = "未收到结束原因"

        log(f"/api/chat 转发完成：{n_chunks} 块（含正文 {n_content}），"
            f"{state}，{end_reason}")

    return {
        "chunks": n_chunks,
        "content_chunks": n_content,
        "messages": n_msgs,
        "client_gone": client_gone,
        "finish_reason": finish_reason,
    }
