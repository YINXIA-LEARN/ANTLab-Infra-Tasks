#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_evict.py —— 淘汰策略的边界：配额、LRU 顺序、引用计数、只删能删的

断言什么
  1. 没超配额时不删任何东西
  2. 超了只删到「配额 × 比例」那么多，不全删
  3. 删的是最久没用的（LRU），不是随便挑
  4. 引用计数 > 0 的不删
  5. 只删 L3 层、只删文件真存在的条目
  6. 删完索引与文件保持一致（不留幽灵条目）
"""

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


def setup(d, quota, ratio=0.20):
    s = ST.Store(Path(d) / "L2", Path(d) / "L3", l2_quota=MiB,
                 l3_quota=quota, l3_evict_ratio=ratio,
                 spill_cooldown_s=0, log=lambda *_: None)
    ix = IX.Index(Path(d) / "kv_index.json")
    return s, ix


def put(s, ix, key, size, tier="L3", age=0.0):
    p = s.path_in(tier, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xab" * size)
    ix.add(key, tier=None, bytes=size, n_tokens=size // 100)
    e = ix.get(key)
    e["file_tier"] = tier
    e["last_used_at"] = time.time() - age       # age 越大越"老"
    return e


def t_below_quota():
    T.case("没超配额 → 一条都不删")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=10 * MiB)
        for i in range(3):
            put(s, ix, f"k{i}", 2 * MiB)
        used = ST.dir_usage_bytes(s.l3)
        T.check(used < 10 * MiB, f"用量 {used/MiB:.0f} MiB < 配额 10 MiB")
        removed = s.evict_l3(ix)
        T.eq(len(removed), 0, "没删任何东西")
        T.eq(len(ix.entries), 3, "索引条目一个没少")


def t_lru_order():
    T.case("超配额 → 按 LRU 删")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=4 * MiB)
        # 5 个 1 MiB，age 越大越老
        ages = {"oldest": 100, "older": 50, "mid": 10, "new": 1, "newest": 0}
        for k, age in ages.items():
            put(s, ix, k, MiB, age=age)
        T.check(ST.dir_usage_bytes(s.l3) > 4 * MiB, "已超配额")

        removed = s.evict_l3(ix)
        T.check(len(removed) > 0, f"删了 {len(removed)} 条")
        T.eq(removed[0], "oldest", "第一个删的是最久没用的")
        T.check("newest" not in removed, "最新的没被删")
        # 目标 = 配额 × 0.20 = 0.8 MiB → 删 1 条（1 MiB）就够
        T.check(len(removed) <= 2, f"只删到够为止（删了 {len(removed)} 条）")


def t_refcount():
    T.case("引用计数 > 0 的不删")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=3 * MiB)
        # 4 个 1 MiB → 共 4 MiB > 配额 3 MiB
        for k, age in (("busy", 100), ("idle", 50), ("x", 10), ("y", 5)):
            put(s, ix, k, MiB, age=age)
        used = ST.dir_usage_bytes(s.l3)
        T.check(used > 3 * MiB, f"确实超配额（{used/MiB:.0f} > 3）")
        removed = s.evict_l3(ix, refcount={"busy": 2})
        T.check("busy" not in removed, "被占用的 busy 没删")
        T.check(len(removed) > 0, "删了别的")
        T.check(s.path_in("L3", "busy").is_file(), "busy 的文件还在")
        T.check(ST.dir_usage_bytes(s.l3) <= 3 * MiB,
                "删完回到配额内")


def t_only_l3():
    T.case("只删 L3 层，不碰其他层")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=2 * MiB)
        put(s, ix, "in_l2", 3 * MiB, tier="L2")     # 在 L2，不该删
        put(s, ix, "in_l3", 3 * MiB, tier="L3")
        removed = s.evict_l3(ix)
        T.check("in_l2" not in removed, "L2 的条目不会被 L3 淘汰动到")
        T.check(s.path_in("L2", "in_l2").is_file(), "L2 的文件还在")


def t_ghost():
    T.case("文件已被外部删掉 → 不留幽灵条目")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=2 * MiB)
        put(s, ix, "gone", 2 * MiB)
        put(s, ix, "real", 2 * MiB)
        s.path_in("L3", "gone").unlink()            # 模拟外部删除
        removed = s.evict_l3(ix)
        T.check("gone" not in removed, "文件不在的不算被删")
        T.check("gone" in ix.entries, "它的索引条目暂时还在（等对账处理）")
        T.check(isinstance(removed, list), "不崩，返回列表")


def t_consistency():
    T.case("删完索引与文件一致")
    with tempfile.TemporaryDirectory() as d:
        s, ix = setup(d, quota=3 * MiB)
        for i in range(6):
            put(s, ix, f"k{i}", MiB, age=i)
        removed = s.evict_l3(ix)
        for k in removed:
            T.check(ix.get(k) is None, f"{k} 的索引已删")
            T.check(not s.path_in("L3", k).exists(), f"{k} 的文件已删")
        remain = [k for k in ix.entries if ix.get(k).get("file_tier") == "L3"]
        for k in remain:
            T.check(s.path_in("L3", k).is_file(), f"保留的 {k} 文件确实在")


def main():
    for fn in (t_below_quota, t_lru_order, t_refcount, t_only_l3, t_ghost,
               t_consistency):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
