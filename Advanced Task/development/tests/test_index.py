#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_index.py —— 索引：原子写、崩溃重建、tier 语义

断言什么
  1. 写入后能读回（持久化正确）
  2. 写的过程用「临时文件 + replace」→ 不会留下半个 JSON
  3. 对账（reconcile）：文件在/索引没有 → 补登记；索引有/文件没了 → 标失效
  4. ⚠️ tier 只表示「在不在显存」，文件层在 file_tier —— 恢复出来的条目
     不能把文件层写进 tier（这个 bug 犯过一次）
  5. LRU 计数与 drop
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

sys.path.insert(0, str(T.DEV / "proxy"))
import index as IX                                           # noqa: E402


def t_roundtrip():
    T.case("持久化往返")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "kv_index.json"
        ix = IX.Index(p, config_fp="fp-A")
        ix.add("aaaa", tier="L1", slot=2, n_tokens=1000, bytes=123)
        ix.save()
        T.check(p.is_file(), "文件已写出")

        ix2 = IX.Index(p, config_fp="fp-A")
        T.check(ix2.load(), "能读回")
        e = ix2.get("aaaa")
        T.check(e is not None, "条目还在")
        T.eq(e["tier"], "L1", "tier 保留")
        T.eq(e["slot"], 2, "slot 保留")
        T.eq(e["n_tokens"], 1000, "token 数保留")
        T.eq(ix2.config_fp, "fp-A", "配置指纹保留在构造参数里")


def t_atomic():
    T.case("原子写：不留半个 JSON")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "kv_index.json"
        ix = IX.Index(p)
        ix.add("k1")
        ix.save()
        T.check(not (Path(str(p) + ".tmp")).exists(),
                "写完没有残留 .tmp 文件")
        # 模拟"写到一半崩溃"：先放一个坏 JSON，再正常 save 应覆盖它
        p.write_text('{"version": 1, "entries": {', encoding="utf-8")
        try:
            json.loads(p.read_text(encoding="utf-8"))
            T.check(False, "坏 JSON 居然能解析")
        except Exception:                                     # noqa: BLE001
            T.check(True, "确认文件此刻是坏的")
        ix.save()
        json.loads(p.read_text(encoding="utf-8"))
        T.check(True, "再 save 之后文件恢复合法")


def t_load_missing():
    T.case("索引文件不存在时的行为")
    with tempfile.TemporaryDirectory() as d:
        ix = IX.Index(Path(d) / "nope.json")
        T.check(not ix.load(), "load 返回 False")
        T.eq(ix.entries, {}, "entries 是空字典（不崩）")


def t_reconcile():
    T.case("对账：以文件为准重建索引")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "kv_index.json"
        ix = IX.Index(p)
        # 索引里有 f1（L2），但磁盘上还有 f2 没登记，f1 的文件其实不存在
        ix.add("f1", tier="L2", bytes=111, n_tokens=10)
        ix.add("f2", tier="L1", slot=1, bytes=222, n_tokens=20)
        ix.save()

        scan = {"f2": ("L2", 222), "f3": ("L3", 333)}
        rep = ix.reconcile(scan)
        T.eq(rep["added"], 1, "f3 被补登记")
        T.eq(rep["missing"], 1, "f1 文件不在 → 标失效")
        T.check(ix.get("f3") is not None, "补登记的条目存在")
        T.eq(ix.get("f1")["tier"], "missing", "f1 标为 missing")

        # ⚠️ 关键：文件层必须进 file_tier，不能进 tier
        e3 = ix.get("f3")
        T.eq(e3["tier"], None, "补登记条目的 tier 是 None（不在显存）")
        T.eq(e3["file_tier"], "L3", "文件层写进了 file_tier")
        e2 = ix.get("f2")
        T.eq(e2["tier"], None, "原本 L1 的条目在重启后降级（tier=None）")
        T.eq(e2["file_tier"], "L2", "它的文件层是 L2")
        T.eq(e2["slot"], None, "slot 被清空")


def t_lru_and_drop():
    T.case("LRU 计数与删除")
    with tempfile.TemporaryDirectory() as d:
        ix = IX.Index(Path(d) / "kv_index.json")
        ix.add("a")
        ea = ix.get("a")
        T.eq(ea["use_count"], 1, "新建时 use_count=1")
        t0 = ea["last_used_at"]
        time.sleep(0.01)
        ix.touch("a")
        T.eq(ea["use_count"], 2, "touch 后 +1")
        T.check(ea["last_used_at"] > t0, "last_used_at 被刷新")

        ix.add("b")
        T.eq(len(ix.entries), 2, "两个条目")
        dropped = ix.drop("b")
        T.check(dropped is not None, "drop 返回被删的条目")
        T.eq(len(ix.entries), 1, "只剩一个")
        T.check(ix.drop("zzz") is None, "删不存在的返回 None")


def t_stats():
    T.case("统计")
    with tempfile.TemporaryDirectory() as d:
        ix = IX.Index(Path(d) / "kv_index.json")
        ix.add("a", tier="L1", slot=0)
        ix.add("b", tier=None)
        ix.add("c", tier=None)
        s = ix.stats()
        T.eq(s["total"], 3, "总数 3")
        T.eq(s["by_tier"].get("L1"), 1, "L1 有一个")
        T.eq(s["by_tier"].get("?"), 2, "没有 tier 的记为 ?")


def main():
    for fn in (t_roundtrip, t_atomic, t_load_missing, t_reconcile,
               t_lru_and_drop, t_stats):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
