#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_slotfile.py —— .bin 文件头与前缀块指纹

断言什么
  1. 文件格式：12 字节头（magic/version/n_tokens）+ n×4 字节，与源码一致
  2. 魔数就是 GGUF 家族里的 "GGSQ"（0x67677371），版本 3
  3. 坏文件（太小 / 魔数错 / token 数离谱）要被识别出来，不能默默通过
  4. 前缀块指纹：相同前缀 → 同样的块哈希；不同前缀 → 不同的块哈希
  5. ⚠️ .bin 里的 token 区是【打包格式】，不能当裸 token 用 —— 用测试固化这个事实
"""

import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

sys.path.insert(0, str(T.DEV / "proxy"))
import slotfile as SF                                        # noqa: E402


def write_bin(path, n_tokens, packed=None, kv_bytes=64, magic=None,
              version=None):
    """按源码格式造一个假的 save 文件"""
    magic = SF.MAGIC if magic is None else magic
    version = SF.VERSION if version is None else version
    packed = packed if packed is not None else list(range(n_tokens))
    with open(path, "wb") as f:
        f.write(struct.pack("<III", magic, version, n_tokens))
        f.write(struct.pack(f"<{len(packed)}i", *packed))
        f.write(b"\x00" * kv_bytes)
    return path


def t_format():
    T.case("文件格式与魔数")
    T.eq(SF.MAGIC, 0x67677371, "魔数 = 0x67677371（'GGSQ' 小端）")
    T.eq(SF.VERSION, 3, "版本 = 3（源码 llama.h 里 LLAMA_STATE_SEQ_VERSION）")
    with tempfile.TemporaryDirectory() as d:
        p = write_bin(os.path.join(d, "a.bin"), 40, kv_bytes=1000)
        n, packed = SF.read_header(p)
        T.eq(n, 40, "读出的 token 数正确")
        T.eq(len(packed), 40, "打包区长度 = n_tokens")
        ok, msg = SF.validate(p)
        T.check(ok, f"validate 通过（{msg}）")
        # 文件总大小 = 12 + 40*4 + 1000
        T.eq(Path(p).stat().st_size, 12 + 40 * 4 + 1000,
             "总大小 = 12 + n*4 + KV 字节")


def t_packed_not_raw():
    T.case("⚠️ 固化事实：token 区是打包格式，不是裸 token")
    with tempfile.TemporaryDirectory() as d:
        # 模拟实测形状：[版本, token数, 真token...]
        packed = [1, 5, 151644, 8948, 198, 105043, 103149]
        p = write_bin(os.path.join(d, "b.bin"), 7, packed=packed)
        n, got = SF.read_header(p)
        T.eq(got, packed, "读出来就是写入的打包序列")
        T.eq(got[1], 5, "第 2 个元素是 token 数（不是 token）")
        # 这就是为什么不能拿它做前缀比较：
        T.eq(SF.common_prefix_len([1, 5, 151644], [1, 6, 151644]), 1,
             "两份不同长度的文件会在第 2 个元素分叉 → LCP 恒为 1")


def t_bad_files():
    T.case("坏文件要被识别出来")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tiny.bin")
        Path(p).write_bytes(b"\x00" * 4)
        ok, msg = SF.validate(p)
        T.check(not ok and "太小" in msg, f"文件太小被拒（{msg}）")

        p = write_bin(os.path.join(d, "magic.bin"), 10, magic=0xDEADBEEF)
        ok, msg = SF.validate(p)
        T.check(not ok and "magic" in msg, f"魔数错被拒（{msg}）")

        p = write_bin(os.path.join(d, "ver.bin"), 10, version=99)
        ok, msg = SF.validate(p)
        T.check(not ok and "version" in msg, f"版本错被拒（{msg}）")

        # 声称 1000 个 token 但文件只有几十字节
        p = os.path.join(d, "short.bin")
        with open(p, "wb") as f:
            f.write(struct.pack("<III", SF.MAGIC, SF.VERSION, 1000))
            f.write(b"\x00" * 8)
        ok, msg = SF.validate(p)
        T.check(not ok, f"声称的 token 数与文件大小不符被拒（{msg}）")

        ok, msg = SF.validate(os.path.join(d, "missing.bin"))
        T.check(not ok and "不存在" in msg, f"文件不存在被识别（{msg}）")


def t_blocks():
    T.case("前缀块指纹")
    a = list(range(1000))
    b = list(range(500)) + list(range(9000, 9500))   # 前 500 相同
    c = list(range(1000))                             # 与 a 完全相同

    ba, bb, bc = SF.prefix_blocks(a), SF.prefix_blocks(b), SF.prefix_blocks(c)
    T.eq(len(ba), 8, "1000 token / 128 = 8 个块（向上取整）")
    T.eq(SF.matching_blocks(ba, bc), 8, "完全相同 → 8 块全匹配")
    lat = SF.estimate_lcp(ba, bb)
    # 前 500 个相同 → 128*3 = 384 整块相同，第 4 块（384-511）已不同
    T.eq(lat, 384, "前 500 相同 → 估算 384（向下取整到块边界）")
    T.check(abs(lat - 500) <= SF.BLOCK,
            f"估算误差 ≤ 块大小 {SF.BLOCK}（实测差 {abs(lat-500)}）")
    T.eq(SF.matching_blocks([], ba), 0, "空指纹匹配 0 块")
    T.eq(SF.estimate_lcp(None, ba), 0, "None 也安全")


def t_blocks_threshold():
    T.case("阈值判定：轻微改动 vs 大改")
    # 模拟 v1/v2：3288 token 的前缀，末 50 个 token 不同
    v1 = list(range(3288))
    v2 = list(range(3238)) + list(range(50000, 50050))
    lcp = SF.estimate_lcp(SF.prefix_blocks(v1), SF.prefix_blocks(v2))
    ratio = lcp / len(v2)
    T.check(ratio >= 0.5, f"v1→v2 的估算比例 {ratio:.3f} ≥ 0.5 → 判定为轻微改动")

    # 完全不同
    x = list(range(1000))
    y = list(range(9000, 10000))
    lcp2 = SF.estimate_lcp(SF.prefix_blocks(x), SF.prefix_blocks(y))
    T.eq(lcp2, 0, "完全不同 → 0 块匹配")


def t_common_prefix():
    T.case("裸 token 的精确 LCP")
    T.eq(SF.common_prefix_len([1, 2, 3, 4], [1, 2, 9, 4]), 2, "前 2 个相同")
    T.eq(SF.common_prefix_len([1, 2], [1, 2, 3]), 2, "短的是长的前缀")
    T.eq(SF.common_prefix_len([-1, 1, 2], [1, 2]), 2,
         "跳过负数哨兵后比较")
    T.eq(SF.common_prefix_len([], [1]), 0, "空序列")


def main():
    for fn in (t_format, t_packed_not_raw, t_bad_files, t_blocks,
               t_blocks_threshold, t_common_prefix):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
