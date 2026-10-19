#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slotfile.py —— 读写 llama.cpp 的 slot 保存文件

═══════════════════════════════════════════════════════════════════════════
 为什么需要这个模块
═══════════════════════════════════════════════════════════════════════════
 一个 KV 保存文件有几百 MiB，但它的【token 列表】在文件最前面，
 只有几十 KB。为了做"轻微改动"的前缀匹配，我们只需要 token 列表，
 不需要加载整个文件。

 文件格式（源码 llama-context.cpp:3378 state_seq_save_file）：
     [magic  u32][version u32][n_tokens u32][tokens: n × i32][KV 数据...]

 实测（本项目环境）：
     magic   = 0x67677371  ("GGSQ" 小端)
     version = 3
     一份 40 token 的文件：头部+token = 172 字节，总文件 4.1 MB
     → 只读头部，成本可忽略

 ⚠️ 实测发现的怪事：第一个 token 是 -1。它是某种哨兵值，
    做前缀比较时要跳过负数 token。
"""

import hashlib
import struct
from pathlib import Path

MAGIC = 0x67677371          # "GGSQ"
VERSION = 3


class BadSlotFile(Exception):
    pass


def read_header(path, max_tokens=200_000):
    """
    只读文件头，返回 (n_tokens, packed_list)。

    ⚠️ 重要：返回的列表【不是裸 token id】，而是 server_tokens::serialize()
       的打包结果。实测形状如 [1, 3338, 151644, 8948, 198, ...]，
       其中 1 是格式版本、3338 是 token 数、之后才是 token。
       所以：
         · 可以用它拿 n_tokens、校验文件完整性
         · 【不要】拿它做逐 token 前缀比较（会在第 2 个元素就分叉）
       前缀比较请用 prefix_blocks() + tokenize 接口。
    """
    p = Path(path)
    size = p.stat().st_size
    if size < 12:
        raise BadSlotFile(f"文件太小（{size} 字节）")

    with p.open("rb") as f:
        magic, version, n = struct.unpack("<III", f.read(12))
        if magic != MAGIC:
            raise BadSlotFile(f"magic 不匹配：0x{magic:08x}（期望 0x{MAGIC:08x}）")
        if version != VERSION:
            raise BadSlotFile(f"version 不匹配：{version}（期望 {VERSION}）")
        if n <= 0 or n > max_tokens:
            raise BadSlotFile(f"token 数异常：{n}")
        need = 12 + n * 4
        if need > size:
            raise BadSlotFile(f"token 数 {n} 与文件大小 {size} 不符")
        packed = list(struct.unpack(f"<{n}i", f.read(n * 4)))

    return n, packed


def read_tokens(path):
    return read_header(path)[1]


def common_prefix_len(a, b, skip_negative=True):
    """
    最长公共前缀长度（逐 token 精确比较）。
    ⚠️ 只对【裸 token 数组】有意义。
       不要拿它比较 .bin 文件里的 token —— 那是打包格式，见 read_header 的注释。
    """
    if skip_negative:
        a = [t for t in a if t >= 0]
        b = [t for t in b if t >= 0]
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


# ══════════════════════════════════════════════════════════════════════════
#  前缀指纹（块哈希）
#
#  ⚠️ 为什么不用 .bin 里的 token 做比较（踩过的坑）：
#     llama.cpp 存盘时写的是 server_tokens::serialize() 的结果，是一个
#     【打包二进制】：实测 v1 的头部是 [1, 3338, 151644, ...]，
#     其中 1 是版本、3338 是 token 数、151644 才是第一个真正的 token。
#     直接拿它做逐 token 比较，会在第 2 个元素就分叉，LCP 恒为 1。
#
#  正确做法：代理自己把前缀文本 tokenize，把 token 切成固定大小的块，
#  每块算一个哈希存进索引。比较时数"从头开始连续相同多少块"即可。
#  精度：块大小 128，误差 ±128 token，对 50% 这种阈值完全够用。
# ══════════════════════════════════════════════════════════════════════════
BLOCK = 128
MAX_BLOCKS = 128          # 最多 128 块 × 128 = 16384 token


def prefix_blocks(tokens, block=BLOCK, max_blocks=MAX_BLOCKS):
    """把 token 序列切成块，返回每块的短哈希列表。"""
    clean = [t for t in tokens if t >= 0][:block * max_blocks]
    out = []
    for i in range(0, len(clean), block):
        chunk = clean[i:i + block]
        out.append(hashlib.sha256(
            struct.pack(f"<{len(chunk)}i", *chunk)).hexdigest()[:12])
    return out


def matching_blocks(a, b):
    """从头开始连续相同的块数。"""
    n = 0
    for x, y in zip(a or [], b or []):
        if x != y:
            break
        n += 1
    return n


def estimate_lcp(a_blocks, b_blocks, block=BLOCK):
    """用块匹配估算公共前缀长度（向下取整到块边界）。"""
    return matching_blocks(a_blocks, b_blocks) * block


def validate(path):
    """返回 (ok, 说明)。用于 restore 前的自检与崩溃恢复。"""
    try:
        n, toks = read_header(path)
        real = Path(path).stat().st_size
        expect_min = 12 + n * 4
        if real < expect_min:
            return False, f"文件大小 {real} < 最少应有 {expect_min}"
        return True, f"{n} token / {real} 字节"
    except BadSlotFile as e:
        return False, str(e)
    except FileNotFoundError:
        return False, "文件不存在"
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        ok, msg = validate(f)
        print(f"{'✅' if ok else '❌'} {f}  {msg}")
        if ok:
            n, toks = read_header(f)
            clean = [t for t in toks if t >= 0]
            print(f"     前 10 个 token: {clean[:10]}")
            print(f"     指纹: {prefix_fingerprint(toks)}")
