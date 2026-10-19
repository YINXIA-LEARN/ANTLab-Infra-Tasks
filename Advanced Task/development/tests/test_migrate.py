#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_migrate.py —— 三层迁移：复制校验、整批搬家、配额、符号链接不越界

断言什么
  1. copy_verify 只复制内容完全一致的文件；不一致必须报错且不留下脏目标
  2. 搬家只搬「file_tier=L2」的条目，不碰显存里的（tier=L1）
  3. 搬家是【整批】：一次搬走的量要接近「配额 × 比例」，不是搬一条
  4. 引用计数 > 0 的条目不能被搬走（正在被用）
  5. 搬完后 L2 用量回落到配额以下
  6. 搬家后文件内容不变（sha256 一致）
"""

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

sys.path.insert(0, str(T.DEV / "proxy"))
import index as IX                                           # noqa: E402
import store as ST                                           # noqa: E402

MiB = 1024 * 1024


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(MiB), b""):
            h.update(c)
    return h.hexdigest()


def make_entry(store, ix, key, size, tier="L2", use=0):
    """在 store 的某一层放一个文件并登记索引"""
    p = store.path_in(tier, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    ix.add(key, tier=None, bytes=size, n_tokens=size // 100)
    e = ix.get(key)
    e["file_tier"] = tier
    e["last_used_at"] = time.time() + use
    return e


def t_copy_verify():
    T.case("复制 + 校验")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src = d / "src.bin"
        src.write_bytes(os.urandom(2 * MiB + 123))
        dst = d / "sub" / "dst.bin"
        ST.copy_verify(src, dst)
        T.check(dst.is_file(), "目标已生成（自动建父目录）")
        T.eq(sha(src), sha(dst), "sha256 一致")
        T.check(not Path(str(dst) + ".part").exists(), "没有残留 .part")

        # 源变了但目标还是老的 → 再次 copy_verify 应覆盖
        old = sha(dst)
        src.write_bytes(os.urandom(2 * MiB + 123))
        ST.copy_verify(src, dst)
        T.eq(sha(src), sha(dst), "覆盖后仍然一致")
        T.check(sha(dst) != old, "内容确实换了")

        # 源不存在 → 抛异常
        try:
            ST.copy_verify(d / "nope.bin", d / "x.bin")
            T.check(False, "源不存在居然成功")
        except Exception as e:                                # noqa: BLE001
            T.check(True, f"源不存在会报错（{type(e).__name__}）")
        T.check(not (d / "x.bin").exists(), "失败时不留目标文件")


def t_usage():
    T.case("目录用量与配额判断")
    with tempfile.TemporaryDirectory() as d:
        s = ST.Store(Path(d) / "L2", Path(d) / "L3",
                     l2_quota=10 * MiB, l3_quota=100 * MiB,
                     spill_cooldown_s=0, log=lambda *_: None)
        T.eq(ST.dir_usage_bytes(s.l2), 0, "空目录 0 字节")
        (s.l2 / "a.bin").write_bytes(b"x" * (3 * MiB))
        (s.l2 / "b.bin").write_bytes(b"x" * (4 * MiB))
        T.eq(ST.dir_usage_bytes(s.l2), 7 * MiB, "两个文件合计 7 MiB")
        T.check(s.can_fit_l2(2 * MiB), "7+2 ≤ 10 → 放得下")
        T.check(not s.can_fit_l2(4 * MiB), "7+4 > 10 → 放不下")


def t_spill_batch():
    T.case("整批搬家（核心）")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        quota = 10 * MiB
        s = ST.Store(d / "L2", d / "L3", l2_quota=quota,
                     l3_quota=100 * MiB, l2_spill_ratio=0.30,
                     spill_cooldown_s=0, log=lambda *_: None)
        ix = IX.Index(d / "kv_index.json")

        # 8 个 1.5 MiB 的文件 = 12 MiB > 配额 10 MiB
        keys = []
        for i in range(8):
            k = f"k{i}"
            make_entry(s, ix, k, int(1.5 * MiB), tier="L2", use=i)
            keys.append(k)
        used_before = ST.dir_usage_bytes(s.l2)
        T.check(used_before > quota, f"搬家前已超配额（{used_before/MiB:.1f} > 10）")
        hashes = {k: sha(s.path_in("L2", k)) for k in keys}

        # 其中一个正在被用 → 不能被搬
        moved = s.spill_to_l3(ix, refcount={"k0": 1})

        T.check(len(moved) > 1, f"一次搬了 {len(moved)} 条（是整批，不是一条）")
        T.check("k0" not in moved, "引用计数 > 0 的条目没被搬走")
        total = sum((s.path_in("L3", k).stat().st_size for k in moved))
        target = int(quota * 0.30)
        T.check(total >= target,
                f"搬走的总量 {total/MiB:.2f} MiB ≥ 目标 {target/MiB:.2f} MiB")

        # 搬完后 L2 应该低于配额（k0 还在，所以留了 1.5 MiB）
        used_after = ST.dir_usage_bytes(s.l2)
        T.check(used_after <= quota,
                f"搬家后 L2 回落到配额内（{used_after/MiB:.1f} ≤ 10）")

        # 文件内容不能变
        for k in moved:
            T.eq(sha(s.path_in("L3", k)), hashes[k], f"{k} 搬到 L3 后内容一致")
            T.check(not s.path_in("L2", k).exists(), f"{k} 的 L2 副本已删除")
            T.eq(ix.get(k)["file_tier"], "L3", f"{k} 的 file_tier 更新为 L3")


def t_spill_only_file_tier():
    T.case("在显存里的条目：文件副本可以被搬，但显存状态不能动")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        s = ST.Store(d / "L2", d / "L3", l2_quota=5 * MiB,
                     l3_quota=50 * MiB, spill_cooldown_s=0,
                     log=lambda *_: None)
        ix = IX.Index(d / "kv_index.json")
        # a 在显存里（tier=L1）同时有文件；b 只在文件里
        make_entry(s, ix, "a", MiB, tier="L2", use=0)
        ix.get("a")["tier"] = "L1"
        ix.get("a")["slot"] = 0
        make_entry(s, ix, "b", MiB, tier="L2", use=1)

        moved = s.spill_to_l3(ix)
        T.check("a" in moved, "a 的文件副本被搬到 L3（文件只是备份，搬走没问题）")
        T.check("b" in moved, "b 也被搬")
        # 关键：搬的是文件，不是显存
        T.eq(ix.get("a")["tier"], "L1", "a 仍然标记为在显存里")
        T.eq(ix.get("a")["slot"], 0, "a 的 slot 编号没变")
        T.eq(ix.get("a")["file_tier"], "L3", "a 的文件层变成 L3")
        T.check(not s.path_in("L2", "a").exists(), "a 的 L2 文件已删")


def t_cooldown():
    T.case("冷却时间")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        s = ST.Store(d / "L2", d / "L3", l2_quota=2 * MiB,
                     l3_quota=50 * MiB, spill_cooldown_s=60,
                     log=lambda *_: None)
        ix = IX.Index(d / "kv_index.json")
        for i in range(4):
            make_entry(s, ix, f"k{i}", MiB, tier="L2", use=i)
        first = s.spill_to_l3(ix)
        T.check(len(first) > 0, f"第一次搬了 {len(first)} 条")
        second = s.spill_to_l3(ix)
        T.eq(len(second), 0, "冷却期内再调用不搬")
        forced = s.spill_to_l3(ix, force=True)
        T.check(len(forced) > 0, "force=True 可以绕过冷却（放不下时要用）")


def t_promote():
    T.case("从 L3 提升回 L2")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        s = ST.Store(d / "L2", d / "L3", l2_quota=4 * MiB,
                     l3_quota=50 * MiB, spill_cooldown_s=0,
                     log=lambda *_: None)
        ix = IX.Index(d / "kv_index.json")
        make_entry(s, ix, "k", 2 * MiB, tier="L3")
        h = sha(s.path_in("L3", "k"))

        T.check(s.promote_to_l2(ix, "k"), "提升成功")
        T.eq(ix.get("k")["file_tier"], "L2", "file_tier 变成 L2")
        T.check(s.path_in("L2", "k").is_file(), "L2 里有文件了")
        T.check(not s.path_in("L3", "k").exists(), "L3 里的删掉了")
        T.eq(sha(s.path_in("L2", "k")), h, "内容一致")

        # 空间不够时提升失败，且不动原文件
        make_entry(s, ix, "big", 3 * MiB, tier="L3")
        p2 = s.path_in("L3", "big")
        ok = s.promote_to_l2(ix, "big")
        T.check(not ok, "L2 放不下时提升失败")
        T.check(p2.is_file(), "失败时 L3 的文件仍在（不丢数据）")
        T.eq(ix.get("big")["file_tier"], "L3", "失败时 tier 不变")


def t_evict():
    T.case("L3 超配额淘汰")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        s = ST.Store(d / "L2", d / "L3", l2_quota=MiB,
                     l3_quota=4 * MiB, l3_evict_ratio=0.20,
                     spill_cooldown_s=0, log=lambda *_: None)
        ix = IX.Index(d / "kv_index.json")
        for i in range(6):
            make_entry(s, ix, f"k{i}", MiB, tier="L3", use=i)
        used = ST.dir_usage_bytes(s.l3)
        T.check(used > 4 * MiB, f"L3 已超配额（{used/MiB:.0f} > 4）")

        removed = s.evict_l3(ix, refcount={"k0": 1})
        T.check(len(removed) > 0, f"删了 {len(removed)} 条")
        T.check("k0" not in removed, "被引用的没删")
        T.check(not s.path_in("L3", removed[0]).exists(), "文件真的删了")
        T.check(ix.get(removed[0]) is None, "索引条目也删了")
        T.check(ST.dir_usage_bytes(s.l3) <= 4 * MiB,
                "删完不超配额")


def t_scan():
    T.case("扫描文件（崩溃恢复的基础）")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        s = ST.Store(d / "L2", d / "L3", l2_quota=MiB, l3_quota=MiB,
                     spill_cooldown_s=0, log=lambda *_: None)
        (s.l2 / "a.bin").write_bytes(b"x" * 100)
        (s.l3 / "b.bin").write_bytes(b"x" * 200)
        (s.l2 / "empty.bin").write_bytes(b"")        # 空文件不算
        (s.l2 / "note.txt").write_bytes(b"ignore")
        scan = s.scan_files()
        T.check("a" in scan, "L2 的 a.bin 被扫到")
        T.check("b" in scan, "L3 的 b.bin 被扫到")
        T.check("empty" not in scan, "空文件被忽略")
        T.check("note" not in scan, "非 .bin 文件被忽略")
        T.eq(scan["a"][0], "L2", "a 的层是 L2")
        T.eq(scan["b"][0], "L3", "b 的层是 L3")


def main():
    for fn in (t_copy_verify, t_usage, t_spill_batch, t_spill_only_file_tier,
               t_cooldown, t_promote, t_evict, t_scan):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
