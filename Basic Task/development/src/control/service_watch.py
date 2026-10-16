#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service_watch.py —— 服务状态监测 + 测试期间的自动重启

═══════════════════════════════════════════════════════════════════════════
 这个模块解决什么
═══════════════════════════════════════════════════════════════════════════
 跑测试时模型服务中途挂了，那一轮剩下的请求会全部失败。人往往要等测试
 跑完、看到一堆失败才发现。本模块在后台盯着，发现掉了就把它拉回来，
 让那一轮测试能继续跑完。

═══════════════════════════════════════════════════════════════════════════
 ⚠️ 为什么"只能在测试运行中动作"（这是本模块最要紧的设计）
═══════════════════════════════════════════════════════════════════════════
 之前我做过一版"服务掉了就自动重启"，结果它把系统搞得很乱，最后整个
 移除了。原因是它分不清两种情况：

     ① 服务真的崩了                    → 该救
     ② 有人正在【有意】重启服务         → 不该救
        （`serve.sh model` 的语义就是先停后起，中间约 3 秒没监听）

 第 ② 种在监测器眼里和崩溃一模一样，于是它每次都去"救"，
 而"救"的过程自己又制造一次空窗 —— 服务被反复停起。
 实测表现：手动用服务很稳，一跑测试脚本就频繁掉线。

 现在的规避办法很直接：**只在测试运行中才允许动作**。

     测试在跑   → 服务掉了说明是意外，救它
     测试没在跑 → 只提示，绝不动手
                  （这时服务掉线多半是有人故意停的 / 正在换模型）

 这条规则同时满足了另一个要求："如果在跑测试之前模型就掉了，
 那只能提示，不会有动作。"

═══════════════════════════════════════════════════════════════════════════
 几个必须有的保护
═══════════════════════════════════════════════════════════════════════════
 · 开关默认【关】：这是会真的动进程的操作，不该默认开着。
 · 同一次测试里最多重启 N 次（默认 2）：避免"起来就挂"时无限重启 ——
   那种情况说明不是偶发问题，反复重启只会掩盖它。
 · 重启之间要有冷却（默认 20 秒）：进程刚起来可能还在加载，
   立刻就判定又要重启会把服务来回折腾。
 · 每次都记事件（时间、原因、耗时、结果），供界面和报告引用。
 · 重启动作复用 model_manager 的启停逻辑 —— 不重写一份。
"""

import json
import os
import threading
import time
from pathlib import Path

from .logutil import log

_SRC = Path(__file__).resolve().parents[1]
LOGS_DIR = _SRC / "logs"
EVENTS_FILE = LOGS_DIR / "service-events.jsonl"

# ── 可调参数（服务页可以改）────────────────────────────────────────────────
_DEFAULTS = {
    "enabled": False,        # ⚠️ 默认关：会动进程的操作不该默认开
    "poll_interval_s": 3.0,  # 多久探一次
    "fail_threshold": 2,     # 连续失败几次算"真的掉了"（防抖）
    "cooldown_s": 20.0,      # 两次重启之间至少隔这么久
    "max_restarts_per_run": 2,   # 同一次测试最多重启几次
    "ready_timeout_s": 90,   # 等就绪的上限
    # ⭐ 测试刚结束后的宽限期（秒）
    #
    # ⚠️ 为什么必须有它（这是实测踩出来的）：
    #    服务在测试【中途】挂掉时，剩下的请求会很快集体失败，
    #    测试程序于是提前跑完退出。等监测器下一次探活时，
    #    bench_runner.active() 已经返回 None —— 于是判定
    #    "没有测试在跑，只提示不动作"，眼睁睁看着不去救。
    #    实测时序：
    #        18:24:28  服务被杀
    #        18:24:44  测试因请求全败而退出（48 条里 42 条 service_lost）
    #        18:24:50  监测器探活 → "没有测试在跑" → 不动作 ❌
    #    逻辑没错，但错过了窗口。
    #
    #    加上宽限期后：只要【刚刚】还有测试在跑（默认 90 秒内），
    #    就仍然认为这次掉线是测试期间的意外，该救。
    #    宽限期不能太长 —— 否则"人跑完测试后手工停服务"也会被当成
    #    意外而自动拉起，正好违背了"只在测试时动作"这条规矩。
    "grace_after_test_s": 90.0,
}

_lock = threading.Lock()
_state = {
    **_DEFAULTS,
    "watching": False,
    "consecutive_failures": 0,
    "recovering": False,
    "last_probe": None,
    "last_health": None,
    "restarts_this_run": 0,
    "run_id_at_last_reset": None,
    "last_restart_at": None,
    "last_event": None,
    "n_restarts_total": 0,
    # ⭐ 上一次"确有测试在跑"的时刻 —— 宽限期就靠它判断
    "last_test_seen_at": None,
    "last_test_label": None,
    # ⭐ 本次掉线是否已经记过事件 —— 防止同一次掉线刷屏
    #    （服务停了之后每轮探活都失败，不拦就是几十条一样的事件）
    "notified_this_outage": False,
}
_stop_flag = threading.Event()
_wake = threading.Event()
_thread = None


# ══════════════════════════════════════════════════════════════════════════
#  探测
# ══════════════════════════════════════════════════════════════════════════

def probe(timeout=5):
    """探一次模型服务。返回 (健康?, 说明)。复用 upstream 的严格判定。"""
    from . import upstream
    try:
        h = upstream.get_health()
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return h.get("state") == "ok", (h.get("detail") or h.get("state") or "未知")


def _test_running():
    """
    有没有测试正在跑。返回 (在跑?, 状态 dict 或 None)。

    ⚠️ 这是本模块的【总闸】的第一半。
       第二半是 _within_grace()（刚跑完的宽限期），
       两半合起来才是"允许动作"的完整判据 —— 见 _tick()。
    """
    try:
        from . import bench_runner
        st = bench_runner.active()
        return (st is not None), st
    except Exception as e:                                    # noqa: BLE001
        log(f"服务监测：查询测试状态失败（{type(e).__name__}: {e}），"
            f"按【没有测试在跑】处理（即不动作）")
        return False, None


def _within_grace():
    """
    最近【刚刚】还有测试在跑吗？返回 (在宽限期内?, 还剩几秒, 当时的测试名)。

    ⚠️ 为什么需要它：服务在测试中途挂掉时，剩下的请求会很快集体失败，
       测试程序提前退出；等监测器下一次探活时已经"没有测试在跑"了。
       实测就是这么错过窗口的（见 _DEFAULTS 里 grace_after_test_s 的说明）。
    """
    with _lock:
        t = _state["last_test_seen_at"]
        g = _state["grace_after_test_s"]
        label = _state["last_test_label"]
    if not t:
        return False, 0.0, None
    left = g - (time.time() - t)
    return (left > 0), max(0.0, left), label


# ══════════════════════════════════════════════════════════════════════════
#  事件记录
# ══════════════════════════════════════════════════════════════════════════

def _record(ev):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ev.setdefault("t", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as e:                                      # noqa: BLE001
        log(f"写服务事件失败：{e}")
    with _lock:
        _state["last_event"] = ev
    return ev


def list_events(limit=50):
    if not EVENTS_FILE.is_file():
        return []
    out = []
    try:
        for line in EVENTS_FILE.read_text(
                encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return []
    return out[-limit:]


# ══════════════════════════════════════════════════════════════════════════
#  重启
# ══════════════════════════════════════════════════════════════════════════

def _do_restart(reason, run_state=None):
    """
    执行一次重启：停 → 起 → 就绪确认。

    返回事件 dict。

    ⚠️ 复用 model_manager 的启停逻辑，不另写一份：
       服务参数怎么拼、PID 文件怎么处理、就绪怎么判，全在那里。
       复制一份迟早两处不一致（本项目在 models.json 上栽过一次）。
    """
    from . import config, model_manager, models_config

    ev = {
        "kind": "restart",
        "reason": reason,
        "test_run": (run_state or {}).get("label"),
        "test_run_id": None,
        "ok": False,
        "steps": [],
    }
    t0 = time.time()

    # 哪次测试
    try:
        from . import bench_runner
        rs = bench_runner.read_state() or {}
        before = set(rs.get("results_before") or [])
        root = _SRC.parent / "results"
        if root.is_dir():
            new = [d.name for d in sorted(root.iterdir())
                   if d.is_dir() and d.name not in before]
            ev["test_run_id"] = new[-1] if new else None
    except Exception:                                         # noqa: BLE001
        pass

    # 注册表里的当前模型
    active_id = models_config.get_initial_active()
    # ⚠️ get_initial_active() 返回的是【字符串 id】，不是字典。
    #    早先当成 dict 用过，取出来的是 'q' 这样的单个字符。
    if not active_id or not isinstance(active_id, str):
        ev["error"] = f"读不到当前模型 id（得到 {active_id!r}）"
        return _record(ev)
    entry, err = models_config.get_model_entry(active_id)
    if err or not entry:
        ev["error"] = f"读不到模型记录：{err}"
        return _record(ev)
    model_path, perr = config.find_model_path_by_name(entry["file"])
    if model_path is None:
        ev["error"] = f"找不到模型文件：{perr}"
        return _record(ev)

    ev["model_file"] = entry["file"]

    # ① 停
    ts = time.time()
    try:
        model_manager._stop_server()
        ev["steps"].append({"step": "停服务", "ok": True,
                            "ms": round((time.time() - ts) * 1000, 1)})
    except Exception as e:                                    # noqa: BLE001
        # 服务本来就没了，停不掉不算失败
        ev["steps"].append({"step": "停服务", "ok": True,
                            "note": f"{type(e).__name__}: {e}（可能本来就没了）"})

    # ② 起
    ts = time.time()
    try:
        started = model_manager._start_server(entry, model_path)
    except Exception as e:                                    # noqa: BLE001
        ev["error"] = f"启动异常：{type(e).__name__}: {e}"
        ev["steps"].append({"step": "起服务", "ok": False, "error": ev["error"]})
        ev["ms"] = round((time.time() - t0) * 1000, 1)
        return _record(ev)
    ev["steps"].append({"step": "起服务", "ok": bool(started),
                        "ms": round((time.time() - ts) * 1000, 1)})
    if not started:
        ev["error"] = "启动失败（_start_server 返回 False）"
        ev["ms"] = round((time.time() - t0) * 1000, 1)
        return _record(ev)

    # ③ 就绪确认（不只看起来了，还要看模型对不对）
    ts = time.time()
    ok, why = probe()
    ready_detail = {"health": ok, "detail": why}
    try:
        from . import upstream
        # ⚠️ get_props() 返回的是 (数据, 错误) 元组，不是 dict ——
        #    我第一版直接当成 dict 用（props.get(...)），
        #    结果 AttributeError: 'tuple' object has no attribute 'get'。
        #    照抄 model_manager.py:270 的用法。
        props, perr = upstream.get_props()
        if isinstance(props, dict):
            mp = props.get("model_path") or ""
            ready_detail["model_file"] = mp.rsplit("/", 1)[-1]
            # ⚠️ 上下文不在 props 顶层的 n_ctx，而在
            #    default_generation_settings.n_ctx 里（照抄 upstream 的解析）
            ready_detail["ctx"] = (props.get("default_generation_settings")
                                   or {}).get("n_ctx")
            ready_detail["与注册表一致"] = (
                ready_detail["model_file"] == entry["file"])
        elif perr:
            ready_detail["props_error"] = perr
    except Exception as e:                                    # noqa: BLE001
        ready_detail["props_error"] = f"{type(e).__name__}: {e}"
    ev["steps"].append({"step": "就绪确认", "ok": bool(ok),
                        "ms": round((time.time() - ts) * 1000, 1),
                        "detail": ready_detail})
    if not ok:
        ev["error"] = f"就绪确认失败：{why}"
        ev["ms"] = round((time.time() - t0) * 1000, 1)
        return _record(ev)

    # ④ 恢复后发一个请求确认能推理（题目原文要求）
    ts = time.time()
    vok, vwhy = _verify_inference()
    ev["steps"].append({"step": "恢复后验证", "ok": bool(vok),
                        "ms": round((time.time() - ts) * 1000, 1),
                        "detail": vwhy})
    ev["ok"] = bool(vok)
    if not vok:
        ev["error"] = vwhy
    ev["ms"] = round((time.time() - t0) * 1000, 1)
    return _record(ev)


def _verify_inference(timeout=60):
    """恢复后真发一个请求 —— 只看到 /health 200 不算，那只能说明进程活着。"""
    import urllib.request
    from . import config
    url = (f"http://{config.MODEL_SERVER_HOST}:{config.MODEL_SERVER_PORT}"
           f"/v1/chat/completions")
    body = {"messages": [{"role": "user", "content": "只回答两个字：收到"}],
            "max_tokens": 16, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:                                    # noqa: BLE001
        return False, f"恢复后请求失败：{type(e).__name__}: {e}"
    ch = (d.get("choices") or [{}])[0]
    txt = ((ch.get("message") or {}).get("content") or "").strip()
    return bool(txt), (f"恢复后请求成功：{txt[:30]!r}" if txt
                       else "恢复后请求返回空内容")


# ══════════════════════════════════════════════════════════════════════════
#  监测循环
# ══════════════════════════════════════════════════════════════════════════

def _tick():
    """一次探测 + 决策"""
    ok, why = probe()
    with _lock:
        _state["last_probe"] = time.time()
        _state["last_health"] = {"ok": ok, "detail": why}

    # ⭐ 先记下"这一刻有没有测试在跑"。
    #    必须【每次都记】（不管服务是否健康）—— 因为服务掉线后
    #    测试会很快退出，等我们要判断时已经查不到了，
    #    只能靠之前记下的这个时刻来判断宽限期。
    running, st = _test_running()
    if running:
        with _lock:
            _state["last_test_seen_at"] = time.time()
            _state["last_test_label"] = (st or {}).get("label")

    if ok:
        with _lock:
            was_down = _state["consecutive_failures"] >= _state["fail_threshold"]
            _state["consecutive_failures"] = 0
            # ⭐ 恢复了 → 允许下一次掉线再记一条事件
            _state["notified_this_outage"] = False
        if was_down:
            log("服务监测：模型服务已恢复")
        return None

    with _lock:
        _state["consecutive_failures"] += 1
        n = _state["consecutive_failures"]
        ft = _state["fail_threshold"]
        enabled = _state["enabled"]
        already_notified = _state["notified_this_outage"]

    # ── 关着就只记状态，什么都不做 ────────────────────────────────────
    if not enabled:
        return None

    # ── 防抖：连续失败够次数才认定"真的掉了" ──────────────────────────
    if n < ft:
        log(f"服务监测：模型服务不可达（{n}/{ft}）：{why}")
        return None

    # ══════════════════════════════════════════════════════════════════
    #  ⚠️ 同一次掉线只记一条事件
    #
    #  为什么：服务停了之后每一轮探活都会失败。不拦的话，
    #  停 30 秒就是 10 条一模一样的事件，时间线被刷屏，
    #  真正重要的那条（比如自动重启）反而被淹没。
    #  实测踩过：一次掉线记了 7 条重复的"只提示未动作"。
    #
    #  什么时候重置：服务恢复健康时（上面 ok 分支里）。
    # ══════════════════════════════════════════════════════════════════
    if already_notified:
        return None

    # ══════════════════════════════════════════════════════════════════
    #  ⚠️⚠️ 总闸：只有在"测试运行中 或 刚跑完的宽限期内"才允许动作
    #
    #  这一条是本模块安全性的核心。理由见文件头：
    #  服务"掉线"有两种（真的崩了 / 有人正在有意停它），监测器
    #  分辨不出是哪种 —— 所以只在"测试相关"这个明确的时间窗内动作。
    #
    #  两个条件缺一不可地构成这个窗口：
    #    · running         —— 此刻就有测试在跑
    #    · 宽限期内         —— 刚刚还在跑（服务中途挂掉时测试会提前退出，
    #                          发现时往往已经"没在跑"了）
    # ══════════════════════════════════════════════════════════════════
    in_grace, grace_left, grace_label = _within_grace()
    if not running and not in_grace:
        with _lock:
            _state["notified_this_outage"] = True
        _record({
            "kind": "noticed_only",
            "reason": f"模型服务不可达（{why}）",
            "action": "none",
            "why_no_action": ("当前没有测试在跑，也不在'刚跑完'的宽限期内 —— "
                              "按设计只提示、不自动重启。"
                              "（服务掉线可能是有人正在有意停它/换模型，"
                              "自动拉起会与那人打架）"),
        })
        log(f"服务监测：模型服务不可达，但【没有测试在跑】"
            f"→ 只记录，不重启。{why}")
        with _lock:
            _state["consecutive_failures"] = 0   # 避免反复记同一条
        return None

    # 走到这里说明在窗口内 —— 说明原因（便于报告里讲清楚）
    if running:
        why_action = f"测试 {st.get('label')} 正在运行"
        run_id = st.get("label") or "?"
    else:
        why_action = (f"测试 {grace_label} 刚刚还在跑"
                      f"（宽限期还剩 {grace_left:.0f} 秒）"
                      f"—— 服务是在测试期间掉的")
        run_id = grace_label or "?"

    # ── 同一次测试里的重启次数上限 ────────────────────────────────────
    with _lock:
        if _state["run_id_at_last_reset"] != run_id:
            _state["run_id_at_last_reset"] = run_id
            _state["restarts_this_run"] = 0
        used = _state["restarts_this_run"]
        cap = _state["max_restarts_per_run"]
        last_at = _state["last_restart_at"]
        cooldown = _state["cooldown_s"]

    if used >= cap:
        with _lock:
            _state["notified_this_outage"] = True
        _record({
            "kind": "give_up",
            "reason": f"模型服务不可达（{why}）",
            "test_run": run_id,
            "action": "none",
            "why_no_action": (f"本次测试已重启 {used} 次，达到上限 {cap} —— "
                              f"再重启只会掩盖问题，交给人处理"),
        })
        log(f"服务监测：本次测试已重启 {used}/{cap} 次，不再重启")
        with _lock:
            _state["consecutive_failures"] = 0
        return None

    if last_at and (time.time() - last_at) < cooldown:
        log(f"服务监测：还在冷却期（{cooldown - (time.time() - last_at):.0f}s），"
            f"暂不重启")
        return None

    # ── 动手 ──────────────────────────────────────────────────────────
    with _lock:
        if _state["recovering"]:
            return None
        _state["recovering"] = True
    try:
        log(f"服务监测：模型服务掉线，且{why_action}"
            f" → 自动重启（本次第 {used + 1}/{cap} 次）")
        ev = _do_restart(f"测试期间服务掉线（{why}）；{why_action}", st)
        with _lock:
            _state["restarts_this_run"] = used + 1
            _state["last_restart_at"] = time.time()
            _state["consecutive_failures"] = 0
            if ev.get("ok"):
                _state["n_restarts_total"] += 1
        return ev
    finally:
        with _lock:
            _state["recovering"] = False


def restart_now(reason="手动触发"):
    """
    手动触发一次完整流程：停 → 起 → 就绪确认 → 恢复后验证。

    ⚠️ 这个入口【不受】"只在测试运行中才动作"那道闸限制。
       为什么：那道闸是防【自动】动作误判用的 ——
       自动动作分不清"服务崩了"和"有人正在有意重启它"。
       而人明确点了这个按钮，意图是清楚的，不存在误判。
       这也是题目「演示服务退出后的检测、重启、就绪确认」要的入口。

    ⚠️ 如果自动重启正在跑，就不要重入 —— 两个重启并行会互相抢进程。
    """
    with _lock:
        if _state["recovering"]:
            return _record({
                "kind": "restart_skipped",
                "reason": reason,
                "ok": False,
                "error": "已经有一次自动重启在进行中，请等它结束",
            })
        _state["recovering"] = True
    try:
        log(f"服务页：手动重启模型服务（{reason}）")
        ev = _do_restart(reason)
        with _lock:
            if ev.get("ok"):
                _state["n_restarts_total"] += 1
            _state["last_restart_at"] = time.time()
        return ev
    finally:
        with _lock:
            _state["recovering"] = False


def start_watch():
    """启动后台监测线程（幂等）。"""
    global _thread
    with _lock:
        if _state["watching"]:
            return False
        _state["watching"] = True
        _stop_flag.clear()

    def loop():
        with _lock:
            iv = _state["poll_interval_s"]
            en = _state["enabled"]
        log(f"服务监测已启动（每 {iv} 秒探一次；自动重启="
            f"{'开' if en else '关'}；"
            f"★ 只在测试运行中才会自动重启）")
        while not _stop_flag.is_set():
            with _lock:
                iv = _state["poll_interval_s"]
            _wake.wait(iv)
            _wake.clear()
            if _stop_flag.is_set():
                break
            try:
                _tick()
            except Exception as e:                            # noqa: BLE001
                log(f"服务监测循环异常：{type(e).__name__}: {e}")
        with _lock:
            _state["watching"] = False
        log("服务监测已停止")

    _thread = threading.Thread(target=loop, name="service-watch", daemon=True)
    _thread.start()
    return True


def stop_watch():
    _stop_flag.set()
    _wake.set()
    with _lock:
        _state["watching"] = False


def init():
    """控制层启动时调用"""
    try:
        start_watch()
    except Exception as e:                                    # noqa: BLE001
        log(f"⚠️ 启动服务监测失败：{type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  配置与状态（给服务页用）
# ══════════════════════════════════════════════════════════════════════════

def set_config(**kw):
    """改配置（服务页的开关走这里）"""
    allowed = set(_DEFAULTS)
    with _lock:
        for k, v in kw.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
                _state[k] = bool(v)
            elif k in ("fail_threshold", "max_restarts_per_run",
                       "ready_timeout_s"):
                _state[k] = max(1, int(v))
            else:
                _state[k] = max(0.5, float(v))
        out = {k: _state[k] for k in _DEFAULTS}
    _wake.set()
    log(f"服务监测参数已更新：{out}")
    return out


def status():
    with _lock:
        st = {k: _state[k] for k in list(_DEFAULTS) + [
            "watching", "consecutive_failures", "recovering",
            "last_probe", "last_health", "restarts_this_run",
            "run_id_at_last_reset", "last_restart_at",
            "n_restarts_total", "last_event",
            "last_test_seen_at", "last_test_label"]}
    running, rst = _test_running()
    in_grace, grace_left, grace_label = _within_grace()
    # 宽限期剩余秒数（给界面显示倒计时）
    st["grace_left_s"] = round(grace_left, 1) if in_grace else 0
    return {
        **st,
        "test_running": running,
        "test_label": (rst or {}).get("label"),
        "in_grace": in_grace,
        "grace_label": grace_label,
        # ★ 把"现在会不会自动重启"算出来告诉前端 ——
        #   免得用户看到开关是开的，却不知道"其实当前不会动作"
        "will_auto_restart": bool(st["enabled"] and (running or in_grace)),
        "rule": ("只在【测试运行中】、或【测试刚结束的宽限期内】才会自动重启。"
                 "其余时间服务掉线只提示、不动作 —— "
                 "因为那可能是有人正在有意停它/换模型。"
                 f"宽限期默认 {st['grace_after_test_s']:.0f} 秒，"
                 "用来兜住'服务挂掉→测试提前退出→监测器才发现'这个时序。"),
        "recent_events": list_events(20),
    }
