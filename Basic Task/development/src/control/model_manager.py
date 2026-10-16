#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_manager.py —— 模型的启动、停止与切换

背景（实测确认的约束）：
    llama-server 一个进程只能加载一个模型 ——
        --help 里只有一个 -m 参数
        /v1/models 只返回当前加载的那一个
    所以"切换模型" = 杀掉旧进程 + 用新模型启动新进程。

本模块负责：
    · 记录当前生效的模型
    · 执行切换（含状态机与 5 个边界处理）
    · 提供切换状态给前端查询

状态机（对应计划书 4.3）：
    idle ──切换请求──→ 校验 ──失败──→ 回到 idle（不碰正在跑的进程）
                          │成功
                          ↓
                     switching（拒绝新的对话请求）
                          ↓
                 停旧进程 → 启新进程 → 等就绪
                          │
                 ┌────────┴────────┐
              成功               失败
                 │                 │
          更新 active         ⭐ 回退旧模型
                 └────────┬────────┘
                          ↓
                        idle

对应计划书：第四节 4.2 / 4.3，第六节
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config, models_config, upstream
from .logutil import log

# ── 全局状态 ────────────────────────────────────────────────────────────────
_lock = threading.Lock()          # 保证同一时刻只有一次切换

_state = {
    "active": None,               # 当前生效的模型 id
    "switching": False,           # 是否正在切换
    "target": None,               # 正在切向哪个模型
    "started_at": None,           # 切换开始时间
    "last_error": None,           # 上次切换的失败原因
    "last_switch": None,          # 上次成功切换的摘要
}

# llama-server 二进制位置（多候选，便于换机器）
SERVER_BIN_CANDIDATES = [
    os.environ.get("ANTLAB_SERVER_BIN"),
    str(Path.home() / "antlab/src/llama.cpp/build/bin/llama-server"),
    str(Path(__file__).resolve().parents[3] / "src/llama.cpp/build/bin/llama-server"),
]

# 进程与日志
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
PID_FILE = LOG_DIR / "model-server.pid"
SERVER_LOG = LOG_DIR / "model-server.log"

READY_TIMEOUT = 60                # 等就绪最多多少秒


# ── 工具 ────────────────────────────────────────────────────────────────────
def find_server_bin():
    """找 llama-server 可执行文件"""
    for c in SERVER_BIN_CANDIDATES:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return Path(c)
    return None


def is_switching():
    return _state["switching"]


def get_active():
    return _state["active"]


def get_state():
    """给前端看的状态快照"""
    return {
        "active": _state["active"],
        "switching": _state["switching"],
        "target": _state["target"],
        "last_error": _state["last_error"],
        "last_switch": _state["last_switch"],
    }


# ── 进程管理 ────────────────────────────────────────────────────────────────
def _stop_server(wait_seconds=15):
    """
    停止模型服务进程。

    先按 PID 文件精确停止（可靠），再按端口/进程名兜底。
    返回 True 表示端口已释放。

    ⚠️ 关于 PID 文件的归属：
      模型服务可能由 serve.sh 启动，也可能由本模块启动。
      两边约定【共用同一个 PID 文件】，谁启动谁写，谁停止谁删。
      这样无论谁起的，另一方都能按 PID 精确停止，不会出现"停不掉"。
    """
    stopped_pid = None
    if PID_FILE.is_file():
        try:
            pid = int(PID_FILE.read_text().strip())
            if pid > 0:
                os.kill(pid, signal.SIGTERM)
                stopped_pid = pid
                log(f"已向模型服务发送 SIGTERM（PID {pid}）")
        except ValueError:
            log("PID 文件内容不是数字，忽略")
        except ProcessLookupError:
            log("PID 文件里的进程已不存在")
        except PermissionError:
            log("没有权限停止该进程")

    # 等它退出
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not upstream.port_in_use(config.MODEL_SERVER_PORT):
            break
        time.sleep(0.5)

    # 兜底 1：还占着就 SIGKILL
    if upstream.port_in_use(config.MODEL_SERVER_PORT):
        if stopped_pid:
            try:
                os.kill(stopped_pid, signal.SIGKILL)
                log("SIGTERM 无效，已 SIGKILL")
            except ProcessLookupError:
                pass
            time.sleep(1.5)

    # 兜底 2：按进程名清理（处理不是本模块启动的进程）
    if upstream.port_in_use(config.MODEL_SERVER_PORT):
        log("端口仍被占用，按进程名清理 llama-server")
        subprocess.run(["pkill", "-f", "llama-server"],
                       capture_output=True, timeout=10)
        time.sleep(2)

    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"删除 PID 文件失败：{e}")

    free = not upstream.port_in_use(config.MODEL_SERVER_PORT)
    log("模型服务已停止，端口已释放" if free else "⚠️ 端口仍被占用")
    return free


def _build_args(entry, model_path):
    """
    按注册表里的 server_args 组装启动参数。

    ⭐ 参数跟着模型走：小模型可以给大上下文，大模型要收着，否则 KV Cache 吃满显存。

    ⚠️ 这里必须和 serve.sh 的 model_start() 保持一致！
       两条路径都能启动模型服务（serve.sh model / 网页切换模型），
       如果参数不一样，同一个模型会因为"谁启动的"而跑出不同配置，
       测试数据就不可比了。改动时两边一起改。
    """
    sa = entry.get("server_args") or {}
    args = [
        str(find_server_bin()),
        "-m", str(model_path),
        "-ngl", str(sa.get("ngl", 99)),
        "-c",   str(sa.get("ctx", 4096)),
        # ── 为"提交标准要求的证据"固定加的两个参数（与 serve.sh 一致）──
        #   -v            打开详细日志，才有 GPU 驻留证据
        #                 （offloaded N/N layers、layer N: dev = CUDA0、
        #                   CUDA0 KV buffer size）
        #   --cache-ram 0 关闭主机 RAM prompt cache，让缓存工作集驻留显存
        #                 （题目第 17 行要求；不影响 KV 位置与前缀复用）
        "-v", "--cache-ram", "0",
        "--host", config.MODEL_SERVER_HOST,
        "--port", str(config.MODEL_SERVER_PORT),
    ]
    if sa.get("np"):
        args += ["-np", str(sa["np"])]
    # 可选：注册表里显式给了额外参数
    extra = sa.get("extra")
    if isinstance(extra, list):
        args += [str(x) for x in extra]
    return args


def _start_server(entry, model_path):
    """启动模型服务，返回 True 表示就绪"""
    binary = find_server_bin()
    if binary is None:
        log(f"❌ 找不到 llama-server，候选路径：{SERVER_BIN_CANDIDATES}")
        return False

    args = _build_args(entry, model_path)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = SERVER_LOG.open("a", encoding="utf-8")
    logfile.write(f"\n{'='*70}\n[{time.strftime('%H:%M:%S')}] 启动：{' '.join(args)}\n")

    try:
        proc = subprocess.Popen(
            args, stdout=logfile, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        log(f"❌ 启动模型服务失败：{e}")
        return False

    PID_FILE.write_text(str(proc.pid))
    log(f"模型服务已启动 PID {proc.pid}，等待就绪（最多 {READY_TIMEOUT} 秒）…")

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        # 进程已经死了就别等了
        if proc.poll() is not None:
            log(f"❌ 模型服务进程已退出（退出码 {proc.returncode}）")
            return False
        h = upstream.get_health()
        if h["state"] == "ok":          # ⭐ 严格就绪判定（解析 JSON 字段）
            return True
        time.sleep(1)

    log(f"❌ 等待就绪超时（{READY_TIMEOUT} 秒）")
    return False


def _gpu_free_mib():
    """读剩余显存（MiB），失败返回 None"""
    g = upstream.get_gpu_memory()
    if not g.get("available"):
        return None
    return g["total_mib"] - g["used_mib"]


# ── 对外接口 ────────────────────────────────────────────────────────────────

def _read_pid():
    """
    读 PID 文件里的模型服务 PID。读不到或进程已不在则返回 None。

    ⚠️ 为什么不直接返回文件内容：PID 文件可能是【过时】的
       （服务被杀掉时来不及删文件，或者上次没清干净）。
       返回一个过时 PID 会让界面显示"运行中 PID 12345"，
       而那个号可能已经被系统分配给别人了。所以顺便探一下进程在不在。
    """
    if not PID_FILE.is_file():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)          # 信号 0 = 只探测存在性，不真的发信号
        return pid
    except (ProcessLookupError, PermissionError):
        return None


def _resolve_active_model():
    """
    取"当前该用哪个模型"并找到它的文件。

    返回 (entry, model_path, error)。

    ⚠️ 抽出来是因为 start_only / stop_only / switch_to 都要这一段，
       而且这里踩过坑：get_initial_active() 返回的是【字符串 id】，
       不是字典 —— 早先当成 dict 用过，取出来的是 'q' 这样的单个字符。
    """
    active_id = models_config.get_initial_active()
    if not active_id or not isinstance(active_id, str):
        return None, None, (f"读不到当前模型 id（得到 {active_id!r}）；"
                            f"检查 configs/models.json 的 active 字段")
    entry, err = models_config.get_model_entry(active_id)
    if err or not entry:
        return None, None, f"注册表里读不到模型 {active_id}：{err}"
    model_path, perr = config.find_model_path_by_name(entry["file"])
    if model_path is None:
        return None, None, f"找不到模型文件：{perr}"
    return entry, model_path, None


def start_only():
    """
    只启动模型服务（不碰控制层）。

    返回 dict：{ok, action, message, model_file, pid, health, ms}

    ⚠️ 与 serve.sh model 的区别（这是本函数存在的理由）：
       serve.sh model 的语义是【重启】—— 先停再起，中间约 3 秒空窗。
       实测：+1s 000  +2s 000  +3s 503  +4s 200
       于是两种场景不该用它：
         ① 服务已经在跑，只是想确认它在 → 用它等于白白中断 3 秒
         ② 正在跑测试时 → 中断会让那一轮请求集体失败
       本函数是【幂等】的：已经在跑就直接返回，不做任何动作。

    ⚠️ 为什么要做"已经在跑就跳过"而不是"无脑 start"：
       无脑 start 会在端口已被占用时失败，或者起出第二个进程
       （两个进程抢同一个端口，后来者绑定失败但可能留下僵尸）。
    """
    t0 = time.time()
    if is_switching():
        return {"ok": False, "action": "busy",
                "message": "模型切换正在进行中，请等它结束",
                "ms": round((time.time() - t0) * 1000, 1)}

    h = upstream.get_health()
    if h.get("state") == "ok":
        return {"ok": True, "action": "skip",
                "message": "模型服务已经在运行，无需启动（本操作是幂等的）",
                "health": h,
                "pid": _read_pid(),
                "ms": round((time.time() - t0) * 1000, 1)}

    entry, model_path, err = _resolve_active_model()
    if err:
        return {"ok": False, "action": "start", "message": err,
                "ms": round((time.time() - t0) * 1000, 1)}

    # 进程在但 health 不是 ok —— 可能还在加载，也可能半死不活。
    # ⚠️ 这种情况要先停掉再起，否则会起出第二个进程。
    if upstream.port_in_use(config.MODEL_SERVER_PORT):
        log("模型服务：端口被占但健康检查不通过 —— 先停掉残留进程再启动")
        _stop_server()

    log(f"模型服务：按注册表启动 {entry['file']}"
        f"（ctx={(entry.get('server_args') or {}).get('ctx')}）")
    started = _start_server(entry, model_path)
    h2 = upstream.get_health()
    return {
        "ok": bool(started and h2.get("state") == "ok"),
        "action": "started",
        "message": ("模型服务已启动并就绪" if started
                    else "启动失败或等待就绪超时，看 logs/model-server.log"),
        "model_file": entry["file"],
        "pid": _read_pid(),
        "health": h2,
        "ms": round((time.time() - t0) * 1000, 1),
    }


def stop_only():
    """
    只停止模型服务（控制层不受影响）。

    返回 dict：{ok, action, message, ms}

    ⚠️ 为什么控制层要提供这个能力，而不是让人去 WSL 里跑脚本：
       网页上就能停/起模型服务 —— 这是题目要求的"服务管理界面"。
       而 `serve.sh stop` 会【把控制层一起停掉】，那样网页本身也没了，
       根本没法在界面上完成"停模型服务"这个动作。
    """
    t0 = time.time()
    if is_switching():
        return {"ok": False, "action": "busy",
                "message": "模型切换正在进行中，请等它结束",
                "ms": round((time.time() - t0) * 1000, 1)}

    h = upstream.get_health()
    if h.get("state") == "unreachable" and \
            not upstream.port_in_use(config.MODEL_SERVER_PORT):
        return {"ok": True, "action": "skip",
                "message": "模型服务本来就没在运行",
                "ms": round((time.time() - t0) * 1000, 1)}

    freed = _stop_server()
    log("服务页：已停止模型服务（控制层不受影响）")
    return {
        "ok": bool(freed),
        "action": "stopped",
        "message": ("模型服务已停止，控制层仍在运行" if freed
                    else "已发出停止，但端口仍被占用"),
        "ms": round((time.time() - t0) * 1000, 1),
    }


def init_active():
    """
    启动控制层时调用：确认注册表里的 active，并检查模型服务状态。

    注意：本函数【不启动】模型服务 —— 那由 serve.sh 负责。
    这里只做"登记"，避免控制层和服务脚本抢着起进程。
    """
    active = models_config.get_initial_active()
    _state["active"] = active
    log(f"注册表 active 模型：{active}")
    return active


def list_models_with_status(compute_sha=False):
    """
    列出所有模型 + 可用性，并核对"实际在跑的是哪个"。

    ⭐ 一个重要设计：以【服务端实际加载的模型】为准，而不是注册表里的 active。
       因为可能有人手工用别的模型起了服务。两者不一致时，以实际的为准，
       并把注册表里的值标出来，避免"界面说 A，实际在跑 B"。
    """
    props, err = upstream.get_props()
    running_file = None
    if isinstance(props, dict) and props.get("model_path"):
        running_file = Path(props["model_path"]).name

    models, load_err = models_config.list_models(
        active_id=_state["active"], compute_sha=compute_sha)

    if load_err:
        return None, load_err, running_file

    # 用"实际在跑的模型文件"校正 is_active
    matched = None
    if running_file:
        for m in models:
            if m["file"] == running_file:
                matched = m["id"]
                break
    if matched and matched != _state["active"]:
        log(f"⚠️ 注册表 active={_state['active']}，但服务实际在跑 {running_file}"
            f"（对应 id={matched}）—— 以实际为准")
        _state["active"] = matched
        # ⭐ 校正后要写回运行时状态，否则这个修正只活在内存里：
        #    进程一重启，_state 又退回 .runtime 里的旧值，界面就显示错的模型。
        #    写的是 .runtime/（不进 git），所以不会污染仓库。
        ok, werr = models_config.set_active(matched)
        if not ok:
            log(f"（运行时状态写回失败，本次修正仅内存生效：{werr}）")
        for m in models:
            m["is_active"] = (m["id"] == matched)

    return models, None, running_file


def switch_to(model_id, force=False):
    """
    切换到指定模型。

    返回 dict：
        成功 → {"ok": True, "active": ..., "elapsed_ms": ..., "model_info": {...}}
        失败 → {"ok": False, "error": "code", "message": "...", "active": 当前生效值}

    错误码：
        switching      正在切换中
        unknown_model  注册表里没有这个 id
        not_installed  文件不存在
        already_active 已经是当前模型（幂等，除非 force）
        start_failed   启动失败（已尝试回退）
        rollback_failed 回退也失败
        no_binary      找不到 llama-server
    """
    # ── 边界 0：有测试正在跑，禁止切换 ──────────────────────────────────
    #
    # ⚠️ 为什么必须有这道闸：测试是一次测几十个请求、跑几分钟，
    #    中途把模型服务重启成别的参数，正在测的数据就【整体作废】——
    #    而且不会报错，只会让汇总里混进两种配置的样本。
    #    所以要拦在门外，而不是事后发现。
    #
    # 用局部 import：bench_runner 会 import config/logutil，
    # 在模块顶层互相 import 容易绕成环。
    try:
        from . import bench_runner
        running_bench = bench_runner.active()
    except Exception:                                         # noqa: BLE001
        running_bench = None          # 读不出来就不拦（不能因为读到就拒绝服务）
    if running_bench:
        return {
            "ok": False, "error": "bench_running",
            "message": (f"有测试正在运行（{running_bench.get('label')}，"
                        f"PID {running_bench.get('pid')}），"
                        f"切换会让正在测的数据作废。"
                        f"请等它跑完，或到「测试」页停止它。"),
            "active": _state["active"],
            "bench": {"label": running_bench.get("label"),
                      "pid": running_bench.get("pid"),
                      "started_at": running_bench.get("started_at")},
        }

    # ── 边界 1：切换中又来一个切换请求 ──────────────────────────────────────
    if _state["switching"]:
        return {
            "ok": False, "error": "switching",
            "message": f"正在切换到 {_state['target']}，请稍候",
            "active": _state["active"],
        }

    entry, err = models_config.get_model_entry(model_id)
    if err:
        return {"ok": False, "error": "unknown_model",
                "message": err, "active": _state["active"]}

    # 校验文件存在（⭐ 用注册表里的文件名搜索，不接受任意路径）
    model_path, path_err = config.find_model_path_by_name(entry["file"])
    if model_path is None:
        return {"ok": False, "error": "not_installed",
                "message": f"模型文件不存在：{entry['file']}",
                "hint": "请先下载该模型（见 development/README.md 的模型获取说明）",
                "active": _state["active"]}

    # ── 边界 5：已经是当前模型 → 幂等返回 ───────────────────────────────────
    if model_id == _state["active"] and not force:
        return {"ok": True, "active": model_id, "elapsed_ms": 0,
                "message": "已经是当前模型，未做改动"}

    if find_server_bin() is None:
        return {"ok": False, "error": "no_binary",
                "message": f"找不到 llama-server 可执行文件",
                "hint": f"候选路径：{SERVER_BIN_CANDIDATES}",
                "active": _state["active"]}

    with _lock:
        if _state["switching"]:           # 拿到锁后再确认一次
            return {"ok": False, "error": "switching",
                    "message": "正在切换中", "active": _state["active"]}

        t0 = time.time()
        old_id = _state["active"]
        _state["switching"] = True
        _state["target"] = model_id
        _state["started_at"] = t0
        _state["last_error"] = None

        # 显存预检（只提示不阻断 —— 有些占用是临时的）
        free = _gpu_free_mib()
        log(f"切换开始：{old_id} → {model_id} | 剩余显存 {free} MiB")

        try:
            # ① 停旧
            log("① 停止旧模型服务")
            if not _stop_server():
                log("⚠️ 旧服务端口未释放，仍尝试启动新模型")

            # ② 启新
            log(f"② 启动新模型：{entry['file']}")
            ok = _start_server(entry, model_path)

            if ok:
                # ③ 成功
                elapsed = int((time.time() - t0) * 1000)
                _state["active"] = model_id
                models_config.set_active(model_id)

                props, _ = upstream.get_props()
                info = {}
                if isinstance(props, dict):
                    info = {
                        "model_path": props.get("model_path"),
                        "quant": props.get("model_ftype"),
                        "n_ctx": (props.get("default_generation_settings") or {}).get("n_ctx")
                                 or props.get("n_ctx"),
                        "total_slots": props.get("total_slots"),
                    }
                _state["last_switch"] = {"from": old_id, "to": model_id,
                                         "elapsed_ms": elapsed, "at": time.time()}
                log(f"✅ 切换成功：{old_id} → {model_id}，耗时 {elapsed} ms")
                return {"ok": True, "active": model_id,
                        "elapsed_ms": elapsed, "model_info": info,
                        "switched_from": old_id}

            # ── 边界 3/4：启动失败 → 回退 ──────────────────────────────────
            log(f"❌ 新模型启动失败，尝试回退到 {old_id}")
            rollback_ok = False
            if old_id and old_id != model_id:
                old_entry, _ = models_config.get_model_entry(old_id)
                if old_entry:
                    old_path, _ = config.find_model_path_by_name(old_entry["file"])
                    if old_path:
                        _stop_server()
                        rollback_ok = _start_server(old_entry, old_path)
                        if rollback_ok:
                            log(f"↩️ 已回退到 {old_id}")
                        else:
                            log(f"❌ 回退到 {old_id} 也失败了")

            tail = _server_log_tail(12)
            _state["last_error"] = {
                "target": model_id,
                "message": "新模型启动失败",
                "rollback_ok": rollback_ok,
                "log_tail": tail,
            }
            return {
                "ok": False,
                "error": "start_failed" if rollback_ok else "rollback_failed",
                "message": (f"模型 {model_id} 启动失败"
                            + ("，已回退到原模型" if rollback_ok
                               else "，且回退也失败，需要手动重启服务")),
                "active": _state["active"] if rollback_ok else None,
                "log_tail": tail,
            }
        finally:
            _state["switching"] = False
            _state["target"] = None


def _server_log_tail(n=12):
    """读模型服务日志末尾几行，用于失败时排查"""
    try:
        lines = SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return [ln for ln in lines[-n:] if ln.strip()]
    except OSError:
        return []
