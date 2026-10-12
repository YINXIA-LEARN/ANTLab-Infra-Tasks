#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py —— 阶段 S1 的启动入口

做三件事：
  1. 探测 llama-server 是否在跑（不在就跑不了对话，但控制层照常启动）
  2. 找到模型文件（找不到给出明确指引）
  3. 启动控制层

用法：
    python3 run.py
    ANTLAB_CONTROL_PORT=8001 python3 run.py       # 换端口

对应计划书：第八节 S1
"""

import sys
from pathlib import Path

# 让 control 包可以被导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import config, upstream      # noqa: E402
from control import server as control_server  # noqa: E402


def banner(text):
    print("=" * 62)
    print(text)
    print("=" * 62)


def main():
    banner(f"{config.SERVICE_NAME} —— 启动前检查")

    # ── 检查 1：下游模型服务 ────────────────────────────────────────────────
    health = upstream.get_health()
    state = health["state"]

    if state == "ok":
        props, err = upstream.get_props()
        if isinstance(props, dict):
            name = (props.get("model_path") or "").rsplit("/", 1)[-1]
            print(f"✅ 模型服务：正常")
            print(f"   模型    ：{name}")
            print(f"   量化    ：{props.get('model_ftype')}")
            print(f"   slot 数 ：{props.get('total_slots')}")
        else:
            print(f"✅ 模型服务：正常（但读取详情失败：{err}）")
    else:
        print(f"⚠️  模型服务：{health['detail']}")
        print()
        print("   控制层仍会启动，页面上会显示该状态。")
        print("   要让对话可用，请先在另一个终端启动模型服务：")
        print()
        print("       ~/antlab/src/llama.cpp/build/bin/llama-server \\")
        print(f"         -m ~/antlab/models/{config.MODEL_FILENAME} \\")
        print("         -ngl 99 -c 4096 --host 127.0.0.1 --port 8080 \\")
        print("         > ~/antlab/logs/server.log 2>&1 &")

    # ── 检查 2：本地模型文件 ────────────────────────────────────────────────
    local_path, local_err = config.find_model_path()
    if local_path:
        size_mb = local_path.stat().st_size / 1024 / 1024
        print(f"✅ 本地模型：{local_path}  ({size_mb:.0f} MiB)")
    else:
        print("⚠️  本地未找到模型文件：")
        print(local_err)

    # ── 检查 3：前端目录 ────────────────────────────────────────────────────
    if not config.WEB_DIR.is_dir():
        print(f"❌ 找不到前端目录：{config.WEB_DIR}")
        sys.exit(1)
    print(f"✅ 前端目录：{config.WEB_DIR}")

    print()
    control_server.main()


if __name__ == "__main__":
    main()
