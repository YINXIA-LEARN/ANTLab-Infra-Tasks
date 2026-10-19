#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
index.py —— 缓存索引

特点（见计划书 §6.2、§7.2）：
  · 原子写（临时文件 + os.replace）—— 崩溃不会留半个 JSON
  · 可从文件重建 —— 索引损坏不代表缓存全丢
  · 每个条目记录 tier / slot / LRU / 使用次数 / 分段指纹
"""

import json
import os
import time
from pathlib import Path


class Index:
    def __init__(self, path, config_fp="", log=print):
        self.path = Path(path)
        self.config_fp = config_fp
        self.log = log
        self.entries = {}

    # ── 持久化 ────────────────────────────────────────────────────────
    def save(self):
        """原子写：先写 .tmp 再 replace。"""
        data = {
            "version": 1,
            "config": {"server_fingerprint": self.config_fp},
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "entries": self.entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self):
        if not self.path.is_file():
            self.entries = {}
            return False
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = d.get("entries") or {}
            return True
        except Exception as e:                                # noqa: BLE001
            self.log({"event": "index_load_failed",
                      "error": f"{type(e).__name__}: {e}"})
            self.entries = {}
            return False

    # ── 查询 ──────────────────────────────────────────────────────────
    def get(self, key):
        return self.entries.get(key)

    def by_slot(self, slot):
        for e in self.entries.values():
            if e.get("tier") == "L1" and e.get("slot") == slot:
                return e
        return None

    def touch(self, key, now=None):
        """更新 LRU 与使用次数"""
        e = self.entries.get(key)
        if not e:
            return
        now = now or time.time()
        e["last_used_at"] = now
        e["use_count"] = int(e.get("use_count") or 0) + 1

    def add(self, key, **fields):
        """
        新建条目。

        ⚠️ 两个位置字段含义不同，别混：
             tier       只表示【在不在显存】："L1" 或 None
             file_tier  文件副本在哪一层：  "L2" / "L3" / None
        早期版本只有一个 tier，把文件层也塞进去，导致
          · 对账时"文件没了"的条目识别不出来
          · 状态面板把"仅在文件"的条目错显示成 L2/L3
        """
        now = time.time()
        e = {
            "key": key,
            "doc": fields.get("doc"),
            "n_tokens": fields.get("n_tokens"),
            "bytes": fields.get("bytes"),
            "tier": fields.get("tier"),
            "slot": fields.get("slot"),
            "file_tier": fields.get("file_tier"),
            "fp": fields.get("fp"),
            "prefix_blocks": fields.get("prefix_blocks") or [],
            "created_at": now,
            "last_used_at": now,
            "use_count": 1,
            "save_ms": fields.get("save_ms"),
        }
        self.entries[key] = e
        return e

    def drop(self, key):
        return self.entries.pop(key, None)

    # ── 崩溃恢复：以文件为准 ──────────────────────────────────────────
    def reconcile(self, scan, token_reader=None):
        """
        对账：scan = {key: (tier, bytes)}（来自 store.scan_files()）

        规则：
          · 文件在、索引也在  → 以文件的 tier 为准（文件是权威）
          · 文件在、索引没有  → 补登记（可能是崩溃前刚写完文件）
          · 索引在、文件不在  → 改判为 missing（调用方决定怎么处理）
        """
        added, fixed, missing = 0, 0, 0
        for key, (tier, size) in scan.items():
            e = self.entries.get(key)
            if e is None:
                e = self.add(key, file_tier=tier, bytes=size,
                             doc="(recovered)", n_tokens=None,
                             fp=self.config_fp)
                if token_reader:
                    try:
                        n, _toks = token_reader(key)
                        e["n_tokens"] = n
                    except Exception:                         # noqa: BLE001
                        pass
                added += 1
            else:
                if e.get("file_tier") != tier or e.get("bytes") != size:
                    e["file_tier"] = tier
                    e["bytes"] = size
                    fixed += 1
                # 进程刚启动，显存里什么都没有
                if e.get("tier") == "L1":
                    e["tier"] = None
                    e["slot"] = None

        for key, e in list(self.entries.items()):
            # 兼容老数据：把误写进 tier 的文件层搬到 file_tier
            if e.get("tier") in ("L2", "L3"):
                e["file_tier"] = e.get("file_tier") or e["tier"]
                e["tier"] = None
            # 进程重启后显存清空 → 只剩文件层；文件也不在就是真的没了
            if key not in scan:
                e["file_tier"] = None
                e["slot"] = None
                e["tier"] = "missing"
                missing += 1
            elif e.get("tier") == "L1":
                # 文件在，但显存已清 → 降级到文件层
                e["tier"] = None
                e["slot"] = None

        self.save()
        return {"added": added, "fixed": fixed, "missing": missing}

    # ── 统计 ──────────────────────────────────────────────────────────
    def stats(self):
        by_tier = {}
        for e in self.entries.values():
            t = e.get("tier") or "?"
            by_tier[t] = by_tier.get(t, 0) + 1
        return {"total": len(self.entries), "by_tier": by_tier}
