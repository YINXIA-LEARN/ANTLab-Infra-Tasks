#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
files.py —— 可选输入文件的登记与读取

安全设计（这是本模块最重要的部分）：
    ⚠️ 接口绝不接受请求里的任意路径！
    前端只能传【文件 id】，真实路径由本模块从白名单目录里解析。
    否则就是一个任意文件读取漏洞（可以读 /etc/passwd、~/.ssh 等）。

白名单目录：
    ① 题包自带的输入文档    Basic Task/提示词与输入文档/输入文档/   （只读）
    ② 用户上传目录          <src>/uploads/                         （可写）

为什么还要上传目录：
    题目要求"系统能够读取【指定文件】"——不只是题包自带的那三个。
    以后要跑自己的资料，得能放进来。
    放在 src/ 下是因为 development/.gitignore 已覆盖 uploads/，不会误提交。
"""

import hashlib
import re
from pathlib import Path

from .logutil import log

# ── 目录 ───────────────────────────────────────────────────────────────────

PHASE1_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PHASE1_DIR / "uploads"

# 题包输入文档目录：从 src 往上找仓库根
def _find_task_input_dir():
    for up in (PHASE1_DIR, *PHASE1_DIR.parents):
        cand = up / "Basic Task" / "提示词与输入文档" / "输入文档"
        if cand.is_dir():
            return cand
    return None


TASK_INPUT_DIR = _find_task_input_dir()

# 允许的扩展名（题目输入是 .md/.py，但用户可能放别的文本）
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".py", ".json", ".jsonl",
                    ".csv", ".yaml", ".yml", ".log", ".ini", ".cfg", ".toml"}

MAX_FILE_BYTES = 8 * 1024 * 1024        # 单个文件上限 8 MiB


class FileError(Exception):
    """文件操作失败（找不到、不允许的格式、太大等）"""


# ── 列表 ───────────────────────────────────────────────────────────────────

def _describe(path, source, base_dir):
    """把一个文件描述成前端要的结构（不算 token，那个很慢，按需单独要）"""
    try:
        st = path.stat()
    except OSError as e:
        return None
    return {
        "id": _make_id(source, path.relative_to(base_dir)),
        "name": path.name,
        "rel": str(path.relative_to(base_dir)),
        "source": source,               # task=题包自带 / upload=用户上传
        "bytes": st.st_size,
        "suffix": path.suffix.lower(),
    }


def _make_id(source, rel):
    """
    id = 来源 + 相对路径。
    前端只能传回这个 id，真实路径由 resolve() 还原 —— 避免路径穿越。
    """
    return f"{source}:{rel.as_posix()}"


def list_files():
    """列出所有可选的输入文件"""
    out = []

    if TASK_INPUT_DIR and TASK_INPUT_DIR.is_dir():
        for p in sorted(TASK_INPUT_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
                d = _describe(p, "task", TASK_INPUT_DIR)
                if d:
                    out.append(d)

    if UPLOAD_DIR.is_dir():
        for p in sorted(UPLOAD_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
                d = _describe(p, "upload", UPLOAD_DIR)
                if d:
                    out.append(d)

    return out


# ── 解析（安全关键）────────────────────────────────────────────────────────

def resolve(file_id):
    """
    把 file_id 还原成 (真实路径, 来源, 基准目录)。

    ⚠️ 安全要点：
      · 只认 {source}:{rel} 这一种格式
      · rel 里不允许出现 ..（拒绝路径穿越）
      · 解析后必须仍在白名单目录内（二次确认，防符号链接绕过）
    """
    if not isinstance(file_id, str) or ":" not in file_id:
        raise FileError("文件 id 格式不对，应为 来源:相对路径")

    source, _, rel = file_id.partition(":")
    if source not in ("task", "upload"):
        raise FileError(f"未知来源：{source!r}")

    if not rel or rel.startswith(("/", "\\")):
        raise FileError("相对路径不能为空或以斜杠开头")

    # 显式拒绝 ..，不依赖后面的 resolve()
    parts = re.split(r"[/\\]", rel)
    if any(p in ("..", "") for p in parts if p != ""):
        raise FileError("相对路径里不允许出现 .. 或空段")

    base = TASK_INPUT_DIR if source == "task" else UPLOAD_DIR
    if base is None or not base.is_dir():
        raise FileError(f"来源目录不存在：{source}")

    target = (base / rel)
    try:
        real_base = base.resolve(strict=True)
        real_target = target.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise FileError(f"路径解析失败：{e}")

    # ⭐ 二次确认：解析后必须仍在基准目录内
    if real_base != real_target and real_base not in real_target.parents:
        raise FileError("路径越出了允许的目录")

    if not real_target.is_file():
        raise FileError("不是普通文件（可能是目录或链接）")

    if real_target.suffix.lower() not in ALLOWED_SUFFIXES:
        raise FileError(f"不支持的文件类型：{real_target.suffix}")

    st = real_target.stat()
    if st.st_size > MAX_FILE_BYTES:
        raise FileError(
            f"文件过大：{st.st_size} 字节，上限 {MAX_FILE_BYTES}")

    return real_target, source, base


def read(file_id):
    """
    读取文件。

    编码：先 UTF-8，失败回退 GBK，并【记录实际编码】——
    因为编码会影响 token 数，报告里必须能说明。
    """
    path, source, base = resolve(file_id)
    raw = path.read_bytes()

    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]

    encoding = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if encoding is None:
        raise FileError("无法解码（试过 utf-8 与 gbk）—— 可能是二进制文件")

    # 统一换行，保证可复现
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    return {
        "id": file_id,
        "name": path.name,
        "rel": str(path.relative_to(base)),
        "source": source,
        "path": str(path),
        "encoding": encoding,
        "bom": bom,
        "bytes": path.stat().st_size,
        "chars": len(text),
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "text": text,
    }


def dirs_info():
    return {
        "task_input_dir": str(TASK_INPUT_DIR) if TASK_INPUT_DIR else None,
        "upload_dir": str(UPLOAD_DIR),
        "upload_dir_exists": UPLOAD_DIR.is_dir(),
        "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
        "max_file_bytes": MAX_FILE_BYTES,
    }


def ensure_upload_dir():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR
