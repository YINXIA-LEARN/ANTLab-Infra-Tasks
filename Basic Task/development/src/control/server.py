#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 控制层的 HTTP 服务

本阶段（S1）实现：
  GET  /                  返回前端页面
  GET  /static/*          静态资源（CSS/JS）
  GET  /api/status        ⭐ 聚合服务状态（真实数据，不写死）
  POST /api/chat          本阶段返回 501，S2 实现

设计的两个要点：
  1. 【同源】页面和 API 都从这一个服务出去 —— 彻底避开 CORS
     （实测 llama-server 的 Access-Control-Allow-Origin 是空值，直连必被浏览器拦）
  2. 【路径穿越防护】静态文件只允许 web/ 目录内的文件，
     并且解析后必须仍在该目录下

对应计划书：第三节（架构）、第五 5.3（预留接口）、第七节（安全设计）
"""

import json
import mimetypes
import posixpath
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, upstream
from . import bench_store
from . import chat as chat_mod
from . import files as files_mod
from . import model_manager
from . import models_config
from . import tokenizer
from .logutil import log

# 任务注册表在 common/ 下（与控制层平级），run.py 已把 phase1 目录加进 sys.path
try:
    from common import tasks as tasks_mod
    from common import file_input
except ImportError:                                       # pragma: no cover
    tasks_mod = None
    file_input = None

# 预留接口清单：尚未实现的统一返回 501，并说明属于哪个阶段
PLANNED_ENDPOINTS = {
    "/api/bench/run":        (3, "启动一次性能测试（前端触发）"),
    "/api/recovery/restart": (3, "触发服务重启（恢复测试）"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ANTLabControl/" + config.SERVICE_VERSION
    protocol_version = "HTTP/1.1"          # 支持 keep-alive

    # ── 工具方法 ────────────────────────────────────────────────────────────
    def _send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, code, raw, mime):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _not_implemented(self, path):
        phase, desc = PLANNED_ENDPOINTS[path]
        self._send_json(501, {
            "error": "not_implemented",
            "message": f"该功能为后续阶段预留（计划阶段 {phase}）：{desc}",
            "planned_phase": phase,
        })

    def log_message(self, fmt, *args):
        """覆盖默认日志：只记一行，且带时间前缀"""
        log(f"{self.address_string()} {fmt % args}")

    # ── 静态文件 ────────────────────────────────────────────────────────────
    def _serve_static(self, url_path):
        """
        提供 web/ 目录下的静态文件。

        ⚠️ 安全：必须防路径穿越。
        做法是【解析成绝对路径后，检查它是否仍在 WEB_DIR 之下】。
        """
        rel = url_path[len("/static/"):] if url_path.startswith("/static/") else url_path.lstrip("/")
        rel = posixpath.normpath(unquote(rel))
        if rel in ("", "."):
            rel = "index.html"

        target = (config.WEB_DIR / rel).resolve()
        web_root = config.WEB_DIR.resolve()

        # ⭐ 关键检查：解析后的路径必须在 web/ 之内
        if web_root != target and web_root not in target.parents:
            log(f"拒绝路径穿越尝试：{url_path!r} -> {target}")
            self._send_json(403, {"error": "forbidden", "message": "路径不在允许范围内"})
            return

        if not target.is_file():
            self._send_json(404, {"error": "not_found", "message": f"找不到静态文件：{rel}"})
            return

        mime, _ = mimetypes.guess_type(str(target))
        if mime is None:
            mime = "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime += "; charset=utf-8"

        try:
            self._send_bytes(200, target.read_bytes(), mime)
        except OSError as e:
            self._send_json(500, {"error": "read_failed", "message": str(e)})

    # ── GET ─────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return

        if path.startswith("/static/"):
            self._serve_static(path)
            return

        if path == "/api/status":
            try:
                payload = upstream.collect_status()
                # ⭐ S3：顺带带上模型切换状态，让状态栏/下拉框不用额外再请求一次
                try:
                    payload["model_switch"] = model_manager.get_state()
                except Exception as e:                    # noqa: BLE001
                    payload["model_switch"] = {"error": f"{type(e).__name__}: {e}"}
                self._send_json(200, payload)
            except Exception as e:                        # noqa: BLE001
                log(f"/api/status 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {
                    "error": "status_failed",
                    "message": f"{type(e).__name__}: {e}",
                })
            return

        if path == "/api/models":
            # ⭐ S3：列出模型注册表 + 每个的安装状态
            # compute_sha 用查询参数控制（算 1.7GB 的哈希要几秒，不能每次状态轮询都算）
            qs = urlparse(self.path).query
            compute_sha = "sha=1" in qs
            try:
                models, err, running_file = model_manager.list_models_with_status(
                    compute_sha=compute_sha)
                if err:
                    self._send_json(500, {"error": "registry_error", "message": err})
                    return
                st = model_manager.get_state()
                ai = models_config.active_info()
                self._send_json(200, {
                    "active": st["active"],
                    # ⭐ 当前模型从哪来：
                    #    runtime     用户在界面上选过（记在 .runtime/active-model.json）
                    #    registry    没人选过，用的注册表里的 default_active
                    #    first_model 注册表连 default_active 都没写
                    "active_source": ai.get("active_source"),
                    "default_active": ai.get("default_active"),
                    "switching": st["switching"],
                    "target": st["target"],
                    "running_file": running_file,
                    "sha_computed": compute_sha,
                    "models": models,
                })
            except Exception as e:                        # noqa: BLE001
                log(f"/api/models 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "models_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/files":
            # ⭐ A：列出可选的输入文件（题包自带 + 用户上传）
            try:
                self._send_json(200, {
                    "files": files_mod.list_files(),
                    "dirs": files_mod.dirs_info(),
                    "tokenizer": tokenizer.info(),
                })
            except Exception as e:                        # noqa: BLE001
                log(f"/api/files 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "files_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/files/meta":
            # ⭐ A：单个文件的元信息 + 精确 token 数（带缓存）
            fid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fid:
                self._send_json(400, {"error": "bad_request",
                                      "message": "缺少 id 查询参数"})
                return
            try:
                f = files_mod.read(fid)
                n = tokenizer.count(f["text"], f["sha256"])
                f.pop("text", None)        # 元信息接口不返回全文
                f["tokens"] = n
                self._send_json(200, f)
            except files_mod.FileError as e:
                self._send_json(404, {"error": "file_error", "message": str(e)})
            except Exception as e:                        # noqa: BLE001
                log(f"/api/files/meta 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "meta_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/tasks":
            # ⭐ A：规定任务清单（编号、类型、关联文件、提示词）
            try:
                self._send_json(200, {
                    "system_message": tasks_mod.SYSTEM_MESSAGE,
                    "tasks": tasks_mod.TASKS,
                })
            except Exception as e:                        # noqa: BLE001
                log(f"/api/tasks 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "tasks_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/tokenizer":
            self._send_json(200, tokenizer.info())
            return

        # ── 测试结果（只读）────────────────────────────────────────────
        if path == "/api/bench/runs":
            try:
                self._send_json(200, bench_store.list_runs())
            except Exception as e:                        # noqa: BLE001
                log(f"/api/bench/runs 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "bench_list_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/bench/compare":
            q = parse_qs(urlparse(self.path).query)
            a = (q.get("a") or [""])[0]
            b = (q.get("b") or [""])[0]
            if not a or not b:
                self._send_json(400, {"error": "bad_request",
                                      "message": "需要 a 和 b 两个 run_id"})
                return
            try:
                self._send_json(200, bench_store.compare(a, b))
            except bench_store.BenchStoreError as e:
                self._send_json(404, {"error": "bench_error", "message": str(e)})
            except Exception as e:                        # noqa: BLE001
                log(f"/api/bench/compare 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "compare_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        # /api/bench/runs/<run_id>[...]
        m = re.match(r"^/api/bench/runs/([^/]+)(/.*)?$", path)
        if m:
            run_id = unquote(m.group(1))
            sub = m.group(2) or ""
            q = parse_qs(urlparse(self.path).query)
            try:
                if sub == "":
                    self._send_json(200, bench_store.get_run(run_id))
                elif sub == "/requests":
                    self._send_json(200, bench_store.get_requests(
                        run_id,
                        offset=int((q.get("offset") or ["0"])[0] or 0),
                        limit=int((q.get("limit") or ["100"])[0] or 100),
                        only=(q.get("only") or [None])[0],
                        task_id=(q.get("task_id") or [None])[0]))
                elif sub == "/errors":
                    self._send_json(200, bench_store.get_errors(run_id))
                elif sub == "/gpu":
                    self._send_json(200, bench_store.get_gpu_samples(run_id))
                elif sub.startswith("/request/"):
                    rid = unquote(sub[len("/request/"):])
                    self._send_json(200, bench_store.get_request_detail(
                        run_id, rid))
                else:
                    self._send_json(404, {"error": "not_found",
                                          "message": f"未知子路径：{sub}"})
            except bench_store.BenchStoreError as e:
                self._send_json(404, {"error": "bench_error", "message": str(e)})
            except ValueError:
                self._send_json(400, {"error": "bad_request",
                                      "message": "offset/limit 必须是整数"})
            except Exception as e:                        # noqa: BLE001
                log(f"/api/bench/runs/... 失败：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "bench_read_failed",
                                      "message": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/chat":
            self._send_json(405, {"error": "method_not_allowed",
                                  "message": "/api/chat 必须用 POST"})
            return

        if path in PLANNED_ENDPOINTS:
            self._not_implemented(path)
            return

        self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})

    # ── /api/prepare 的实现 ─────────────────────────────────────────────────
    def _handle_prepare(self, payload):
        """
        把「文件 + 任务」组装成模型输入，并核算 token 预算。

        请求：
            { "task_id": "D01",                   # 用规定任务（与 file_id 二选一）
              "file_id": "task:办公输入01-...md",  # 或直接指定文件
              "prompt": "自定义任务提示词",          # 没给 task_id 时用
              "system_message": "...",             # 可选，默认用题目推荐那段
              "max_output_tokens": 2048 }

        响应：messages、各部分 token 明细、预算结论。
        """
        task_id = (payload.get("task_id") or "").strip() or None
        file_id = (payload.get("file_id") or "").strip() or None
        prompt = payload.get("prompt")
        system_message = payload.get("system_message")
        try:
            max_out = int(payload.get("max_output_tokens", 2048))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "bad_request",
                                  "message": "max_output_tokens 必须是整数"})
            return
        if not (1 <= max_out <= 32768):
            self._send_json(400, {"error": "bad_request",
                                  "message": "max_output_tokens 必须在 1~32768"})
            return

        # 上下文用服务端的真实值，不能猜
        props, _ = upstream.get_props()
        n_ctx = None
        if isinstance(props, dict):
            n_ctx = ((props.get("default_generation_settings") or {}).get("n_ctx")
                     or props.get("n_ctx"))
        if not n_ctx:
            self._send_json(503, {
                "error": "server_unavailable",
                "message": "读不到模型服务的上下文配置，请确认模型服务在运行"})
            return

        try:
            doc_text, doc_meta = None, None
            if task_id:
                try:
                    task = tasks_mod.get(task_id)
                except KeyError as e:
                    self._send_json(404, {"error": "unknown_task",
                                          "message": str(e)})
                    return
                if task["file"]:
                    fid = self._find_file_id_by_name(task["file"])
                    if fid is None:
                        self._send_json(404, {
                            "error": "file_missing",
                            "message": f"规定任务的关联文件没找到：{task['file']}"})
                        return
                    f = files_mod.read(fid)
                    doc_text = f["text"]
                    doc_meta = {k: v for k, v in f.items() if k != "text"}
                task_prompt = task["prompt"]
                task_meta = {"id": task["id"], "title": task["title"],
                             "kind": task["kind"]}
            else:
                if not isinstance(prompt, str) or not prompt.strip():
                    self._send_json(400, {
                        "error": "bad_request",
                        "message": "task_id 与 prompt 至少要给一个"})
                    return
                if file_id:
                    f = files_mod.read(file_id)
                    doc_text = f["text"]
                    doc_meta = {k: v for k, v in f.items() if k != "text"}
                task_prompt = prompt
                task_meta = {"id": None, "title": "自定义", "kind": "custom"}

            messages = file_input.build_messages(task_id, doc_text, system_message)

            # ── 分段精确计数（走缓存，重复请求毫秒级）──
            sys_text = messages[0]["content"]
            user_text = messages[1]["content"]
            t_sys = tokenizer.count(sys_text, file_input.sha256_text(sys_text))
            t_user = tokenizer.count(user_text, file_input.sha256_text(user_text))

            t_doc = 0
            if doc_text:
                seg = (f"{file_input.DOC_BEGIN}\n{doc_text.rstrip()}\n"
                       f"{file_input.DOC_END}")
                t_doc = tokenizer.count(seg, file_input.sha256_text(seg))
            seg = f"{file_input.TASK_BEGIN}\n{task_prompt.strip()}"
            t_task = tokenizer.count(seg, file_input.sha256_text(seg))
            t_tpl = max(0, t_user - t_doc - t_task)

            bud = file_input.budget(t_doc, t_task, t_sys, t_tpl, max_out, n_ctx)

            self._send_json(200, {
                "ok": True,
                "task": task_meta,
                "messages": messages,
                "doc": doc_meta,
                "tokens": {
                    "system": t_sys, "doc": t_doc, "task": t_task,
                    "template_overhead": t_tpl, "user_total": t_user,
                    "local_sum": t_sys + t_user,
                },
                "budget": bud,
                "count_model": tokenizer.COUNT_MODEL,
            })

        except files_mod.FileError as e:
            self._send_json(404, {"error": "file_error", "message": str(e)})
        except RuntimeError as e:
            self._send_json(500, {"error": "tokenize_failed", "message": str(e)})
        except Exception as e:                            # noqa: BLE001
            log(f"/api/prepare 失败：{e}\n{traceback.format_exc()}")
            self._send_json(500, {"error": "prepare_failed",
                                  "message": f"{type(e).__name__}: {e}"})

    def _find_file_id_by_name(self, filename):
        """在规定任务的输入目录里按文件名找 file id"""
        for f in files_mod.list_files():
            if f["name"] == filename:
                return f["id"]
        return None

    # ── POST ────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        # 先读掉请求体，避免 keep-alive 连接错位
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > config.MAX_REQUEST_BODY:
            self._send_json(413, {"error": "payload_too_large",
                                  "message": f"请求体超过上限 {config.MAX_REQUEST_BODY} 字节"})
            return
        body = self.rfile.read(length) if length > 0 else b""

        if path == "/api/models/switch":
            # ⭐ S3：切换模型（会重启 llama-server 进程，可能要几秒）
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_json(400, {"error": "bad_json",
                                      "message": f"请求体不是合法 JSON：{e}"})
                return

            model_id = payload.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                self._send_json(400, {"error": "bad_request",
                                      "message": "缺少 id 字段（要切换到哪个模型）"})
                return

            try:
                result = model_manager.switch_to(
                    model_id.strip(), force=bool(payload.get("force")))
            except Exception as e:                        # noqa: BLE001
                log(f"/api/models/switch 异常：{e}\n{traceback.format_exc()}")
                self._send_json(500, {"error": "switch_exception",
                                      "message": f"{type(e).__name__}: {e}"})
                return

            # 按结果给合适的 HTTP 状态码
            if result.get("ok"):
                code = 200
            else:
                code = {
                    "switching": 409,
                    "unknown_model": 404,
                    "not_installed": 404,
                    "no_binary": 500,
                    "start_failed": 500,
                    "rollback_failed": 500,
                }.get(result.get("error"), 400)
            self._send_json(code, result)
            return

        if path == "/api/chat":
            # ⭐ S2：流式转发
            # ⭐ S3 边界 2：切换中拒绝对话请求
            #    不能让请求打到"半死"的进程上（旧的可能刚被杀，新的还没就绪）
            if model_manager.is_switching():
                self._send_json(409, {
                    "error": "switching",
                    "message": f"模型正在切换到 {model_manager.get_state()['target']}，"
                               f"请稍候再发送",
                })
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_json(400, {"error": "bad_json",
                                      "message": f"请求体不是合法 JSON：{e}"})
                return
            try:
                chat_mod.stream_to_client(self, payload)
            except chat_mod.ChatError as e:
                # 还没发响应头，可以正常返回错误码
                self._send_json(502, {"error": "upstream_failed",
                                      "message": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                log("/api/chat 客户端在响应前断开")
            return

        if path == "/api/prepare":
            # ⭐ A：把「文件 + 任务」组装成模型输入，并核算 token 预算
            if tasks_mod is None or file_input is None:
                self._send_json(500, {"error": "tasks_unavailable",
                                      "message": "common/ 模块未找到"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_json(400, {"error": "bad_json",
                                      "message": f"请求体不是合法 JSON：{e}"})
                return
            self._handle_prepare(payload)
            return

        if path in PLANNED_ENDPOINTS:
            self._not_implemented(path)
            return

        self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})


def main():
    if not config.WEB_DIR.is_dir():
        log(f"❌ 找不到前端目录：{config.WEB_DIR}")
        log("   请确认 web/ 目录存在")
        sys.exit(1)

    # 启动前先探一次，把状态打到日志里，方便排错
    log(f"服务名称：{config.SERVICE_NAME}  版本：{config.SERVICE_VERSION}")
    log(f"前端目录：{config.WEB_DIR}")
    log(f"下游模型服务：{config.MODEL_SERVER_BASE}")

    health = upstream.get_health()
    log(f"模型服务健康检查：{health['state']} —— {health['detail']}")

    # ⭐ S3：登记当前生效的模型（会与实际在跑的模型校正）
    try:
        models, merr, running_file = model_manager.list_models_with_status()
        if merr:
            log(f"⚠️ 模型注册表读取失败：{merr}")
        else:
            st = model_manager.get_state()
            log(f"模型注册表：{len(models)} 个模型，当前 active = {st['active']}")
            if running_file:
                log(f"服务实际加载的是：{running_file}")
            for m in models:
                mark = "← 当前" if m["is_active"] else ""
                inst = "已安装" if m["installed"] else "❌未安装"
                log(f"   · {m['id']:<18} {inst:<8} {m.get('size_mb') or '?'} MB {mark}")
    except Exception as e:                                # noqa: BLE001
        log(f"⚠️ 初始化模型管理器失败：{type(e).__name__}: {e}")

    local_path, local_err = config.find_model_path()
    if local_path:
        log(f"本地模型文件：{local_path}")
    else:
        log("⚠️ 本地未找到模型文件（不影响控制层启动，但对话会不可用）")

    srv = ThreadingHTTPServer((config.CONTROL_HOST, config.CONTROL_PORT), Handler)
    srv.daemon_threads = True

    log("─" * 62)
    log(f"✅ 控制层已启动")
    log(f"   WSL 内访问：   http://127.0.0.1:{config.CONTROL_PORT}")
    log(f"   Windows 访问： http://localhost:{config.CONTROL_PORT}")
    log("─" * 62)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在关闭…")
    finally:
        srv.server_close()
        log("已关闭")


if __name__ == "__main__":
    main()
