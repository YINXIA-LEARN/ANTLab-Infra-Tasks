#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py —— 本阶段的所有配置项

设计原则：
  1. 只用标准库，零依赖
  2. 关键值支持环境变量覆盖（为将来"评委在自己机器上跑"做准备）
  3. 不做任何写死的绝对路径 —— 模型路径通过搜索定位

对应计划书：第五节（接口契约）、第七节（安全设计）
"""

import os
from pathlib import Path

# ── 网络配置 ────────────────────────────────────────────────────────────────
# 控制层：监听 0.0.0.0，让 Windows 浏览器能访问
CONTROL_HOST = os.environ.get("ANTLAB_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("ANTLAB_CONTROL_PORT", "8000"))

# llama-server：只监听 127.0.0.1，只给控制层调用（安全分级）
MODEL_SERVER_HOST = os.environ.get("ANTLAB_MODEL_HOST", "127.0.0.1")
MODEL_SERVER_PORT = int(os.environ.get("ANTLAB_MODEL_PORT", "8080"))

MODEL_SERVER_BASE = f"http://{MODEL_SERVER_HOST}:{MODEL_SERVER_PORT}"

# 调用模型服务的超时（秒）
UPSTREAM_STATUS_TIMEOUT = 5
UPSTREAM_CHAT_TIMEOUT = 300

# ── 模型配置 ────────────────────────────────────────────────────────────────
MODEL_FILENAME = os.environ.get("ANTLAB_MODEL_FILE", "Qwen3-0.6B-Q8_0.gguf")

# 模型搜索目录（按顺序找，找到一个就用）
# 这样评委把模型放哪都能跑，只要设 ANTLAB_MODEL_DIR 就行
#
# 目录层级（control/config.py）：
#     parents[0]=control [1]=src [2]=development [3]=Basic Task [4]=仓库根
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]        # 仓库根
_DEV_DIR = _HERE.parents[2]          # development/
MODEL_SEARCH_DIRS = [
    os.environ.get("ANTLAB_MODEL_DIR"),              # 环境变量优先
    str(Path.home() / "antlab" / "models"),          # 本题的开发位置（模型不在仓库里）
    str(_DEV_DIR / "models"),                        # development/models/
    str(_REPO_ROOT / "models"),                      # 仓库根 models/
    str(Path.cwd() / "models"),
    "/models",
    "/opt/models",
]

# ── 生成参数默认值 ──────────────────────────────────────────────────────────
DEFAULT_MAX_TOKENS = int(os.environ.get("ANTLAB_MAX_TOKENS", "512"))
DEFAULT_TEMPERATURE = float(os.environ.get("ANTLAB_TEMPERATURE", "0.7"))

DEFAULT_SYSTEM_PROMPT = (
    "你是一个乐于助人的中文助手，回答简洁准确。"
)

# ⚠️ Qwen3 必须关掉思考模式，否则 content 为空（思维链跑到 reasoning_content）
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}

# ── 安全配置 ────────────────────────────────────────────────────────────────
# 请求体大小上限（字节）。防止有人塞个大 body 把内存吃满
MAX_REQUEST_BODY = 1 * 1024 * 1024      # 1 MB

# 静态文件只允许从 web/ 目录提供（严格防路径穿越）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 会话 token：第一版不启用（局域网不可达，且会增加调试复杂度）
# 将来若要暴露给局域网，改为 True 并在页面里注入
REQUIRE_SESSION_TOKEN = os.environ.get("ANTLAB_REQUIRE_TOKEN", "0") == "1"

# ── 服务信息 ────────────────────────────────────────────────────────────────
SERVICE_NAME = "ANTLab 推理控制台"
SERVICE_VERSION = "dev-r2"


def find_model_path():
    """找【默认配置里那个】模型文件（MODEL_FILENAME）"""
    return find_model_path_by_name(MODEL_FILENAME)


def find_model_path_by_name(filename):
    """
    按文件名在 MODEL_SEARCH_DIRS 里搜索。

    为什么需要它：模型切换器要支持多个模型，每个模型文件名不同，
    不能只认一个 MODEL_FILENAME。而【评委的模型路径未知】，
    所以靠"搜索一组候选目录"来解决，而不是写死绝对路径。

    返回 (Path, None) 或 (None, 说明字符串)
    """
    for d in MODEL_SEARCH_DIRS:
        if not d:
            continue
        p = Path(d) / filename
        try:
            if p.is_file():
                return p, None
        except OSError:
            continue

    tried = "\n".join(f"      - {d}" for d in MODEL_SEARCH_DIRS if d)
    msg = (
        f"找不到模型文件 {filename}\n"
        f"  已尝试这些目录：\n{tried}\n"
        f"  解决办法（任选一种）：\n"
        f"      ① 设置环境变量： export ANTLAB_MODEL_DIR=/你的/模型目录\n"
        f"      ② 放到默认位置： mkdir -p ~/antlab/models && 把 {filename} 放进去\n"
        f"      ③ 如果还没有模型：从 ModelScope 的 Qwen/Qwen3-*-GGUF 仓库下载"
    )
    return None, msg
