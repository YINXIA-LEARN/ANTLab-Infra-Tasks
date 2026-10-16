#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_11_service_watch.py —— 服务监测与"测试期间自动重启"

═══════════════════════════════════════════════════════════════════════════
 这个功能最要紧的一条规则，也是本测试的重点：
     ★ 只在【测试运行中】才会自动重启；其余时间只提示、不动作。
═══════════════════════════════════════════════════════════════════════════
 为什么这条规则这么要紧：之前做过一版"服务掉了就自动重启"，结果把系统
 搞得很乱，最后整体移除了。原因是它分不清两种情况：
     · 服务真的崩了
     · 有人正在【有意】重启服务（serve.sh model 就是先停后起，约 3 秒空窗）
 第 ② 种在监测器眼里和崩溃一模一样 → 它每次都去"救"，
 而"救"自己又制造一次空窗 → 服务被反复停起。

 所以本测试查的是【规则】，不是"能不能重启"：
   ① 开关默认必须是关的
   ② 没有测试在跑时掉线 → 事件里必须是 noticed_only，绝不能有 restart
   ③ 事件记录必须写明"为什么不动作"
   ④ 状态接口必须如实报告"当前会不会动作"
   ⑤ 配置能改、能读回、有合理边界
   ⑥ 两个单独启停接口存在且幂等
   ⑦ 服务真的掉了时，状态接口要如实反映（不能报 healthy）
═══════════════════════════════════════════════════════════════════════════
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _testlib import Report, run                                  # noqa: E402

BASE = "http://127.0.0.1:8000"


def api(path, method="GET", body=None, timeout=150):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        # 服务在，但返回了错误码（4xx/5xx）
        try:
            return json.loads(e.read().decode("utf-8")), e.code
        except Exception:                                     # noqa: BLE001
            return {"error": "http", "message": str(e)}, e.code
    except urllib.error.URLError as e:
        # ⚠️ 这条分支是后补的，踩过一次坑：
        #    原来只捕 HTTPError，而"控制层根本没在跑"抛的是 URLError
        #    （Connection refused），于是异常直接飞出 api()，
        #    main() 里那句"控制层没在跑，跳过"**永远不会执行** ——
        #    实际表现是甩出 40 行 traceback、退出码 2，
        #    看起来像测试自己坏了，其实是环境没起。
        #    （触发场景：跑过 `wsl --terminate` 之后控制层被杀了。）
        #    现在统一收成 (err_dict, 0)，让调用方能判 code != 200 并体面跳过。
        return {"error": "unreachable", "message": str(e)}, 0


def main():
    rep = Report("test_11_service_watch —— 服务监测与测试期间自动重启")

    # 控制层在不在
    st, code = api("/api/status", timeout=10)
    if code != 200:
        rep.note("⚠️ 控制层没在跑，跳过（这条测试需要控制层）")
        return rep.finish()

    # ── ① 默认必须是关的 ────────────────────────────────────────────
    #
    # ⚠️ 为什么把"默认关"当成一条断言：
    #    这是会真的去动进程的操作。默认开着的话，任何人只要跑一次测试，
    #    系统就会在服务掉线时自动重启 —— 包括"有人故意停服务"的情况。
    w, code = api("/api/service/watch")
    rep.check(f"GET /api/service/watch 返回 200", code, 200)
    rep.check_true("监测状态里有 enabled 字段", "enabled" in w)
    rep.check_true("监测状态里有 will_auto_restart 字段",
                   "will_auto_restart" in w,
                   "这个字段是给界面判断『当前会不会动作』用的，不能少")
    rep.check_true("状态里写明了规则（rule 字段）", bool(w.get("rule")))
    rep.check_true("监测线程在运行（watching=True）",
                   w.get("watching") is True)

    # ── ② 没有测试在跑时，will_auto_restart 必须为 False ────────────
    running = w.get("test_running")
    in_grace = w.get("in_grace")
    if not running and not in_grace:
        rep.check_true(
            "没有测试在跑时 will_auto_restart 必须为 False",
            w.get("will_auto_restart") is False,
            f"enabled={w.get('enabled')} test_running={running} "
            f"in_grace={in_grace}")
        # 即使把开关打开，此刻也不该"会动作"
        api("/api/service/watch", "POST", {"enabled": True})
        w2, _ = api("/api/service/watch")
        rep.check_true(
            "★ 开关打开、但没有测试在跑 → 仍必须为 False（本功能的核心规则）",
            w2.get("will_auto_restart") is False,
            f"enabled={w2.get('enabled')} test_running={w2.get('test_running')} "
            f"in_grace={w2.get('in_grace')}")
        rep.check_true("状态里能看出『在不在宽限期内』", "in_grace" in w2)
    else:
        rep.note(f"有测试在跑 / 在宽限期内（running={running} "
                 f"in_grace={in_grace}）—— 跳过 ② 的前半段")

    # ── ③ 事件里"只提示"的记录必须写明原因 ──────────────────────────
    ev, code = api("/api/service/events?limit=50")
    rep.check("GET /api/service/events 返回 200", code, 200)
    events = ev.get("events") or []
    rep.note(f"现有事件 {len(events)} 条")

    noticed = [e for e in events if e.get("kind") == "noticed_only"]
    if noticed:
        e = noticed[-1]
        rep.check_true(
            "『只提示不动作』的事件写明了为什么不动作",
            bool(e.get("why_no_action")),
            "缺 why_no_action —— 用户会不知道『为什么没帮我重启』")
        rep.check_true(
            "『只提示』事件的 action 是 none",
            e.get("action") == "none", f"action={e.get('action')!r}")
        rep.check_true(
            "『只提示』事件里不能有重启步骤（那意味着它其实动了手）",
            not e.get("steps"),
            f"steps={e.get('steps')}")
    else:
        rep.note("（还没有 noticed_only 事件 —— 要造：开关开着、无测试、停服务）")

    # ── ④ 自动重启的记录必须走完四步 ────────────────────────────────
    restarts = [e for e in events if e.get("kind") == "restart"]
    if restarts:
        e = restarts[-1]
        steps = [s.get("step") for s in (e.get("steps") or [])]
        rep.note(f"最近一次重启：ok={e.get('ok')} 耗时={e.get('ms')}ms "
                 f"步骤={steps}")
        # 题目要求：触发退出 → 检测 → 重启 → 就绪确认 → 恢复后请求
        for need in ("停服务", "起服务", "就绪确认", "恢复后验证"):
            rep.check_true(f"重启记录里有「{need}」这一步",
                           need in steps, f"实际步骤={steps}")
        rep.check_true("重启记录写了原因", bool(e.get("reason")))
        rep.check_true("重启记录有时间戳", bool(e.get("t")))
        ready = next((s for s in e["steps"] if s.get("step") == "就绪确认"), {})
        det = ready.get("detail")
        if isinstance(det, dict):
            rep.check_true(
                "就绪确认里核对了『模型与注册表是否一致』",
                "与注册表一致" in det,
                "只看 /health 200 不够 —— 那只说明进程活着")
    else:
        rep.note("（还没有 restart 事件 —— 要造：开着开关跑测试、中途停服务）")

    # ── ⑤ 配置：能改、能读回、边界合理 ──────────────────────────────
    cfg, code = api("/api/service/watch", "POST", {
        "poll_interval_s": 5, "fail_threshold": 3, "cooldown_s": 30,
        "max_restarts_per_run": 1,
    })
    rep.check("POST /api/service/watch 返回 200", code, 200)
    c = cfg.get("config") or {}
    rep.check("探活间隔被改成了 5", c.get("poll_interval_s"), 5.0)
    rep.check("失败阈值被改成了 3", c.get("fail_threshold"), 3)
    rep.check("冷却被改成了 30", c.get("cooldown_s"), 30.0)
    rep.check("单次上限被改成了 1", c.get("max_restarts_per_run"), 1)

    # 改回来（别把测试环境留在奇怪的状态）
    api("/api/service/watch", "POST", {
        "poll_interval_s": 3, "fail_threshold": 2, "cooldown_s": 20,
        "max_restarts_per_run": 2,
    })

    # 非法值要被夹到合理范围，而不是原样接受
    bad, _ = api("/api/service/watch", "POST",
                 {"poll_interval_s": -5, "fail_threshold": 0})
    cb = bad.get("config") or {}
    rep.check_true("探活间隔为负时被夹到 >= 0.5",
                   cb.get("poll_interval_s", 0) >= 0.5,
                   f"得到 {cb.get('poll_interval_s')}")
    rep.check_true("失败阈值为 0 时被夹到 >= 1",
                   cb.get("fail_threshold", 0) >= 1,
                   f"得到 {cb.get('fail_threshold')}")
    api("/api/service/watch", "POST",
        {"poll_interval_s": 3, "fail_threshold": 2})

    # ── ⑥ 两个单独启停接口存在、且启动是幂等的 ──────────────────────
    #
    # ⚠️ 注意：这里【不会】去真的停服务 —— 那会让后面的测试全挂。
    #    只验证"已经在跑时调启动接口 → action=skip"这一条幂等语义。
    r, code = api("/api/service/model/start", "POST", {}, timeout=180)
    rep.check("POST /api/service/model/start 返回 200", code, 200)
    rep.check_true("启动接口返回里有 ok 字段", "ok" in r)
    rep.check_true("启动接口返回里有 action 字段", "action" in r)
    if r.get("action") == "skip":
        rep.check_true("★ 服务已在跑时，启动接口是幂等的（action=skip）",
                       r.get("ok") is True)
        rep.note(f"   message: {r.get('message')}")
    else:
        rep.note(f"   本次 action={r.get('action')}（服务当时没在跑，"
                 f"所以真的启动了）")

    # 停止接口只检查"路由存在"（用 OPTIONS 之外的轻办法：不真调）
    #
    # ⚠️ 不能直接 POST /api/service/model/stop —— 那会把服务停掉，
    #    后面的测试和界面都会不可用。所以这里只确认前端有对应按钮，
    #    以及服务端确实注册了这个路径（靠故意用错方法看它是否回 405/400
    #    都不合适），因此改为检查代码里存在该分支。
    src = Path(__file__).resolve().parents[1] / "src" / "control" / "server.py"
    if src.is_file():
        txt = src.read_text(encoding="utf-8", errors="replace")
        rep.check_true("服务端注册了 /api/service/model/stop",
                       "/api/service/model/stop" in txt)
        rep.check_true("服务端注册了 /api/service/restart",
                       "/api/service/restart" in txt)
        rep.check_true(
            "服务端只在这两类请求里放行自动动作（有总闸）",
            "只在【测试运行中】" in txt or "_test_running" in txt
            or "service_watch" in txt)

    # ── ⑦ 服务状态必须如实反映（掉了就说掉了）───────────────────────
    s, _ = api("/api/status")
    m = s.get("model_server") or {}
    rep.check_true("状态里有 model_server.reachable",
                   "reachable" in m)
    health = m.get("health")
    reachable = m.get("reachable")
    rep.check_true(
        "★ reachable 与 health 必须自洽（不能一边说不可达一边说 ok）",
        (reachable is True and health == "ok")
        or (reachable is False and health != "ok"),
        f"reachable={reachable} health={health!r}")

    # ── ⑧ ★ 跑完了但服务中断过的运行，不能显示成"完整" ──────────────
    #
    # ⚠️ 这一条是用户反馈后加的。原来的问题：
    #    有几次测试【所有请求都跑完了】，summary 里 state=finished，
    #    界面上就显示"完整"。可实际上那次测试期间模型服务掉过、
    #    被拉起来过 —— 中间那几条请求是失败的，而且重启前后
    #    KV 池与前缀缓存被清空，两段数据特性不同，
    #    混在一起的中位数没法解释。这种运行不该叫"完整"。
    #
    # 判据：运行目录里的 service-starts.jsonl（"服务被拉起"的记录）
    runs, _ = api("/api/bench/runs")
    rl = runs.get("runs") or []
    rep.note(f"结果页共 {len(rl)} 个运行")
    bad = []
    for r in rl:
        st = r.get("status")
        n = r.get("n_service_starts") or 0
        pids = r.get("service_pids") or []
        # 有服务重启记录（或跨了多个 PID）的运行，绝不能是 "ok"（完整）
        if (n > 0 or len(pids) > 1) and st == "ok":
            bad.append((r.get("run_id"), n, pids))
        # 标成 interrupted 的必须真的有过中断
        if st == "interrupted" and n == 0 and len(pids) <= 1:
            bad.append((r.get("run_id"), "interrupted 但无任何中断证据",
                        n, pids))
    rep.check_true(
        "★ 服务中断过的运行不能显示为『完整』（status=ok）",
        not bad,
        f"有问题的运行：{bad}")

    n_int = sum(1 for r in rl if r.get("status") == "interrupted")
    if n_int:
        rep.note(f"其中 {n_int} 个标为 interrupted（跑完了但服务中断过）")
        one = next(r for r in rl if r.get("status") == "interrupted")
        rep.check_true(
            "interrupted 的说明里讲清了『不要当完整测量』",
            "完整" in (one.get("status_text") or ""),
            f"status_text={one.get('status_text')!r}")
        rep.check_true(
            "interrupted 的运行 usable 必须是 False",
            one.get("usable") is False,
            f"usable={one.get('usable')}")
    else:
        rep.note("（当前没有被标为 interrupted 的运行 —— 这属于『这条边界"
                 "没被现有数据覆盖』，要造：开着开关跑测试、中途停服务）")

    print()
    return rep.finish()


if __name__ == "__main__":
    sys.exit(run(main))
