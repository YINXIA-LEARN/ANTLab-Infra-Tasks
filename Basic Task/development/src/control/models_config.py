#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models_config.py —— 模型注册表的加载与查询

职责：
  · 读取 configs/models.json
  · 用现有的搜索逻辑把"文件名"解析成真实路径（评委路径未知，不能写死）
  · 查询每个模型的安装状态与校验值

对应计划书：第四节 4.1
"""

import hashlib
import json
from pathlib import Path

from . import config
from .logutil import log

# 注册表文件位置：<development>/configs/models.json
CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
MODELS_FILE = CONFIG_DIR / "models.json"

# sha256 缓存（算一次挺慢，1.7 GB 要几秒；按 路径+mtime 缓存）
_sha_cache = {}


def load_registry():
    """
    读取模型注册表。

    返回 (数据, 错误信息)。成功时错误为 None。
    """
    if not MODELS_FILE.is_file():
        return None, f"找不到模型注册表：{MODELS_FILE}"

    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"模型注册表不是合法 JSON：{e}"
    except OSError as e:
        return None, f"读取模型注册表失败：{e}"

    models = data.get("models")
    if not isinstance(models, list) or not models:
        return None, "模型注册表里 models 必须是非空数组"

    # 基本结构校验
    seen = set()
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            return None, f"models[{i}] 必须是对象"
        for key in ("id", "name", "file"):
            if not m.get(key):
                return None, f"models[{i}] 缺少必填字段 {key}"
        if m["id"] in seen:
            return None, f"模型 id 重复：{m['id']}"
        seen.add(m["id"])

    active = data.get("active")
    if active and active not in seen:
        return None, f"active 指向不存在的模型 id：{active}"

    return data, None


def _sha256_of(path):
    """算文件的 SHA256，带缓存（按路径 + 修改时间 + 大小）"""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _sha_cache:
        return _sha_cache[key]

    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        log(f"读取 {path} 计算校验值失败：{e}")
        return None

    digest = h.hexdigest()
    _sha_cache[key] = digest
    return digest


def describe_model(entry, active_id=None, compute_sha=False):
    """
    把注册表里的一条记录，补上"实际状态"后返回给前端。

    compute_sha=True 时才算 SHA256（慢，默认不算；前端可单独请求）
    """
    path, err = config.find_model_path_by_name(entry["file"])

    info = {
        "id": entry["id"],
        "name": entry.get("name") or entry["id"],
        "file": entry["file"],
        "params": entry.get("params"),
        "quant": entry.get("quant"),
        "size_mb": entry.get("size_mb"),
        "source": entry.get("source"),
        "notes": entry.get("notes"),
        "server_args": entry.get("server_args") or {},
        # ── 实际状态 ──
        "installed": path is not None,
        "path": str(path) if path else None,
        "is_active": (entry["id"] == active_id),
        "expected_sha256": entry.get("sha256") or None,
        "actual_sha256": None,
        "sha256_ok": None,          # None = 未校验
        "error": err,               # 没找到时的说明
    }

    if path is not None and compute_sha:
        actual = _sha256_of(path)
        info["actual_sha256"] = actual
        expected = entry.get("sha256")
        if expected and actual:
            info["sha256_ok"] = (actual == expected)

    return info


def list_models(active_id=None, compute_sha=False):
    """
    返回注册表里所有模型的完整状态。

    返回 (列表, 错误信息)
    """
    data, err = load_registry()
    if err:
        return None, err

    if active_id is None:
        active_id = data.get("active")

    out = [describe_model(m, active_id, compute_sha) for m in data["models"]]
    return out, None


def get_model_entry(model_id):
    """按 id 取一条注册表记录。返回 (记录, 错误信息)"""
    data, err = load_registry()
    if err:
        return None, err
    for m in data["models"]:
        if m["id"] == model_id:
            return m, None
    return None, f"注册表里没有这个模型 id：{model_id}"


def get_initial_active():
    """注册表里标记的 active（启动时用）"""
    data, err = load_registry()
    if err:
        return None
    return data.get("active")


def set_active(model_id):
    """
    把 active 写回注册表文件。

    ⚠️ 注意：这个文件是要提交到仓库的，所以写入要保持可读格式。
    """
    data, err = load_registry()
    if err:
        return False, err
    data["active"] = model_id
    try:
        MODELS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True, None
    except OSError as e:
        return False, f"写入注册表失败：{e}"
