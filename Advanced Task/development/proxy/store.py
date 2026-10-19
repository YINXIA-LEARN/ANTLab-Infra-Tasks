#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store.py —— 三层存储管理：L2(tmpfs 内存) / L3(磁盘)

职责：
  · 文件放在哪一层（tier）
  · L2 → L3 的【整批搬迁】（用户要求：一口气转到磁盘）
  · L3 → L2 的搬回
  · L3 超配额时的淘汰
  · 全部用「复制 → 校验 → 删源」，不用 mv（崩溃时语义不确定）

设计要点（见计划书 §5、§6）：
  · L2 是 tmpfs（/dev/shm），文件真的住内存，配额用 df 可读
  · 搬一批（默认 30%），不是搬一条 —— 避免抖动
  · 冷却时间：两次搬家之间至少隔 N 秒
  · 引用计数 > 0 的条目跳过（正在被请求使用）
"""

import json
import os
import shutil
import time
from pathlib import Path


def dir_usage_bytes(path):
    """目录下所有文件的总字节数（不递归子目录以外的层级做多，够用）"""
    total = 0
    p = Path(path)
    if not p.is_dir():
        return 0
    try:
        for e in p.iterdir():
            if e.is_file():
                try:
                    total += e.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def fs_free_bytes(path):
    """文件系统剩余空间"""
    try:
        st = os.statvfs(str(path))
        return st.f_bavail * st.f_frsize
    except Exception:                                         # noqa: BLE001
        return 0


def copy_verify(src, dst):
    """
    复制并校验：大小一致且 sha256 一致才返回 True。
    ⚠️ 不用 shutil.move：跨文件系统的 move 失败时语义不确定，
       我们用「复制 → 校验 → 删源」，最坏情况是两边都有一份（可清理）。
    """
    import hashlib

    def h(p):
        hh = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                hh.update(chunk)
        return hh.hexdigest()

    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copyfile(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise IOError(f"复制后大小不一致: {src} → {tmp}")
    if h(tmp) != h(src):
        tmp.unlink(missing_ok=True)
        raise IOError(f"复制后 sha256 不一致: {src}")
    os.replace(tmp, dst)
    return True


class Store:
    """
    管理 L2 / L3 两层的文件。
    索引由调用方（proxy）持有；本类只负责文件与容量，
    并把「该更新索引了」的事实通过返回值/事件回调报出去。
    """

    def __init__(self, l2_dir, l3_dir, l2_quota, l3_quota,
                 l2_spill_ratio=0.30, l3_evict_ratio=0.20,
                 spill_cooldown_s=5.0, log=print):
        self.l2 = Path(l2_dir)
        self.l3 = Path(l3_dir)
        self.l2.mkdir(parents=True, exist_ok=True)
        self.l3.mkdir(parents=True, exist_ok=True)
        self.l2_quota = int(l2_quota)
        self.l3_quota = int(l3_quota)
        self.l2_spill_ratio = l2_spill_ratio
        self.l3_evict_ratio = l3_evict_ratio
        self.spill_cooldown_s = spill_cooldown_s
        self._last_spill = 0.0
        self.log = log

    # ── 路径 ──────────────────────────────────────────────────────────
    def path_in(self, tier, key):
        d = self.l2 if tier == "L2" else self.l3
        return d / f"{key}.bin"

    def exists_in(self, tier, key):
        p = self.path_in(tier, key)
        return p.is_file() and p.stat().st_size > 12

    def usage(self):
        return {
            "l2_used": dir_usage_bytes(self.l2),
            "l2_quota": self.l2_quota,
            "l2_free_fs": fs_free_bytes(self.l2),
            "l3_used": dir_usage_bytes(self.l3),
            "l3_quota": self.l3_quota,
            "l3_free_fs": fs_free_bytes(self.l3),
        }

    def can_fit_l2(self, nbytes):
        """
        能不能再放进 L2？
        同时看两个约束：配置配额，以及 tmpfs 的真实剩余空间。
        （tmpfs 的空间和内存共享，写满会让系统 OOM，所以必须双重检查）
        """
        u = self.usage()
        if u["l2_used"] + nbytes > self.l2_quota:
            return False
        if nbytes > u["l2_free_fs"] * 0.9:
            return False
        return True

    # ── 迁移：L2 → L3（整批）──────────────────────────────────────────
    def need_spill(self):
        return dir_usage_bytes(self.l2) > self.l2_quota

    def spill_to_l3(self, index, refcount=None, now=None, force=False):
        """
        把 L2 的一批条目搬到 L3。返回搬走的 key 列表。

        ⚠️ 算法要点（对应用户要求的"一口气转到磁盘"）：
          1. 目标释放量 = L2 配额 × spill_ratio（默认 30%）
             —— 一次搬够，避免"搬一条→立刻又被填满"的抖动
          2. 按 LRU（last_used_at 最小）排序挑选
          3. 引用计数 > 0 的跳过（正在被用）
          4. 逐个「复制→校验→删源」，成功后改 index 的 file_tier
          5. force=True 时忽略冷却 —— 用于"新文件放不下，必须立刻腾地方"
        """
        now = now or time.time()
        if not force and now - self._last_spill < self.spill_cooldown_s:
            return []

        target = int(self.l2_quota * self.l2_spill_ratio)
        refcount = refcount or {}

        # ⚠️ 用 file_tier 而不是 tier：
        #    tier 表示"在不在显存"，file_tier 表示"文件副本在哪一层"。
        #    早期版本把两者混成一个字段，导致这里永远找不到候选（spills 恒为 0）。
        cands = [e for e in index.entries.values()
                 if e.get("file_tier") == "L2"
                 and refcount.get(e["key"], 0) == 0
                 and self.exists_in("L2", e["key"])]
        cands.sort(key=lambda e: e.get("last_used_at") or 0)

        moved, freed = [], 0
        for e in cands:
            if freed >= target:
                break
            key = e["key"]
            src = self.path_in("L2", key)
            dst = self.path_in("L3", key)
            try:
                t0 = time.time()
                copy_verify(src, dst)
                size = dst.stat().st_size
                src.unlink(missing_ok=True)
                e["file_tier"] = "L3"
                e["migrated_at"] = now
                index.save()
                freed += size
                moved.append(key)
                self.log({
                    "event": "migration", "key": key, "from": "L2", "to": "L3",
                    "bytes": size, "ms": round((time.time() - t0) * 1000, 1),
                    "reason": "l2_quota",
                })
            except Exception as exc:                          # noqa: BLE001
                # 搬失败：保持原样（L2 那份还在），记事件，继续下一个
                self.log({"event": "migration_failed", "key": key,
                          "from": "L2", "to": "L3", "error": str(exc)[:200]})

        if moved:
            self._last_spill = now
            self.log({"event": "spill_done", "batch": len(moved),
                      "freed_bytes": freed, "target_bytes": target,
                      "l2_used": dir_usage_bytes(self.l2),
                      "l2_quota": self.l2_quota})
        return moved

    # ── 迁移：L3 → L2 ────────────────────────────────────────────────
    def promote_to_l2(self, index, key):
        """把一条从 L3 搬回 L2。空间不够返回 False。"""
        e = index.entries.get(key)
        if not e or e.get("file_tier") != "L3":
            return False
        src = self.path_in("L3", key)
        if not src.is_file():
            return False
        size = src.stat().st_size
        if not self.can_fit_l2(size):
            return False
        dst = self.path_in("L2", key)
        t0 = time.time()
        try:
            copy_verify(src, dst)
            src.unlink(missing_ok=True)
            e["file_tier"] = "L2"
            index.save()
            self.log({"event": "migration", "key": key, "from": "L3", "to": "L2",
                      "bytes": size, "ms": round((time.time() - t0) * 1000, 1),
                      "reason": "hit_promote"})
            return True
        except Exception as exc:                              # noqa: BLE001
            self.log({"event": "migration_failed", "key": key,
                      "from": "L3", "to": "L2", "error": str(exc)[:200]})
            return False

    # ── 淘汰：L3 超配额 ──────────────────────────────────────────────
    def evict_l3(self, index, refcount=None):
        """
        L3 超配额时，按 LRU 删一批。返回删掉的 key 列表。

        ⚠️ 释放目标要按【超出多少】算，不能只按固定比例：
           早期写成 target = 配额 × 20%，当超出量大于这个值时，
           删一条就停、退出后仍然超配额（单元测试抓到过）。
           正确做法：先补平超出量，再多删一点做余量。
        """
        used = dir_usage_bytes(self.l3)
        if used <= self.l3_quota:
            return []
        excess = used - self.l3_quota
        target = excess + int(self.l3_quota * self.l3_evict_ratio)
        refcount = refcount or {}
        cands = [e for e in index.entries.values()
                 if e.get("file_tier") == "L3"
                 and refcount.get(e["key"], 0) == 0
                 and self.exists_in("L3", e["key"])]
        cands.sort(key=lambda e: e.get("last_used_at") or 0)

        removed, freed = [], 0
        for e in cands:
            if freed >= target:
                break
            key = e["key"]
            p = self.path_in("L3", key)
            try:
                size = p.stat().st_size
                p.unlink()
                index.drop(key)
                index.save()
                freed += size
                removed.append(key)
                self.log({"event": "evict", "key": key, "tier": "L3",
                          "bytes": size, "reason": "l3_quota"})
            except Exception as exc:                          # noqa: BLE001
                self.log({"event": "evict_failed", "key": key,
                          "error": str(exc)[:200]})
        return removed

    # ── 崩溃恢复：以文件为准 ──────────────────────────────────────────
    def scan_files(self):
        """
        扫描两层的文件，返回 {key: (tier, bytes)}。
        启动时用它与索引对账（文件是权威，索引可以重建）。
        """
        out = {}
        for tier, d in (("L2", self.l2), ("L3", self.l3)):
            if not d.is_dir():
                continue
            for e in d.iterdir():
                if not e.is_file() or not e.name.endswith(".bin"):
                    continue
                key = e.stem
                try:
                    size = e.stat().st_size
                except OSError:
                    continue
                if size <= 12:        # 空文件/坏文件，当不存在
                    continue
                # L2 优先（同一 key 两层都有时取更近的）
                if key not in out or tier == "L2":
                    out[key] = (tier, size)
        return out

    def remove(self, tier, key):
        p = self.path_in(tier, key)
        try:
            p.unlink(missing_ok=True)
            return True
        except Exception:                                     # noqa: BLE001
            return False
