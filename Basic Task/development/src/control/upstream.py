#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upstream.py —— 与控制层下游的 llama-server 通信

职责：
  · 查询服务健康状态（带【严格就绪判定】）
  · 读取服务与模型信息（/props）
  · 读取 slot 状态（/slots）
  · 读取 GPU 显存（调 nvidia-smi）

⚠️ 一条重要的经验（计划书 R3）：
   就绪判定必须【解析 JSON】判断 status == "ok"，
   不能用字符串包含（"Loading model" 这类消息里也可能含 "ok" 子串）。
   这个坑我之前实际踩过，导致误判服务已就绪。

对应计划书：第五节 5.1 / 第九节 R3
"""

import json
import socket
import subprocess
import urllib.error
import urllib.request

from . import config


def port_in_use(port, host="127.0.0.1"):
    """
    判断端口是否已被监听。

    做法：尝试自己 bind 那个端口 —— 失败就说明有人在用。
    这比"尝试 connect"更准确：connect 成功只说明有人在监听，
    而 bind 失败才是"确实被占了"。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False          # 能 bind → 没人占
    except OSError:
        return True           # bind 失败 → 被占了
    finally:
        s.close()


def _get_json(path, timeout=None):
    """
    向 llama-server 发一个 GET 请求并解析 JSON。

    返回 (数据, 错误信息)。成功时错误为 None。
    """
    url = config.MODEL_SERVER_BASE + path
    timeout = timeout or config.UPSTREAM_STATUS_TIMEOUT
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"连接失败（{e.reason}）"
    except json.JSONDecodeError as e:
        return None, f"响应不是合法 JSON（{e}）"
    except Exception as e:                       # noqa: BLE001 - 兜底，保证不崩
        return None, f"{type(e).__name__}: {e}"


def get_health():
    """
    严格就绪判定。

    返回 dict：
        state: "ok" | "loading" | "unreachable" | "error"
        detail: 说明文字
    """
    data, err = _get_json("/health")
    if err is not None:
        return {"state": "unreachable", "detail": err}

    if not isinstance(data, dict):
        return {"state": "error", "detail": "健康检查返回结构异常"}

    # ⭐ 关键：解析 JSON 字段，而不是做字符串匹配
    status = data.get("status")
    if status == "ok":
        return {"state": "ok", "detail": "服务正常"}

    # 模型仍在加载时会返回 error 字段
    if "error" in data:
        msg = ""
        e = data.get("error")
        if isinstance(e, dict):
            msg = str(e.get("message", ""))
        else:
            msg = str(e)
        if "loading" in msg.lower():
            return {"state": "loading", "detail": msg or "模型加载中"}
        return {"state": "error", "detail": msg or "服务返回错误"}

    return {"state": "error", "detail": f"未知状态：{status!r}"}


def get_props():
    """读取服务与模型信息。返回 (数据, 错误)。"""
    return _get_json("/props")


def get_slots():
    """读取 slot 状态。返回 (数据, 错误)。"""
    return _get_json("/slots")


def get_gpu_memory():
    """
    读 GPU 显存占用。

    nvidia-smi 在 WSL 里是可用的（路径 /usr/lib/wsl/lib/nvidia-smi）。
    返回 dict：available / used_mib / total_mib / error
    """
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[:120] or "nvidia-smi 返回非零"}

        line = out.stdout.strip().splitlines()[0]
        used, total = [int(x.strip()) for x in line.split(",")]
        return {"available": True, "used_mib": used, "total_mib": total, "error": None}

    except FileNotFoundError:
        return {"available": False, "error": "找不到 nvidia-smi"}
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "nvidia-smi 超时"}
    except Exception as e:                       # noqa: BLE001
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def collect_status():
    """
    汇总成前端要的完整状态对象（计划书 5.1 的响应结构）。

    设计要点：任何一项失败都不让整个接口失败 ——
    把问题记到 errors 列表里，前端照常渲染，并显示哪一项不可用。
    """
    errors = []

    # ── 1. 健康状态 ──
    health = get_health()
    if health["state"] != "ok":
        errors.append(f"模型服务：{health['detail']}")

    # ── 2. 服务与模型信息 ──
    props, props_err = (None, None)
    if health["state"] == "ok":
        props, props_err = get_props()
        if props_err:
            errors.append(f"读取服务信息失败：{props_err}")

    model_block = {
        "reachable": health["state"] == "ok",
        "health": health["state"],
        "health_detail": health["detail"],
        "model_path": None,
        "model_name": None,
        "quant": None,
        "n_ctx": None,
        "total_slots": None,
        "slots": [],
        "build_info": None,
    }

    if isinstance(props, dict):
        mp = props.get("model_path")
        model_block["model_path"] = mp
        model_block["model_name"] = mp.rsplit("/", 1)[-1] if mp else None
        model_block["quant"] = props.get("model_ftype")
        model_block["total_slots"] = props.get("total_slots")
        model_block["build_info"] = props.get("build_info")

        dgs = props.get("default_generation_settings") or {}
        model_block["n_ctx"] = dgs.get("n_ctx") or props.get("n_ctx")

    # ── 3. slot 状态 ──
    if health["state"] == "ok":
        slots, slots_err = get_slots()
        if slots_err:
            errors.append(f"读取 slot 状态失败：{slots_err}")
        elif isinstance(slots, list):
            model_block["slots"] = [
                {
                    "id": s.get("id"),
                    "n_ctx": s.get("n_ctx"),
                    "is_processing": s.get("is_processing"),
                }
                for s in slots
            ]

    # ── 4. 显存 ──
    gpu = get_gpu_memory()
    if not gpu.get("available"):
        errors.append(f"读取显存失败：{gpu.get('error')}")

    # ── 5. 本地模型文件是否存在（与控制层配置比对）──
    local_path, local_err = config.find_model_path()
    local_block = {
        "found": local_path is not None,
        "path": str(local_path) if local_path else None,
        "expected_filename": config.MODEL_FILENAME,
        "hint": local_err,
    }
    if local_path is None:
        errors.append("本地未找到配置中的模型文件")

    return {
        "control": {
            "ok": True,
            "service": config.SERVICE_NAME,
            "version": config.SERVICE_VERSION,
        },
        "model_server": model_block,
        "local_model": local_block,
        "gpu": gpu,
        "errors": errors,
    }
