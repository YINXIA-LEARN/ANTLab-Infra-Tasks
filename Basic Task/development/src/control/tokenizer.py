#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokenizer.py —— 带缓存的精确 token 计数服务

为什么需要缓存：
    llama-tokenize 每次调用都要【加载整个模型】才能拿到词表，
    实测单次 2~4 秒。界面上"选中文件就要显示 token 数"，
    每次都等 3 秒是不可接受的。

    所以按【文件 sha256】缓存结果：
      第一次算 → 慢（2~4 秒），之后 → 毫秒级
      文件改了 → sha256 变 → 自动重算（不会用到过期数据）

另一个设计：token 计数固定在 0.6B 上。
    实测 0.6B 与 1.7B 的 tokenizer 结果【完全一致】（同一词表），
    所以固定用 0.6B 既不影响准确性，又能避免为计数而重复加载大模型。
    这一点要记录在报告里，否则"为什么用 A 模型给 B 模型计数"会被质疑。
"""

import json
import re
import subprocess
import threading
from pathlib import Path

from .logutil import log

# 计数用的模型（词表与 1.7B 相同，见模块说明）
COUNT_MODEL = "Qwen3-0.6B-Q8_0.gguf"

TOKENIZE_CANDIDATES = [
    str(Path.home() / "antlab/src/llama.cpp/build/bin/llama-tokenize"),
    "/usr/local/bin/llama-tokenize",
]

MODEL_DIRS = [
    str(Path.home() / "antlab/models"),
]

# 缓存：cache_key -> token 数
# cache_key = f"{sha256}:{model}"，文件内容变了 key 就变
_cache = {}
_cache_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "errors": 0}


def find_tokenize_bin():
    for c in TOKENIZE_CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def find_count_model():
    for d in MODEL_DIRS:
        p = Path(d) / COUNT_MODEL
        if p.is_file():
            return p
    return None


def available():
    """计数能力是否可用（前端据此提示）"""
    return find_tokenize_bin() is not None and find_count_model() is not None


def info():
    return {
        "tokenize_bin": find_tokenize_bin(),
        "count_model": str(find_count_model()) if find_count_model() else None,
        "available": available(),
        "cache_size": len(_cache),
        "stats": dict(_stats),
        "note": "0.6B 与 1.7B 词表相同，计数固定用 0.6B",
    }


def count(text, sha256_key):
    """
    精确计数，带缓存。

    sha256_key 是文本内容的哈希，用来判断是否需要重算。
    """
    if not available():
        raise RuntimeError(
            "token 计数不可用：找不到 llama-tokenize 或计数模型")

    ck = f"{sha256_key}:{COUNT_MODEL}"
    with _cache_lock:
        if ck in _cache:
            _stats["hits"] += 1
            return _cache[ck]

    _stats["misses"] += 1
    binary = find_tokenize_bin()
    model = find_count_model()

    try:
        out = subprocess.run(
            [binary, "-m", str(model), "--stdin", "--show-count"],
            input=text.encode("utf-8"),
            capture_output=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        _stats["errors"] += 1
        raise RuntimeError("token 计数超时（超过 240 秒）")

    if out.returncode != 0:
        _stats["errors"] += 1
        raise RuntimeError(
            f"llama-tokenize 退出码 {out.returncode}："
            f"{out.stderr.decode('utf-8', 'replace')[-200:]}")

    m = re.search(rb"Total number of tokens:\s*(\d+)", out.stdout)
    if not m:
        _stats["errors"] += 1
        raise RuntimeError("llama-tokenize 输出里没有 token 计数")

    n = int(m.group(1))
    with _cache_lock:
        _cache[ck] = n
    log(f"token 计数完成：{n} tokens（缓存键 {sha256_key[:12]}…）")
    return n


def clear_cache():
    with _cache_lock:
        n = len(_cache)
        _cache.clear()
    return n
