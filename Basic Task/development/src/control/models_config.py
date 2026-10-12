#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models_config.py —— 模型注册表的加载与查询

职责：
  · 读取 configs/models.json（模型【定义】）
  · 用现有的搜索逻辑把"文件名"解析成真实路径（评委路径未知，不能写死）
  · 查询每个模型的安装状态与校验值
  · 维护"当前正在用哪个模型"这个【运行时状态】

────────────────────────────────────────────────────────────────────────────
⭐ 为什么把"当前模型"从注册表里挪出来

  原来 configs/models.json 里有个 "active" 字段，切模型时会写回这个文件。
  但那个文件是【提交到仓库】的，于是每切一次模型 git 就脏一次，
  提交历史里混进一堆"只是想换个模型试试"的噪音。

  更麻烦的是语义混乱：一个被跟踪的配置文件里混了两种东西 ——
      models[]        模型定义（有哪些、怎么启动、SHA256）   → 属于配置
      active          此刻正在跑哪个                        → 属于运行时状态

  现在拆开：
      configs/models.json         进 git。只放定义 + 默认用哪个（default_active）
      .runtime/active-model.json  不进 git（.gitignore 覆盖）。放当前选择

  读取顺序：.runtime/active-model.json > 注册表的 default_active > 第一个模型
  这样切换仍然持久化（重启服务还记得），但注册表永远干净。
────────────────────────────────────────────────────────────────────────────

对应计划书：第四节 4.1
"""

import hashlib
import json
import time
from pathlib import Path

from . import config
from .logutil import log

# 目录层级（control/models_config.py）：
#     parents[0]=control [1]=src [2]=development
_DEV_DIR = Path(__file__).resolve().parents[2]

# 注册表文件位置：<development>/configs/models.json（进 git）
CONFIG_DIR = _DEV_DIR / "configs"
MODELS_FILE = CONFIG_DIR / "models.json"

# 运行时状态：<development>/.runtime/active-model.json（不进 git）
RUNTIME_DIR = _DEV_DIR / ".runtime"
RUNTIME_STATE = RUNTIME_DIR / "active-model.json"

# sha256 缓存（算一次挺慢，1.7 GB 要几秒；按 路径+mtime 缓存）
_sha_cache = {}


# ── 运行时状态读写 ────────────────────────────────────────────────────────

def read_runtime_active():
    """
    读"当前模型"的运行时状态。

    返回 model_id 或 None（文件不存在 / 读不出来 / 内容不合法都算 None，
    让调用方退回注册表里的默认值 —— 状态文件坏了不该让服务起不来）。
    """
    if not RUNTIME_STATE.is_file():
        return None
    try:
        d = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"运行时状态文件读不出来（忽略，用注册表默认值）：{e}")
        return None
    if not isinstance(d, dict):
        return None
    v = d.get("active")
    return v if isinstance(v, str) and v else None


def write_runtime_active(model_id):
    """把"当前模型"写进运行时状态文件。返回 (是否成功, 错误信息)"""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE.write_text(
            json.dumps({
                "active": model_id,
                "switched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "_说明": [
                    "当前正在使用的模型。由控制层在模型切换成功后写入。",
                    "这个文件不进 git（.runtime/ 已被 .gitignore 覆盖）——",
                    "它是运行时状态，不是配置。",
                    "删掉它就等于恢复成 configs/models.json 里的 default_active。",
                ],
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True, None
    except OSError as e:
        return False, f"写入运行时状态失败：{e}"


# ── 注册表加载 ────────────────────────────────────────────────────────────

def load_registry():
    """
    读取模型注册表，并把"当前生效的模型"解析好放进 data["active"]。

    ⭐ 关键：读取时就把 .runtime/ 的状态合并进来，
       这样所有调用方（list_models / get_initial_active / ...）
       都不用改 —— 它们看到的 data["active"] 就是真正生效的那个。

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

    # 默认值：优先 default_active；兼容老注册表里的 active
    default = data.get("default_active") or data.get("active")
    if default and default not in seen:
        return None, f"default_active 指向不存在的模型 id：{default}"

    # ⭐ 解析真正生效的模型：
    #    .runtime 里的选择 > 注册表默认值 > 第一个模型
    runtime = read_runtime_active()
    if runtime and runtime not in seen:
        log(f"运行时状态里的 {runtime!r} 不在注册表里，忽略，改用默认值 {default!r}")
        runtime = None

    data["active"] = runtime or default or models[0]["id"]
    data["active_source"] = (
        "runtime" if runtime else ("registry" if default else "first_model")
    )
    # 统一：内部一律用 default_active 这个名字
    data["default_active"] = default or models[0]["id"]

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
    """启动时用哪个模型（运行时状态优先，其次注册表默认值）"""
    data, err = load_registry()
    if err:
        return None
    return data.get("active")


def set_active(model_id):
    """
    记录"现在用这个模型"。

    ⚠️ 写的是 .runtime/active-model.json，【不是】注册表。
       注册表是配置、要进 git；这个是运行时状态、不进 git。
       如果不小心写进注册表，每切一次模型 git 就脏一次。
    """
    data, err = load_registry()
    if err:
        return False, err
    if model_id not in {m["id"] for m in data["models"]}:
        return False, f"注册表里没有这个模型 id：{model_id}"
    return write_runtime_active(model_id)


def active_info():
    """给前端用：当前生效的模型 + 它是从哪来的 + 注册表默认值"""
    data, err = load_registry()
    if err:
        return {"active": None, "error": err}
    return {
        "active": data.get("active"),
        "active_source": data.get("active_source"),
        "default_active": data.get("default_active"),
        "runtime_state_file": str(RUNTIME_STATE),
    }
