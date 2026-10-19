#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_faults.py —— S8 异常场景：缓存坏了、索引丢了、配置变了、服务重启

断言什么（每一条都是"坏了也要能继续服务"）
  1. 缓存文件被外部删除 → 请求回退到全量计算，不报错
  2. 缓存文件被截断（坏文件）→ restore 失败被识别、条目作废、回退全量
  3. 索引文件损坏 → 代理启动时从文件重建，服务可用
  4. 服务重启 → L2 清空、L3 保留、索引与实际文件一致
  5. 配置指纹变更 → 旧缓存不再命中（不能拿旧 KV 冒充新配置）

⚠️ 这个脚本会删/改缓存文件，请在"可以牺牲缓存"的时候跑。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

PROXY = "http://127.0.0.1:8091"
BASE = "http://127.0.0.1:8092"
DEV = T.DEV
L2 = Path("/dev/shm/antlab_kv/L2")
L3 = DEV / "kv_cache" / "L3"
IDX = DEV / "kv_index.json"
CFG = DEV / "configs" / "cache.json"


def http(path, body=None, timeout=900):
    url = PROXY + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            t = f.read().decode("utf-8", "replace")
            try:
                return f.status, json.loads(t)
            except Exception:                                 # noqa: BLE001
                return f.status, t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def status():
    c, d = http("/admin/status", timeout=10)
    return d if isinstance(d, dict) else {}


def events(limit=500):
    c, ev = http(f"/admin/events?limit={limit}", timeout=10)
    out = []
    if isinstance(ev, str):
        for line in ev.splitlines():
            try:
                out.append(json.loads(line))
            except Exception:                                 # noqa: BLE001
                pass
    return out


def ask(doc_rel, q, timeout=600):
    text = (DEV / "corpus" / doc_rel).read_text(encoding="utf-8")
    body = {"messages": [
        {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁。"},
        {"role": "user", "content": text},
        {"role": "user", "content": q}],
        "max_tokens": 32, "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.time()
    c, d = http("/v1/chat/completions", body, timeout=timeout)
    if not isinstance(d, dict):
        return {"mode": f"ERR{c}", "err": str(d)[:120],
                "wall": time.time() - t0}
    px = d.get("_proxy") or {}
    return {"mode": px.get("mode"), "prompt_n": (d.get("timings") or {})
            .get("prompt_n"), "wall": time.time() - t0}


def restart_proxy_only():
    """只重启代理，保留 llama-server 与缓存文件"""
    subprocess.run(["bash", "-lc",
                    "pkill -f 'proxy/proxy.py.*--dev-dir'"], capture_output=True)
    time.sleep(1.5)
    subprocess.run(
        ["bash", "-lc",
         f'setsid --fork python3 -u "{DEV}/proxy/proxy.py" '
         f'--config "{CFG}" --dev-dir "{DEV}" '
         f'> "{DEV}/logs/proxy.log" 2>&1 < /dev/null'],
        capture_output=True)
    for _ in range(80):
        if http("/health", timeout=2)[0] == 200:
            return True
        time.sleep(0.3)
    return False


def restart_all():
    subprocess.run(["bash", "-lc", f'bash "{DEV}/tools/stop_adv.sh"'],
                   capture_output=True, timeout=180)
    p = subprocess.run(["bash", "-lc", f'bash "{DEV}/tools/start_adv.sh"'],
                       capture_output=True, text=True, timeout=700)
    return "代理入口" in (p.stdout + p.stderr)


def alive():
    return http("/health", timeout=3)[0] == 200


def prime(doc_rel, q="这份资料的核心是什么？"):
    """确保某个文档被缓存（先问一次让它算出并保存）"""
    a = ask(doc_rel, q)
    time.sleep(0.5)
    return a


def find_entry_file(doc_rel):
    """
    找到某份文档在 L2/L3 里的缓存文件，并确保它【不在显存槽里】
    —— 否则测的是 L1 命中，根本没走到 restore。
    """
    prime(doc_rel)
    # 反复用其它文档把槽挤满，直到目标文档确实不在任何槽里
    others = ["small/beta-代码评审流程.md", "small/delta-数据备份策略.md",
              "small/gamma-设备借用登记.md", "small/theta-交付验收清单.md",
              "small/epsilon-会议纪要模板.md", "small/zeta-远程办公申请.md"]
    idx = json.loads(IDX.read_text(encoding="utf-8")) if IDX.is_file() else {}
    # 找出目标文档的键：取最近被 touch 的、且不是刚问过的那些
    target_key = None
    for attempt in range(8):
        slots = set((status().get("slots") or {}).values())
        idx = json.loads(IDX.read_text(encoding="utf-8")) if IDX.is_file() else {}
        cands = [k for k, e in (idx.get("entries") or {}).items()
                 if e.get("file_tier") and k not in slots]
        if target_key is None:
            # 第一次：用"刚 prime 过"的推断 —— 直接问一次看它落在哪个槽
            target_key = next((k for k in cands), None)
        if target_key and target_key not in slots:
            e = idx["entries"].get(target_key)
            if e:
                ft = e["file_tier"]
                p = (L2 / f"{target_key}.bin") if ft == "L2" else (
                    L3 / f"{target_key}.bin")
                if p.is_file():
                    return target_key, ft, p
        ask(others[attempt % len(others)], "一句话概括。")
        time.sleep(0.3)
    return None, None, None


# ══════════════════════════════════════════════════════════════════════════
def t_delete_file():
    T.case("① 缓存文件被外部删除 → 回退全量计算，不报错")
    doc = "small/beta-代码评审流程.md"
    key, ft, p = find_entry_file(doc)
    if not p:
        T.skip("找不到该文档的缓存文件"); return
    T.note(f"缓存 {key} 在 {ft}：{p}")
    p.unlink()
    T.check(not p.exists(), "文件已删除")
    # 重启代理：让它的槽位映射清空，下次请求必然走"找文件"这条路
    T.check(restart_proxy_only(), "代理重启（槽位映射清空）")

    r = ask(doc, "评审需要几个人？")
    T.check(r["mode"] != None and not str(r["mode"]).startswith("ERR"),
            f"请求仍然成功（模式 {r['mode']}）", str(r.get("err")))
    T.check(r["mode"] == "miss",
            f"文件没了 → 老老实实全量重算（模式 {r['mode']}）")
    T.check((r["prompt_n"] or 0) > 500,
            f"确实全量算了 {r['prompt_n']} token")


def t_truncate_file():
    T.case("② 缓存文件被截断 → 识别为坏文件并回退")
    doc = "small/delta-数据备份策略.md"
    key, ft, p = find_entry_file(doc)
    if not p:
        T.skip("找不到缓存文件"); return
    T.note(f"把 {p.name}（原 {p.stat().st_size} 字节）截断成 4 字节")
    # 统一放到 L2（代理只能从 L2 restore）
    lp = L2 / p.name
    if ft == "L3":
        shutil.copyfile(p, lp)
        p.unlink()
    else:
        lp = p
    lp.write_bytes(b"\x00\x00\x00\x00")
    T.check(restart_proxy_only(), "代理重启（槽位映射清空，必然走 restore）")

    r = ask(doc, "备份保留多少天？")
    T.check(not str(r["mode"]).startswith("ERR"),
            f"请求仍然成功（模式 {r['mode']}）", str(r.get("err")))
    ev = [e for e in events() if e.get("event") == "restore_failed"]
    T.check(len(ev) >= 1, f"记录了 restore_failed 事件（{len(ev)} 条）")
    if ev:
        T.note(f"事件内容：{json.dumps(ev[-1], ensure_ascii=False)[:150]}")
        T.check(ev[-1].get("code") is not None or ev[-1].get("reason") is not None,
                "事件里带失败原因")
    T.check(r["mode"] == "miss",
            f"坏文件被作废后走全量重算（模式 {r['mode']}）")


def t_corrupt_index():
    T.case("③ 索引文件损坏 → 启动时从文件重建")
    s_before = status()
    n_before = sum((s_before.get("entries") or {}).values())
    T.note(f"损坏前索引里有 {n_before} 个条目")

    IDX.write_text('{"version": 1, "entries": {"broken": ', encoding="utf-8")
    T.check(True, "索引文件已写成半截 JSON")

    ok = restart_proxy_only()
    T.check(ok, "代理能带着坏索引启动（不崩）")
    s = status()
    n_after = sum((s.get("entries") or {}).values())
    T.check(n_after > 0, f"索引已从缓存文件重建（{n_after} 个条目）")
    T.check(alive(), "代理健康")
    r = ask("small/alpha-客户对接规范.md", "登记时限是多久？")
    T.check(not str(r["mode"]).startswith("ERR"),
            f"重建后仍能命中或计算（模式 {r['mode']}）")


def t_restart_all():
    T.case("④ 服务重启 → L3 保留、索引重建（L2 是文件系统，进程重启不清）")
    s1 = status()
    l2_before = (s1.get("tiers") or {}).get("l2_used", 0)
    l3_before = (s1.get("tiers") or {}).get("l3_used", 0)
    n_before = sum((s1.get("entries") or {}).values())
    T.note(f"重启前 L2={l2_before/1048576:.0f}M L3={l3_before/1048576:.0f}M "
           f"条目={n_before}")
    T.note("⚠️ L2 是 tmpfs（文件系统），进程重启不会清空它；"
           "只有重启 WSL/主机才会没")

    ok = restart_all()
    T.check(ok, "服务重启成功")
    time.sleep(1)

    s2 = status()
    t2 = s2.get("tiers") or {}
    l2_after = t2.get("l2_used", 0)
    l3_after = t2.get("l3_used", 0)
    n_after = sum((s2.get("entries") or {}).values())
    T.note(f"重启后 L2={l2_after/1048576:.0f}M L3={l3_after/1048576:.0f}M "
           f"条目={n_after}")

    T.check(abs(l2_after - l2_before) < 2 * 1024 * 1024,
            f"L2（tmpfs 文件系统）进程重启后仍在"
            f"（{l2_before/1048576:.0f} → {l2_after/1048576:.0f} MiB）")
    T.check(abs(l3_after - l3_before) < 2 * 1024 * 1024,
            f"L3（磁盘）保留（{l3_before/1048576:.0f} → {l3_after/1048576:.0f} MiB）")
    T.check(n_after >= n_before - 2,
            f"索引从文件重建，条目基本没少（{n_before} → {n_after}）")

    # 重启后必须仍能服务：命中或重算都算成功
    r = ask("small/eta-客户资料分级.md", "资料分几级？")
    T.check(not str(r["mode"]).startswith("ERR"),
            f"重启后仍能服务（模式 {r['mode']}）")


def t_fingerprint_change():
    T.case("⑤ 配置指纹变更 → 旧缓存不再命中，且旧条目被清理")
    orig = CFG.read_text(encoding="utf-8")
    s1 = status()
    fp1 = s1.get("config_fp")
    T.note(f"当前指纹 {fp1}")

    # ⚠️ 用一个【每次都不一样】的指纹，否则可能撞上以前测试留下的同名条目
    fp_new = f"{fp1}|TEST-{int(time.time())}"
    cfg = json.loads(orig)
    cfg["server_fingerprint"] = fp_new
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    try:
        ok = restart_proxy_only()
        T.check(ok, "换了指纹后代理能启动")
        time.sleep(0.6)
        s2 = status()
        T.eq(s2.get("config_fp"), fp_new, "指纹已生效")

        # 启动时应该把旧指纹的条目清掉
        idx = json.loads(IDX.read_text(encoding="utf-8")) \
            if IDX.is_file() else {}
        fps = {e.get("fp") for e in (idx.get("entries") or {}).values()}
        T.check(fps <= {fp_new},
                f"索引里只剩新指纹的条目（实际：{fps}）")

        # 用与之前完全相同的文档和问题，必须重新全量算
        r = ask("small/alpha-客户对接规范.md", "登记时限是多久？")
        T.check(r["mode"] == "miss",
                f"旧缓存不命中，走 miss（实际 {r['mode']}）")
        T.check((r["prompt_n"] or 0) > 900,
                f"确实全量重算了（prompt_n={r['prompt_n']}）")
    finally:
        CFG.write_text(orig, encoding="utf-8")
        restart_proxy_only()
        T.check(alive(), "已还原配置并重启")


def main():
    if not alive() or http("/health", timeout=3)[0] != 200:
        T.skip("服务不在线，先跑 tools/start_adv.sh")
        return 0
    print("  ⚠️ 本测试会删除/截断缓存文件，请确认可以牺牲缓存")
    for fn in (t_delete_file, t_truncate_file, t_corrupt_index,
               t_restart_all, t_fingerprint_change):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
