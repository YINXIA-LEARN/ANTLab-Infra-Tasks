#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxy.py —— KV 缓存代理（进阶项目主程序）

═══════════════════════════════════════════════════════════════════════════
 它在干什么（一句话）
═══════════════════════════════════════════════════════════════════════════
 夹在客户端和 llama-server 之间，把"文档的 KV"当成可复用的资产：
   命中  → 直接把缓存恢复进某个 slot，只算问题部分
   未命中 → 转发给 llama-server 算，算完把 KV 存下来

═══════════════════════════════════════════════════════════════════════════
 三层模型（重要）
═══════════════════════════════════════════════════════════════════════════
 每个缓存条目有两个独立的位置：
     tier      它在不在显存里（"L1" 或 None）
     slot      在哪个槽（tier=="L1" 时有效）
     file_tier 文件副本在哪一层（"L2" / "L3" / None）

 为什么分开：save 之后槽里的 KV【还在】。
   v1 把它们混成一个 tier，结果 save 后误判"槽空了"，
   同一个文档被复制到两个槽里（冒烟测试实测到：alpha 占了 slot 0 和 1）。
   分开之后：
     · 槽里有 + 有文件备份  → tier=L1, file_tier=L2   （最省，优先走这里）
     · 只有文件            → tier=None, file_tier=L2   （需要 restore）

═══════════════════════════════════════════════════════════════════════════
 三条铁律（实测得出，见计划书 §2、§6）
═══════════════════════════════════════════════════════════════════════════
  R1  save → 更新索引 → erase，顺序不可颠倒
  R2  搬家用「复制 → 校验 → 删源」，不用 mv
  R3  索引原子写（temp + os.replace）

═══════════════════════════════════════════════════════════════════════════
 请求约定
═══════════════════════════════════════════════════════════════════════════
  messages = [ {system}, {user: 文档全文}, {user: 问题} ]
                      ↑ 前缀（要被缓存的）   ↑ 每次都变
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import index as index_mod                                   # noqa: E402
import slotfile                                             # noqa: E402
import store as store_mod                                   # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
#  上游客户端
# ══════════════════════════════════════════════════════════════════════════
class Upstream:
    def __init__(self, base, timeout=600):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, path, body=None, timeout=None, method=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw)
                except Exception:                             # noqa: BLE001
                    return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:                                # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    def health(self):
        return self._req("/health", timeout=3)[0] == 200

    def tokenize(self, text):
        code, d = self._req("/tokenize", {"content": text}, timeout=120)
        return (d.get("tokens") or []) if isinstance(d, dict) else []

    def chat(self, body, timeout=None):
        return self._req("/v1/chat/completions", body, timeout=timeout)

    def slot_action(self, slot, action, filename=None, timeout=600):
        body = {"_": 1}
        if filename is not None:
            body["filename"] = filename
        return self._req(f"/slots/{slot}?action={action}", body, timeout=timeout)


# ══════════════════════════════════════════════════════════════════════════
#  代理
# ══════════════════════════════════════════════════════════════════════════
class Proxy:
    def __init__(self, cfg, dev_dir):
        self.cfg = cfg
        self.dev = Path(dev_dir)
        self.up = Upstream(os.environ.get("PROXY_UPSTREAM")
                           or cfg["upstream"])

        self.l2_dir = Path(cfg["l2_dir"])
        self.l3_dir = self.dev / cfg["l3_dir"]
        self.store = store_mod.Store(
            self.l2_dir, self.l3_dir,
            l2_quota=cfg["l2_quota_bytes"], l3_quota=cfg["l3_quota_bytes"],
            l2_spill_ratio=cfg.get("l2_spill_ratio", 0.30),
            l3_evict_ratio=cfg.get("l3_evict_ratio", 0.20),
            spill_cooldown_s=cfg.get("spill_cooldown_s", 5.0),
            log=self.event)

        self.fp = cfg["server_fingerprint"]
        self.idx = index_mod.Index(self.dev / "kv_index.json",
                                   config_fp=self.fp, log=self.event)
        self.n_slots = int(cfg["num_slots"])
        self.slot_key = {i: None for i in range(self.n_slots)}
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.refcount = {}

        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.events_path = self.dev / cfg.get(
            "events_path", f"results/{self.run_id}/events.jsonl")
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.web_dir = self.dev / "web"

        self.stat = dict.fromkeys(
            ["requests", "hit_l1", "hit_l2", "hit_l3", "hit_prefix", "miss",
             "saves", "restores", "spills", "evicts", "errors",
             "skipped_cache", "reused_tokens", "total_prompt_tokens"], 0)

        self._boot()

    # ── 事件 ──────────────────────────────────────────────────────────
    def event(self, rec):
        rec = dict(rec)
        rec.setdefault("t", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        rec.setdefault("ts", round(time.time(), 3))
        try:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:                                     # noqa: BLE001
            pass
        if os.environ.get("PROXY_VERBOSE"):
            print("[ev]", json.dumps(rec, ensure_ascii=False), flush=True)

    # ── 启动 ──────────────────────────────────────────────────────────
    def _boot(self):
        loaded = self.idx.load()

        def reader(key):
            for tier in ("L2", "L3"):
                p = self.store.path_in(tier, key)
                if p.is_file():
                    return slotfile.read_header(p)
            raise FileNotFoundError(key)

        # 先把所有条目重置：进程重启 → 显存里的 KV 全部失效
        for e in self.idx.entries.values():
            e["tier"] = None
            e["slot"] = None
            ft = e.get("file_tier")
            if not (ft and self.store.exists_in(ft, e["key"])):
                # 文件也不在 → 找一下，找不到就标 missing
                for t in ("L2", "L3"):
                    if self.store.exists_in(t, e["key"]):
                        e["file_tier"] = t
                        break
                else:
                    e["file_tier"] = None
                    e["tier"] = "missing"

        rep = self.idx.reconcile(self.store.scan_files(), token_reader=reader)
        self.idx.save()
        self.slot_key = {i: None for i in range(self.n_slots)}

        # ── 清掉"别的配置"留下的缓存 ─────────────────────────────────
        # 键里含配置指纹，换过配置后这些条目永远不可能再被命中，
        # 但它们的文件还占着 L2/L3 配额。启动时一次性清掉。
        stale = [k for k, e in self.idx.entries.items()
                 if e.get("fp") and e.get("fp") != self.fp]
        freed = 0
        for k in stale:
            e = self.idx.get(k) or {}
            freed += int(e.get("bytes") or 0)
            self.store.remove("L2", k)
            self.store.remove("L3", k)
            self.idx.drop(k)
        if stale:
            self.idx.save()
            self.event({"event": "stale_config_evict", "n": len(stale),
                        "freed_bytes": freed, "current_fp": self.fp,
                        "note": "这些缓存属于别的配置，永远不会再被命中"})

        self.event({"event": "proxy_start", "run_id": self.run_id,
                    "config_fp": self.fp, "index_loaded": loaded,
                    "reconcile": rep, "upstream": self.up.base,
                    "usage": self.store.usage()})

    # ── 键 ────────────────────────────────────────────────────────────
    def cache_key(self, prefix_text):
        h = hashlib.sha256()
        h.update(prefix_text.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.fp.encode("utf-8"))
        return h.hexdigest()[:16]

    @staticmethod
    def split_messages(messages):
        if not messages:
            return "", []
        if len(messages) == 1:
            return messages[0].get("content") or "", []
        prefix = "\n\n".join((m.get("content") or "") for m in messages[:-1])
        return prefix, messages[-1:]

    # ── slot 管理 ─────────────────────────────────────────────────────
    def _free_slots(self):
        return [i for i in range(self.n_slots) if self.slot_key.get(i) is None]

    def _lru_slot(self, skip_busy=True):
        """
        选最久没用的槽。

        ⚠️ skip_busy 必须为 True：正在处理请求的槽（refcount>0）绝不能被挑走，
           否则 _release_slot 会 erase 掉别人正在用的 KV。
           实测踩过：L3 恢复成功但 prompt_n 几乎等于全量，就是恢复的 KV
           被并发的另一个请求覆盖了。
        """
        best, best_t = None, None
        for i in range(self.n_slots):
            if skip_busy and self.refcount.get(i, 0) > 0:
                continue
            k = self.slot_key.get(i)
            e = self.idx.get(k) if k else None
            t = (e or {}).get("last_used_at") or 0
            if best is None or t < best_t:
                best, best_t = i, t
        return best

    def _acquire_slot(self):
        """
        拿一个可用槽：优先空的 → 否则腾掉最久没用的 → 全忙就等。
        """
        with self.lock:
            waited = 0.0
            while True:
                free = self._free_slots()
                if free:
                    return free[0]
                v = self._lru_slot()
                if v is not None:
                    self._release_slot(v)
                    return v
                # 所有槽都在处理请求 → 等别人用完，不要抢
                if waited >= 60:
                    # 兜底：等了 60s 还没空，只能强行抢一个（避免永久卡死）
                    v = self._lru_slot(skip_busy=False)
                    self.event({"event": "slot_wait_timeout",
                                "forced_slot": v,
                                "note": "所有槽被占用超过 60s，强行腾一个"})
                    if v is not None:
                        self._release_slot(v)
                        return v
                    raise RuntimeError("没有可用的 slot")
                self.cond.wait(timeout=0.5)
                waited += 0.5

    def _release_slot(self, slot):
        """
        腾槽：若无文件备份则先 save（R1：save → 索引 → erase）
        """
        key = self.slot_key.get(slot)
        if key is None:
            return True
        e = self.idx.get(key)
        need_save = bool(e) and not (e.get("file_tier")
                                     and self.store.exists_in(e["file_tier"], key))
        if need_save:
            if self._save_slot(slot, key, reason="slot_evict"):
                e = self.idx.get(key)
                e["tier"] = None
                e["slot"] = None
                self.idx.save()
            else:
                if e:
                    e["tier"] = "missing"
                    e["slot"] = None
                    self.idx.save()
        else:
            if e:
                e["tier"] = None
                e["slot"] = None
                self.idx.save()
        code, resp = self.up.slot_action(slot, "erase")
        self.slot_key[slot] = None
        self.event({"event": "erase", "slot": slot, "key": key, "code": code,
                    "n_erased": (resp or {}).get("n_erased")
                    if isinstance(resp, dict) else None,
                    "reason": "slot_evict"})
        return True

    # ── 容量维护 ─────────────────────────────────────────────────────
    def _maintain(self):
        with self.lock:
            try:
                if self.store.need_spill():
                    moved = self.store.spill_to_l3(self.idx, self.refcount)
                    if moved:
                        self.stat["spills"] += 1
                        self.event({"event": "spill_batch", "n": len(moved),
                                    "keys": moved, "usage": self.store.usage()})
                rm = self.store.evict_l3(self.idx, self.refcount)
                if rm:
                    self.stat["evicts"] += 1
                    self.event({"event": "evict_batch", "n": len(rm),
                                "keys": rm, "usage": self.store.usage()})
            except Exception as e:                            # noqa: BLE001
                self.event({"event": "maintain_failed",
                            "error": f"{type(e).__name__}: {e}"})
    # ── 保存 ─────────────────────────────────────────────────────────
    def _save_slot(self, slot, key, reason="after_compute"):
        """把槽里的 KV 存成文件（落在 L2 目录）。成功后【不改变 tier=L1】"""
        e = self.idx.get(key)
        est = int((e or {}).get("n_tokens") or 0) * 112 * 1024
        est = int(est * 1.02)
        # 放不下就腾地方：可能一次不够（大文件要腾好几批），最多试 4 轮
        for _ in range(4):
            if not est or self.store.can_fit_l2(est):
                break
            with self.lock:
                moved = self.store.spill_to_l3(self.idx, self.refcount,
                                               force=True)
            if moved:
                self.stat["spills"] += 1
                self.event({"event": "spill_batch", "n": len(moved),
                            "keys": moved, "reason": "before_save",
                            "usage": self.store.usage()})
            else:
                break
        if est and not self.store.can_fit_l2(est):
            self.stat["skipped_cache"] += 1
            self.event({"event": "skip_cache", "key": key, "reason": "no_space",
                        "need_bytes": est, "usage": self.store.usage()})
            return False

        fname = f"{key}.bin"
        t0 = time.time()
        code, resp = self.up.slot_action(slot, "save", fname)
        n = (resp or {}).get("n_saved") if isinstance(resp, dict) else 0
        if code != 200 or not n:
            self.stat["errors"] += 1
            self.event({"event": "save_failed", "key": key, "slot": slot,
                        "code": code, "resp": str(resp)[:180]})
            return False

        p = self.store.path_in("L2", key)
        size = p.stat().st_size if p.is_file() else 0
        save_ms = round((time.time() - t0) * 1000, 1)
        with self.lock:
            e = self.idx.get(key)
            if e:
                e["file_tier"] = "L2"
                e["bytes"] = size
                e["n_tokens"] = n
                e["save_ms"] = save_ms
                # ⚠️ 关键：槽里那份还在，tier 保持 L1
                if self.slot_key.get(slot) == key:
                    e["tier"] = "L1"
                    e["slot"] = slot
                self.idx.save()
        self.stat["saves"] += 1
        self.event({"event": "save", "key": key, "slot": slot, "n_saved": n,
                    "bytes": size, "ms": save_ms, "reason": reason})
        return True

    # ── 恢复 ─────────────────────────────────────────────────────────
    def _ensure_in_l2(self, key):
        """
        保证文件在 L2 目录里 —— llama-server 只能从 --slot-save-path 指定的
        那一个目录读文件，所以 L3 的文件【必须先搬回 L2】才能 restore。
        这是磁盘层的真实代价：命中冷门文档时要先付一次复制。
        """
        e = self.idx.get(key)
        if not e:
            return False
        if self.store.exists_in("L2", key):
            return True
        if not self.store.exists_in("L3", key):
            return False
        # 要提升回 L2；没空间就先腾一批（最多试 3 轮）
        for _ in range(3):
            t0 = time.time()
            if self.store.promote_to_l2(self.idx, key):
                self.event({"event": "promote", "key": key, "from": "L3",
                            "to": "L2", "reason": "restore",
                            "ms": round((time.time() - t0) * 1000, 1)})
                return True
            with self.lock:
                moved = self.store.spill_to_l3(self.idx, self.refcount,
                                               force=True)
            if moved:
                self.stat["spills"] += 1
                self.event({"event": "spill_batch", "n": len(moved),
                            "keys": moved, "reason": "before_promote",
                            "usage": self.store.usage()})
            else:
                break
        return False

    def _restore_into(self, slot, key):
        if not self._ensure_in_l2(key):
            self.event({"event": "restore_failed", "key": key, "slot": slot,
                        "reason": "file_not_reachable"})
            return False
        fname = f"{key}.bin"
        code, resp = self.up.slot_action(slot, "restore", fname)
        n = (resp or {}).get("n_restored") if isinstance(resp, dict) else 0
        if code == 200 and n:
            self.slot_key[slot] = key
            with self.lock:
                e = self.idx.get(key)
                if e:
                    e["tier"] = "L1"
                    e["slot"] = slot
                    self.idx.save()
            self.stat["restores"] += 1
            self.event({"event": "restore", "key": key, "slot": slot,
                        "n_tokens": n,
                        "ms": round(((resp.get("timings") or {})
                                     .get("restore_ms") or 0), 1)})
            return True
        self.event({"event": "restore_failed", "key": key, "slot": slot,
                    "code": code, "resp": str(resp)[:180]})
        with self.lock:
            e = self.idx.get(key)
            if e:
                e["file_tier"] = None
                e["tier"] = "missing"
                self.idx.save()
        self.store.remove("L2", key)
        self.store.remove("L3", key)
        return False

    # ── 前缀候选（轻微改动）──────────────────────────────────────────
    def _find_prefix_candidate(self, new_tokens):
        """
        找"前缀几乎一样"的旧条目（用于轻微改动）。
        用代理自己算的块指纹比较 —— 不用 .bin 里的 token（那是打包格式）。
        """
        thr = float(self.cfg.get("prefix_reuse_min_ratio", 0.5))
        if not new_tokens:
            return None
        new_blocks = slotfile.prefix_blocks(new_tokens)
        if not new_blocks:
            return None

        best, best_lcp = None, 0
        for e in list(self.idx.entries.values()):
            ft = e.get("file_tier")
            if not ft or not e.get("prefix_blocks"):
                continue
            # ⚠️ 只和【同配置】的缓存比前缀。
            #    配置（-c/-np/KV 量化）变了以后，旧 KV 可能根本装不进新的槽，
            #    拿它做前缀复用会把 restore 失败变成一次白跑。
            if e.get("fp") != self.fp:
                continue
            if not self.store.exists_in(ft, e["key"]):
                continue
            lcp = slotfile.estimate_lcp(e["prefix_blocks"], new_blocks)
            if lcp > best_lcp:
                best, best_lcp = e, lcp

        if not best:
            return None
        ratio = best_lcp / max(1, len(new_tokens))
        self.event({"event": "prefix_probe", "cand": best["key"],
                    "lcp_est": best_lcp, "new_len": len(new_tokens),
                    "ratio": round(ratio, 3), "threshold": thr})
        if ratio >= thr:
            return {"key": best["key"], "file_tier": best["file_tier"],
                    "lcp": best_lcp, "ratio": ratio}
        return None

    # ── 主流程 ───────────────────────────────────────────────────────
    def handle_chat(self, body):
        t0 = time.time()
        messages = body.get("messages") or []
        prefix_text, _rest = self.split_messages(messages)
        if not prefix_text:
            return self._passthrough(body, "no_prefix")

        key = self.cache_key(prefix_text)
        with self.lock:
            self.stat["requests"] += 1

        mode, slot, prefix_src = None, None, None
        new_tokens = None          # 延迟 tokenize：只在需要时做一次

        with self.lock:
            e = self.idx.get(key)

            # ① L1：槽里就有
            if e and e.get("tier") == "L1" and e.get("slot") is not None \
                    and self.slot_key.get(e["slot"]) == key:
                mode, slot = "L1", e["slot"]
                self.stat["hit_l1"] += 1

            # ② 文件命中（L2/L3）
            elif e and e.get("file_tier") \
                    and self.store.exists_in(e["file_tier"], key):
                orig_tier = e["file_tier"]
                slot = self._acquire_slot()
                if self._restore_into(slot, key):
                    mode = orig_tier
                    self.stat["hit_l2" if orig_tier == "L2" else "hit_l3"] += 1
                else:
                    slot = None

            # ③ 轻微改动：找前缀最像的，restore 它再算
            if mode is None and slot is None:
                try:
                    new_tokens = self.up.tokenize(prefix_text)
                except Exception:                             # noqa: BLE001
                    new_tokens = None
                cand = self._find_prefix_candidate(new_tokens) if new_tokens else None
                if cand:
                    s = self._acquire_slot()
                    if self._restore_into(s, cand["key"]):
                        mode, slot, prefix_src = "prefix", s, cand
                        self.stat["hit_prefix"] += 1

            # ④ 全 miss
            if mode is None and slot is None:
                self.stat["miss"] += 1
                mode, slot = "miss", self._acquire_slot()

        fwd = dict(body)
        fwd["id_slot"] = slot
        with self.lock:
            self.refcount[slot] = self.refcount.get(slot, 0) + 1
        try:
            code, resp = self.up.chat(fwd)
        finally:
            with self.lock:
                self.refcount[slot] = max(0, self.refcount.get(slot, 1) - 1)
                # 唤醒在 _acquire_slot 里等槽的线程
                self.cond.notify_all()
        wall = (time.time() - t0) * 1000

        if code != 200 or not isinstance(resp, dict):
            self.stat["errors"] += 1
            self.event({"event": "upstream_error", "key": key, "slot": slot,
                        "code": code, "resp": str(resp)[:200]})
            return self._json(502, {"error": "upstream_failed",
                                    "detail": str(resp)[:400]})

        tim = resp.get("timings") or {}
        use = resp.get("usage") or {}
        pn = tim.get("prompt_n") or 0
        ptot = use.get("prompt_tokens") or 0
        reused = max(0, ptot - pn) if ptot else 0
        with self.lock:
            self.stat["total_prompt_tokens"] += ptot
            self.stat["reused_tokens"] += reused

        # ── 陈旧槽位检测 ──────────────────────────────────────────────
        # 如果代理以为命中了（槽里该有 KV），但实际一个 token 都没复用，
        # 说明槽被外部清空了（比如有人直接调 /slots/N?action=erase，
        # 或者服务重启过而我们没察觉）。此时把映射修正，别一直记错。
        if mode in ("L1", "L2", "L3") and ptot > 500 and reused == 0:
            with self.lock:
                stale = self.idx.get(key)
                if stale:
                    stale["tier"] = None
                    stale["slot"] = None
                    self.idx.save()
                self.slot_key[slot] = None
            self.event({"event": "stale_slot", "key": key, "slot": slot,
                        "claimed": mode, "prompt_total": ptot,
                        "reason": "槽里没有可复用的 KV，映射已修正"})

        # 登记：这个槽现在装的是 key
        with self.lock:
            self.slot_key[slot] = key
            e = self.idx.get(key)
            if e is None:
                self.idx.add(key, tier="L1", slot=slot, n_tokens=ptot or None,
                             fp=self.fp)
                e = self.idx.get(key)
            else:
                e["tier"] = "L1"
                e["slot"] = slot
                e["n_tokens"] = ptot or e.get("n_tokens")
                e["fp"] = self.fp
            # 前缀块指纹（用于"轻微改动"检测）
            if new_tokens is None and not e.get("prefix_blocks"):
                try:
                    new_tokens = self.up.tokenize(prefix_text)
                except Exception:                             # noqa: BLE001
                    new_tokens = None
            if new_tokens:
                e["prefix_blocks"] = slotfile.prefix_blocks(new_tokens)
            self.idx.touch(key)
            self.idx.save()

        self.event({"event": "hit" if mode != "miss" else "miss",
                    "mode": mode, "key": key, "slot": slot,
                    "prompt_n": pn, "prompt_total": ptot,
                    "reused_tokens": reused,
                    "prefill_ms": round(tim.get("prompt_ms") or 0, 1),
                    "wall_ms": round(wall, 1),
                    "prefix_src": (prefix_src or {}).get("key")})

        # 算出来的内容才需要落盘；命中路径文件已存在
        if mode in ("miss", "prefix"):
            if not self._has_file(key):
                self._save_slot(slot, key, reason="after_compute")

        self._maintain()

        resp["_proxy"] = {"mode": mode, "key": key, "slot": slot,
                          "reused_tokens": reused,
                          "proxy_wall_ms": round(wall, 1)}
        return self._json(200, resp)

    def _has_file(self, key):
        e = self.idx.get(key)
        return bool(e and e.get("file_tier")
                    and self.store.exists_in(e["file_tier"], key))

    def _passthrough(self, body, reason=""):
        code, resp = self.up.chat(body)
        if code != 200 or not isinstance(resp, dict):
            return self._json(502, {"error": "upstream_failed",
                                    "detail": str(resp)[:300]})
        resp["_proxy"] = {"mode": "passthrough", "reason": reason}
        return self._json(200, resp)

    @staticmethod
    def _json(code, obj):
        return code, json.dumps(obj, ensure_ascii=False).encode("utf-8")

    # ── 状态 ─────────────────────────────────────────────────────────
    def status(self):
        with self.lock:
            st = dict(self.stat)
            by_tier = {}
            for e in self.idx.entries.values():
                t = e.get("tier") or "file_only"
                by_tier[t] = by_tier.get(t, 0) + 1
            return {
                "run_id": self.run_id,
                "config_fp": self.fp,
                "upstream": self.up.base,
                "slots": {str(k): v for k, v in self.slot_key.items()},
                "tiers": self.store.usage(),
                "entries": by_tier,
                "stat": st,
                "hit_rate": (st["hit_l1"] + st["hit_l2"] + st["hit_l3"]
                             + st["hit_prefix"]) / max(1, st["requests"]),
                "reuse_token_ratio": st["reused_tokens"]
                / max(1, st["total_prompt_tokens"]),
            }


# ══════════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════════
def make_server(proxy, port):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _static(self, path):
            import mimetypes
            rel = "index.html" if path in ("/", "") else path[len("/web/"):]
            rel = rel.split("?")[0]
            # 防目录穿越
            target = (proxy.web_dir / rel).resolve()
            try:
                target.relative_to(proxy.web_dir.resolve())
            except Exception:                                 # noqa: BLE001
                return self._send(403, b'{"error":"forbidden"}')
            if not target.is_file():
                return self._send(404, b'{"error":"not found"}')
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)

        def do_GET(self):
            p = self.path
            bare = p.split("?", 1)[0]
            if bare in ("/", "") or bare.startswith("/web/"):
                return self._static(bare)
            if p.startswith("/health"):
                return self._send(200, b'{"ok":true}')
            if p.startswith("/admin/status"):
                return self._send(200, json.dumps(
                    proxy.status(), ensure_ascii=False).encode())
            if p.startswith("/admin/events"):
                n = 50
                if "?" in p:
                    import urllib.parse as u
                    q = u.parse_qs(p.split("?", 1)[1])
                    n = int((q.get("limit") or ["50"])[0])
                try:
                    lines = proxy.events_path.read_text(
                        encoding="utf-8").splitlines()[-n:]
                except Exception:                             # noqa: BLE001
                    lines = []
                return self._send(200, "\n".join(lines).encode())
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            p = self.path
            if p.startswith("/admin/spill"):
                with proxy.lock:
                    moved = proxy.store.spill_to_l3(proxy.idx, proxy.refcount)
                return self._send(200, json.dumps(
                    {"moved": moved}, ensure_ascii=False).encode())
            if p.startswith("/admin/reset_stat"):
                with proxy.lock:
                    for k in proxy.stat:
                        proxy.stat[k] = 0
                return self._send(200, b'{"ok":true}')
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln) if ln else b"{}"
            if p.startswith("/v1/chat/completions"):
                try:
                    body = json.loads(raw)
                except Exception:                             # noqa: BLE001
                    return self._send(400, b'{"error":"bad json"}')
                return self._send(*proxy.handle_chat(body))
            return self._send(404, b'{"error":"not found"}')

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--dev-dir", default=None)
    a = ap.parse_args()
    here = Path(__file__).resolve().parent.parent
    cfg = json.loads(Path(a.config or here / "configs" / "cache.json")
                     .read_text(encoding="utf-8"))
    px = Proxy(cfg, Path(a.dev_dir) if a.dev_dir else here)
    port = int(cfg.get("proxy_port", 8091))
    srv = make_server(px, port)
    print(f"缓存代理已启动  http://127.0.0.1:{port}", flush=True)
    print(f"  上游      {px.up.base}", flush=True)
    print(f"  L2(tmpfs) {px.l2_dir}", flush=True)
    print(f"  L3(磁盘)  {px.l3_dir}", flush=True)
    print(f"  配额      L2 {cfg['l2_quota_bytes']//1048576} MiB / "
          f"L3 {cfg['l3_quota_bytes']//1048576} MiB", flush=True)
    print(f"  事件日志  {px.events_path}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到停止信号", flush=True)


if __name__ == "__main__":
    main()
