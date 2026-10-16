#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_runner.py —— 从控制层启动并跟踪一次性能测试

═══════════════════════════════════════════════════════════════════════════
 这个模块存在的唯一理由：让网页能跑测试。
 而"网页跑测试"的难点全在【子进程管理】上，不在界面。
═══════════════════════════════════════════════════════════════════════════

它要处理的 6 件事（每一件都是踩过的坑或明确的失败模式）：

  ① 启动子进程
     用 --config <生成的配置文件> 而不是 --interactive ——
     因为交互模式读 stdin，网页给不了。
     生成配置文件还有个好处：参数落成文件，可复现、可 git 审查。

  ② 状态跟踪（跑完了吗？）
     状态写进 logs/bench_run.json 并立即落盘。
     判断"跑完没"用三个信号：【PID 还活着吗】+【results 下有没有新目录】
     +【那个目录里有没有 summary.json】——
     summary.json 是 run_bench.py【最后】才写的，所以它出现 = 正常跑完。

  ③ 防并发
     启动前检查状态文件里的 PID 是否还活着；活着就拒绝。
     ⚠️ 两个测试同时跑会抢同一个模型服务，【两边数据全污染】。

  ④ 孤儿进程
     控制层重启后，子进程还在跑但没人知道。
     所以状态是【文件】而不是内存 —— 重启后读文件就能恢复判断。
     而且切换模型时必须能问它"有没有测试在跑"（否则切了模型就毁掉数据）。

  ⑤ 参数对齐
     测试要求「配置与服务实际参数一致」。
     run_bench.py 会校验 ctx / KV 精度，不一致就拒绝运行（退出码 6）。
     所以启动前先看服务当前是什么；不一致就用 serve.sh + ANTLAB_OVERRIDE
     重启成目标参数 —— 复用现有启动逻辑，不在这里拼 llama-server 命令行。

  ⑥ 清理残留
     run_bench.py 中止时会留下只有 meta.json / ABORTED.txt 的目录。
     这些不是数据，是"证据"。本模块能识别并把它们列出来/清掉。
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import config
from .logutil import log

# ── 路径 ─────────────────────────────────────────────────────────────────
# 本文件在 <development>/src/control/ 下：
#     parents[0]=control [1]=src [2]=development
_SRC = Path(__file__).resolve().parents[1]
_DEV = Path(__file__).resolve().parents[2]

SERVE_SH = _SRC / "serve.sh"
BENCH_PY = _SRC / "bench" / "run_bench.py"
CONFIGS_DIR = _SRC / "bench" / "configs"
RESULTS_ROOT = _DEV / "results"
STATE_FILE = _SRC / "logs" / "bench_run.json"

# 中止残留的识别特征
#
# summary.json  : run_bench.py 【最后】才写 —— 有它 = 正常跑完
# ABORTED.txt   : 主动中止（模型校验/参数校验失败）时会写
# run.log       : 正常路径（含 dry-run）都会写
#
# ⚠️ 用实测反推出来的判据，别凭猜：
#     · 正常 dry-run  → meta + run-config + prompt-index + run.log
#     · 主动中止      → meta + run-config + ABORTED.txt
#     · 崩在中间      → meta + run-config + prompt-index（没有 run.log）
#   所以"没有 summary.json"只说明没跑完，还要能区分是哪种。
def _leftover_info(d):
    """是残留就返回原因字符串，不是就返回 None"""
    if not d.is_dir():
        return None
    if (d / "summary.json").is_file():
        return None                      # 正常结果
    files = {f.name for f in d.iterdir() if f.is_file()}
    if "ABORTED.txt" in files:
        return "主动中止（校验未通过）"
    if "run.log" in files:
        return None                      # dry-run 的正常产物，不是残留
    if files:
        return "跑到一半中断（缺 summary.json 与 run.log）"
    return "空目录"


# ══════════════════════════════════════════════════════════════════════════
#  状态文件读写
# ══════════════════════════════════════════════════════════════════════════

def read_state():
    """读状态文件。坏文件当没有 —— 不能因为状态坏了就让网页打不开。"""
    if not STATE_FILE.is_file():
        return None
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_state(d):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return True
    except OSError as e:                                      # noqa: BLE001
        log(f"写入测试状态失败：{e}")
        return False


def _alive(pid):
    """PID 还活着吗（含僵尸：僵尸进程也算"已结束"，不该被认为在跑）"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    # 是僵尸就不算在跑
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            fields = f.read().split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except OSError:
        pass
    return True


def active():
    """
    当前有没有测试在跑。返回状态 dict 或 None。

    ⚠️ 这是给别的模块调的（比如模型切换要问它）——
       所以逻辑要能处理"状态文件说在跑、但进程其实已经死了"的情况。
    """
    st = read_state()
    if not st:
        return None
    if _alive(st.get("pid")):
        return st
    return None


# ══════════════════════════════════════════════════════════════════════════
#  残留识别
# ══════════════════════════════════════════════════════════════════════════

def list_leftovers(min_age_s=60):
    """
    列出"没跑完"的结果目录。

    min_age_s：只报创建超过这么久还没写完的目录 ——
        避免把【正在跑的那次】误判成残留（它一开始也没有 summary.json）。
    """
    if not RESULTS_ROOT.is_dir():
        return []
    now = time.time()
    out = []
    for d in sorted(RESULTS_ROOT.iterdir()):
        reason = _leftover_info(d)
        if not reason:
            continue
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < min_age_s:
            continue
        out.append({"run_id": d.name, "reason": reason,
                    "age_s": int(age),
                    "files": sorted(f.name for f in d.iterdir() if f.is_file())})
    return out


def clean_leftovers(run_ids):
    """
    删除指定的残留目录，并从 runs.json 里摘掉对应条目。

    只允许删【本函数自己判定为残留】的目录 —— 不接受任意路径，
    避免网页端误删正式数据。返回 (删了几个, 说明列表)。
    """
    ok_ids = {x["run_id"] for x in list_leftovers(min_age_s=0)}
    import shutil

    removed, msgs = [], []
    for rid in run_ids:
        if rid not in ok_ids:
            msgs.append(f"{rid}：不是残留目录（或缺 summary.json 但仍在跑），已跳过")
            continue
        d = RESULTS_ROOT / rid
        try:
            shutil.rmtree(d)
            removed.append(rid)
            msgs.append(f"{rid}：已删除")
        except OSError as e:
            msgs.append(f"{rid}：删除失败 {e}")

    # 摘登记表
    reg_path = RESULTS_ROOT / "runs.json"
    if removed and reg_path.is_file():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            before = len(reg)
            reg = [r for r in reg if r.get("run_id") not in set(removed)]
            reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            msgs.append(f"登记表：{before} → {len(reg)} 条")
        except (OSError, json.JSONDecodeError) as e:
            msgs.append(f"登记表更新失败：{e}")
    return removed, msgs


# ══════════════════════════════════════════════════════════════════════════
#  服务参数对齐
# ══════════════════════════════════════════════════════════════════════════

def _query_props():
    """问模型服务的 /props。返回 (信息 dict, 错误)"""
    import urllib.error
    import urllib.request
    url = f"http://{config.MODEL_SERVER_HOST}:{config.MODEL_SERVER_PORT}/props"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"

    dgs = d.get("default_generation_settings") or {}
    cmd = ""
    try:
        r = subprocess.run(
            ["bash", "-c",
             "ps -o args= -p $(pgrep -f llama-server | head -1) 2>/dev/null"],
            capture_output=True, text=True, timeout=8)
        cmd = (r.stdout or "").strip()
    except Exception:                                         # noqa: BLE001
        pass
    toks = cmd.split()

    def _val(*names):
        for n in names:
            if n in toks and toks.index(n) + 1 < len(toks):
                return toks[toks.index(n) + 1]
        return None

    return {
        "model_file": (d.get("model_path") or "").rsplit("/", 1)[-1] or None,
        "ctx": dgs.get("n_ctx"),
        "slots": d.get("total_slots"),
        "kv_unified": dgs.get("kv_unified"),
        "ctk": _val("-ctk", "--cache-type-k") or "f16",
        "ctv": _val("-ctv", "--cache-type-v") or "f16",
        "cmdline": cmd,
    }, None


def service_plan(model_file, ctx, ctk, ctv):
    """
    对比"目标参数"与"服务当前参数"，返回计划：
        {"need_restart": bool, "reason": str, "current": {...}}
    """
    cur, err = _query_props()
    if err:
        return {"need_restart": True, "current": None,
                "reason": f"模型服务当前不可达（{err}），需要启动"}
    diffs = []
    if cur["model_file"] != model_file:
        diffs.append(f"模型 {cur['model_file']} → {model_file}")
    if cur["ctx"] != ctx:
        diffs.append(f"上下文 {cur['ctx']} → {ctx}")
    want_ctk = ctk or "f16"
    want_ctv = ctv or "f16"
    if (cur["ctk"] or "f16") != want_ctk or (cur["ctv"] or "f16") != want_ctv:
        diffs.append(f"KV 精度 {cur['ctk']}-{cur['ctv']} → {want_ctk}-{want_ctv}")
    return {"need_restart": bool(diffs),
            "reason": "；".join(diffs) if diffs else "服务已在目标配置上，无需重启",
            "current": cur}


def restart_service(model_file, ctx, ctk, ctv, timeout=180):
    """
    用目标参数重启模型服务（复用 serve.sh 的启动逻辑）。

    返回 (是否成功, 说明, 服务现状)。
    """
    if not SERVE_SH.is_file():
        return False, f"找不到 serve.sh：{SERVE_SH}", None

    override = json.dumps({"file": model_file, "ctx": ctx,
                           "ctk": ctk or None, "ctv": ctv or None},
                          ensure_ascii=False)
    env = dict(os.environ)
    env["ANTLAB_OVERRIDE"] = override

    log(f"测试页：重启模型服务为 {model_file} / ctx {ctx} / KV {ctk}-{ctv}")
    try:
        r = subprocess.run(["bash", str(SERVE_SH), "model"],
                           cwd=str(_SRC), env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"serve.sh model 超过 {timeout} 秒未返回", None
    except Exception as e:                                    # noqa: BLE001
        return False, f"调用 serve.sh 失败：{type(e).__name__}: {e}", None

    combined = (r.stdout or "") + (r.stderr or "")
    cur, err = _query_props()
    if err:
        tail = "\n".join(combined.strip().splitlines()[-10:])
        return False, f"重启后服务仍不可达：{err}\n{tail}", None
    # 独立核对参数真的生效（不只信 serve.sh 打印了"就绪"）
    if cur["model_file"] != model_file or cur["ctx"] != ctx:
        return False, (f"重启后参数不符：期望 {model_file}/ctx {ctx}，"
                       f"实际 {cur['model_file']}/ctx {cur['ctx']}"), cur
    return True, (f"就绪：{cur['model_file']} / ctx {cur['ctx']} / "
                  f"KV {cur['ctk']}-{cur['ctv']}"), cur


# ══════════════════════════════════════════════════════════════════════════
#  生成配置文件
# ══════════════════════════════════════════════════════════════════════════

def write_run_config(params, template_name=None):
    """
    按网页给的参数生成一份运行配置，写到 bench/configs/ 下。

    ⭐ 为什么落成文件而不是用 --interactive：
       · 交互模式读 stdin，网页给不了
       · 落成文件 = 参数可复现、可 git 审查（符合"配置要有唯一载体"的原则）
       · run_bench.py 会把它快照进 run-config.json，链路完整

    返回 (配置路径, 配置内容, 错误)。
    """
    base = {
        "base_url": f"http://{config.MODEL_SERVER_HOST}:{config.MODEL_SERVER_PORT}",
        "input_dir": str(_DEV.parent / "提示词与输入文档" / "输入文档"),
        "model_file": params["model_file"],
        "task_ids": ["P01", "P02", "P03", "P04",
                     "D01", "D02", "D03", "D04", "C01", "C02"],
        "rounds": 3,
        "concurrency": [1, 4, 10],
        "seed": 20261009,
        "order": "shuffled",
        "max_tokens": 2048,
        "temperature": 0.0,
        "timeout_s": 900,
        "warmup": {"enabled": True, "n": 2, "task_id": "P01"},
    }
    # 如果指定了模板配置，用它的负载部分（任务/轮次/并发/seed…）
    if template_name:
        tp = CONFIGS_DIR / template_name
        if tp.is_file():
            try:
                t = json.loads(tp.read_text(encoding="utf-8"))
                for k in ("task_ids", "rounds", "concurrency", "seed", "order",
                          "max_tokens", "temperature", "timeout_s", "warmup"):
                    if k in t:
                        base[k] = t[k]
            except (OSError, json.JSONDecodeError) as e:
                return None, None, f"读模板配置失败：{e}"

    # 网页可调的项
    base["label"] = params["label"]
    base["server_ctx"] = int(params["ctx"])
    base["kv_type_k"] = params["ctk"] or "f16"
    base["kv_type_v"] = params["ctv"] or "f16"
    # ⭐ 输出上限：网页可以覆盖模板里的值。
    #    ⚠️ 早先这里是写死的 2048，网页上根本没有这个字段 ——
    #       结果是"输出被截断"这件事既看不见也调不了。
    #       现在从 params 取（start() 里已经校验过范围）。
    if params.get("max_tokens"):
        base["max_tokens"] = int(params["max_tokens"])

    # note 自动生成 —— 只写本轮参数，天然满足 note 规范
    base["note"] = (
        f"{Path(params['model_file']).stem} · ctx {base['server_ctx']} · "
        f"KV {base['kv_type_k']}/{base['kv_type_v']} · "
        f"{len(base['task_ids'])} 任务 x {base['rounds']} 轮 x "
        f"并发{base['concurrency']} · seed {base['seed']} · "
        f"max_tokens {base['max_tokens']} · temp {base['temperature']}"
    )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"web-{params['label']}-{stamp}.json"
    path = CONFIGS_DIR / name
    try:
        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as e:
        return None, None, f"写配置文件失败：{e}"
    return path, base, None


# ══════════════════════════════════════════════════════════════════════════
#  启动 / 停止
# ══════════════════════════════════════════════════════════════════════════

def start(params, template_name=None):
    """
    启动一次测试。返回 (是否成功, 结果 dict)。

    流程（顺序很重要）：
        ① 已经在跑？ → 拒绝
        ② 校验参数
        ③ 对齐服务参数（不一致就重启）
        ④ 记录"启动前 results 里已有哪些目录"（用于事后识别新目录）
        ⑤ 生成配置文件
        ⑥ 起子进程（start_new_session，脱离控制层）
        ⑦ 写状态文件
    """
    cur = active()
    if cur:
        return False, {"error": "already_running",
                       "message": f"已有测试在运行（PID {cur.get('pid')}，"
                                  f"启动于 {cur.get('started_at')}）",
                       "state": cur}

    # ── ② 校验参数 ────────────────────────────────────────────────────
    errs = []
    if not params.get("model_file"):
        errs.append("没给模型")
    if not str(params.get("label") or "").strip():
        errs.append("没给运行标签")
    label = str(params.get("label") or "").strip()
    if label and not all(c.isalnum() or c in "-_." for c in label):
        errs.append("运行标签只能含字母数字和 - _ .（它会成为目录名的一部分）")
    try:
        ctx = int(params.get("ctx"))
        if not (512 <= ctx <= 40960):
            errs.append("上下文要在 512 ~ 40960 之间")
    except (TypeError, ValueError):
        errs.append("上下文必须是整数")
        ctx = None
    for k in ("ctk", "ctv"):
        v = params.get(k) or "f16"
        if v not in ("f32", "f16", "bf16", "q8_0", "q5_0", "q4_1", "q4_0",
                     "iq4_nl", "q5_1"):
            errs.append(f"{k} 取值不支持：{v}")
    # ⭐ 输出上限：必须 < 上下文。
    #    为什么这条校验很重要：max_tokens 是【纯输出】的上限，
    #    而上下文里还要放输入。如果 max_tokens >= ctx，
    #    那输入一进去就超了，服务端直接 400：
    #        request (N tokens) exceeds the available context size
    #    整批请求全废，白跑一轮。
    max_tokens = None
    if params.get("max_tokens") not in (None, ""):
        try:
            max_tokens = int(params["max_tokens"])
            if not (64 <= max_tokens <= 40960):
                errs.append("输出上限要在 64 ~ 40960 之间")
            elif ctx is not None and max_tokens >= ctx:
                errs.append(
                    f"输出上限 {max_tokens} 必须小于上下文 {ctx}"
                    f"（上下文里还要放输入，否则服务端会拒绝请求）")
        except (TypeError, ValueError):
            errs.append("输出上限必须是整数")
            max_tokens = None
    if errs:
        return False, {"error": "bad_params", "message": "；".join(errs)}
    if not BENCH_PY.is_file():
        return False, {"error": "no_bench", "message": f"找不到 {BENCH_PY}"}

    # 语言检查：模型必须在注册表里（不接受任意文件名）
    from . import models_config
    reg, rerr = models_config.load_registry()
    if rerr:
        return False, {"error": "registry_error", "message": rerr}
    known = {m.get("file") for m in reg.get("models", [])}
    if params["model_file"] not in known:
        return False, {"error": "unknown_model",
                       "message": f"注册表里没有这个模型：{params['model_file']}"}

    # ── ③ 对齐服务参数 ────────────────────────────────────────────────
    plan = service_plan(params["model_file"], ctx,
                        params.get("ctk"), params.get("ctv"))
    restarted = False
    if plan["need_restart"]:
        ok, msg, cur2 = restart_service(params["model_file"], ctx,
                                        params.get("ctk"), params.get("ctv"))
        if not ok:
            return False, {"error": "service_not_ready", "message": msg}
        restarted = True
    else:
        msg = plan["reason"]

    # ── ④ 记下启动前已有的结果目录 ────────────────────────────────────
    before = set()
    if RESULTS_ROOT.is_dir():
        before = {d.name for d in RESULTS_ROOT.iterdir() if d.is_dir()}

    # ── ⑤ 生成配置文件 ────────────────────────────────────────────────
    cfg_path, cfg_body, cerr = write_run_config(
        {"model_file": params["model_file"], "label": label, "ctx": ctx,
         "ctk": params.get("ctk") or "f16", "ctv": params.get("ctv") or "f16",
         "max_tokens": max_tokens},
        template_name=template_name)
    if cerr:
        return False, {"error": "config_write_failed", "message": cerr}

    # ── ⑥ 起子进程 ────────────────────────────────────────────────────
    log_dir = _SRC / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / f"bench-{label}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    try:
        f = open(run_log, "w", encoding="utf-8")               # noqa: SIM115
    except OSError as e:
        return False, {"error": "log_open_failed", "message": str(e)}

    cmd = ["python3", str(BENCH_PY), "--config", str(cfg_path),
           "--out", str(RESULTS_ROOT)]
    try:
        # ⭐ start_new_session=True：让子进程脱离控制层。
        #    这样控制层重启不会带走正在跑的测试（长跑 5 分钟，很容易撞上）。
        proc = subprocess.Popen(
            cmd, cwd=str(_SRC), stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:                                    # noqa: BLE001
        f.close()
        return False, {"error": "spawn_failed",
                       "message": f"{type(e).__name__}: {e}"}

    # ── ⑦ 写状态文件 ──────────────────────────────────────────────────
    state = {
        "pid": proc.pid,
        "label": label,
        "model_file": params["model_file"],
        "ctx": ctx,
        "ctk": params.get("ctk") or "f16",
        "ctv": params.get("ctv") or "f16",
        # 记下输出上限 —— 它是"输出会不会被截断"的直接原因，
        # 排查"这次为什么有 truncated"时要靠它
        "max_tokens": (int(params["max_tokens"])
                       if params.get("max_tokens") else None),
        "config_path": str(cfg_path),
        "config_name": cfg_path.name,
        "cmdline": " ".join(cmd),
        "log_file": str(run_log),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started_ts": time.time(),
        "restarted_service": restarted,
        "service_note": msg,
        "results_before": sorted(before),
        "_说明": [
            "当前/最近一次由网页启动的测试。控制层重启后靠它恢复判断。",
            "判断跑完没：PID 是否还活着 + results 下是否出现新目录。",
            "run_bench.py 最后才写 summary.json —— 它出现才算正常跑完。",
        ],
    }
    write_state(state)
    log(f"测试页：已启动测试 {label}（PID {proc.pid}），日志 {run_log.name}")
    return True, {"state": state, "config_name": cfg_path.name,
                  "restarted_service": restarted, "service_note": msg}


def stop(reason="用户从网页停止"):
    """
    停止正在跑的测试。

    ⚠️ 为什么真的需要"停止"：
       它可能跑了 5 分钟还没完，而用户想改参数重跑。
       没有停止的话只能等，或者手工 kill —— 后者容易连模型服务一起杀掉。
    """
    st = active()
    if not st:
        return False, {"error": "not_running", "message": "当前没有测试在跑"}
    pid = int(st["pid"])
    # ══════════════════════════════════════════════════════════════════════
    #  ⭐ 分三步停：SIGINT → SIGTERM → SIGKILL
    #
    #  ⚠️ 为什么不能一上来就 SIGTERM：
    #     原来直接 killpg(SIGTERM)，进程立刻死。而测试程序的工作线程
    #     还阻塞在 socket 读上 —— 它们手里的那几条请求【永远回不来】，
    #     于是记录里少了"正在跑的那几条"。表现就是：
    #     停止后一看，只有跑完的，被这个停止掐断的那几条没有记录。
    #
    #     SIGINT 是 Python 的 KeyboardInterrupt，测试程序能接住：
    #     它会给自己人发信号，让在飞的请求主动收尾并记成 cancelled，
    #     然后才落盘退出。这样"被停止截断的请求"也有名字、有状态。
    #
    #  ⚠️ 但仍然要保留后退路径：SIGINT 若被忽略或卡住，
    #     12 秒后 SIGTERM，再 5 秒 SIGKILL —— 不能让"停止"停不下来。
    # ══════════════════════════════════════════════════════════════════════
    def _killpg(sig):
        try:
            os.killpg(os.getpgid(pid), sig)
            return True
        except ProcessLookupError:
            return False
        except Exception:                                     # noqa: BLE001
            try:
                os.kill(pid, sig)
                return True
            except Exception:                                 # noqa: BLE001
                return False

    def _wait_gone(seconds):
        """等进程退出，返回 True 表示已退"""
        for _ in range(int(seconds / 0.2)):
            if not _alive(pid):
                return True
            time.sleep(0.2)
        return False

    if not _killpg(signal.SIGINT):
        # 进程本来就没了
        pass
    elif not _wait_gone(12):
        log(f"测试页：SIGINT 后 12 秒仍未退出，改用 SIGTERM（PID {pid}）")
        _killpg(signal.SIGTERM)
        if not _wait_gone(5):
            log(f"测试页：SIGTERM 也无效，用 SIGKILL（PID {pid}）")
            _killpg(signal.SIGKILL)
            _wait_gone(3)

    st["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    st["stopped_reason"] = reason
    st["pid"] = None                    # 清掉，避免 active() 认为还在跑
    write_state(st)
    log(f"测试页：已停止测试 {st.get('label')}（{reason}）")
    return True, {"state": st}


# ══════════════════════════════════════════════════════════════════════════
#  状态查询
# ══════════════════════════════════════════════════════════════════════════

def current_run_dir():
    """
    返回【正在跑的那次测试】的结果目录（Path），判断不出来就返回 None。

    ⚠️ 为什么要单独提供一个：`service_watch` 需要把"服务被拉起过"这件事
       写进【那一次运行的目录里】。不写进去的话，事后就没法从运行数据
       本身看出"这次测试期间服务掉过" —— 只能靠人去翻控制层日志，
       而日志是会滚动的。

    ⚠️ 为什么这里用 results_before 判断目录，而不是比目录 mtime：
       时钟跳变时会误判。results_before 是开跑前记下的"已存在目录名"，
       当前目录 = 现在存在、但不在那个集合里的那个。
    """
    st = read_state()
    if not st:
        return None
    if not _alive(st.get("pid")):
        return None
    before = set(st.get("results_before") or [])
    if not RESULTS_ROOT.is_dir():
        return None
    cands = [d for d in sorted(RESULTS_ROOT.iterdir())
             if d.is_dir() and d.name not in before]
    return cands[-1] if cands else None


def last_run_dir(max_age_s=900):
    """
    返回【最近一次测试】的结果目录（Path），没有就 None。

    与 current_run_dir() 的区别：**测试已经退出也照样返回**（只要不太旧）。

    ⚠️ 为什么需要它（这是实测踩出来的时序问题）：
       服务在测试期间掉线时，监测器的处理链是
          发现掉线 → 自动重启 → 服务就绪 → 记录"服务被拉起"
       而重启要花 2~3 秒。如果测试本身很短，它会在"重启完成"之前
       就退出了。等 `_record_service_start` 去查当前运行目录时，
       `current_run_dir()` 因为进程已死返回 None ——
       于是这条关键记录【写不进运行目录】，结果页就仍然显示"完整"。
       实测时序：
           21:11:20  测试跑完退出
           21:11:22  监测器才发现服务不可达
           21:11:24  自动重启启动
           21:11:26  服务起来 → 想记进运行目录，但已经找不到目录了 ❌

    ⚠️ max_age_s 不是多余的：不设上限的话，一个人手动重启服务时
       也会往"昨天那次运行"里写一条记录，把历史数据污染掉。

    ⚠️ 注意不要拿 status() 来实现这个 —— 那个函数很重
       （要读 summary、比对模型），而这里只需一个目录名。
    """
    d = current_run_dir()
    if d is not None:
        return d

    st = read_state()
    if not st:
        return None

    # ① 首选用状态里记的 run_id（start() 很早就把它写进去了）
    rid = st.get("run_id")
    if rid:
        cand = RESULTS_ROOT / str(rid)
        if cand.is_dir():
            return cand

    # ② 兜底：从 results_before 推（测试已退出，进程查不到了）
    before = set(st.get("results_before") or [])
    if not RESULTS_ROOT.is_dir():
        return None
    cands = [x for x in sorted(RESULTS_ROOT.iterdir())
             if x.is_dir() and x.name not in before]
    if not cands:
        return None
    newest = cands[-1]
    try:
        if (time.time() - newest.stat().st_mtime) > max_age_s:
            return None
    except OSError:
        return None
    return newest


def status():
    """
    当前测试的状态（给网页轮询）。

    判断"跑完没有"用三个信号：
        PID 活着                                    → running
        PID 死了 + 出现新目录 + 有 summary.json      → finished
        PID 死了 + 出现新目录 + 没有 summary.json    → aborted（被中断）
        PID 死了 + 没有新目录                        → failed（没跑起来）
    """
    st = read_state()
    if not st:
        return {"has_record": False, "running": False}

    pid = st.get("pid")
    running = _alive(pid)
    out = {
        "has_record": True,
        "running": running,
        "pid": pid,
        "label": st.get("label"),
        "model_file": st.get("model_file"),
        "ctx": st.get("ctx"),
        "ctk": st.get("ctk"),
        "ctv": st.get("ctv"),
        "config_name": st.get("config_name"),
        "log_file": st.get("log_file"),
        "started_at": st.get("started_at"),
        "restarted_service": st.get("restarted_service"),
        "service_note": st.get("service_note"),
        "stopped_reason": st.get("stopped_reason"),
    }
    if st.get("started_ts"):
        out["elapsed_s"] = int(time.time() - st["started_ts"])

    # 本次运行产生的新目录
    before = set(st.get("results_before") or [])
    new_dirs = []
    if RESULTS_ROOT.is_dir():
        for d in sorted(RESULTS_ROOT.iterdir()):
            if d.is_dir() and d.name not in before:
                new_dirs.append(d)
    out["new_dirs"] = [d.name for d in new_dirs]

    if not running:
        if new_dirs:
            d = new_dirs[-1]
            has_summary = (d / "summary.json").is_file()
            out["finished"] = True
            out["run_id"] = d.name
            out["outcome"] = "finished" if has_summary else "aborted"
            if has_summary:
                try:
                    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
                    ov = s.get("overall") or {}
                    out["summary_brief"] = {
                        "n_requests": ov.get("n_requests"),
                        "n_success": ov.get("n_success"),
                        "success_rate": ov.get("success_rate"),
                        "duration_s": s.get("duration_s"),
                    }
                except (OSError, json.JSONDecodeError):
                    pass
            else:
                out["aborted_hint"] = ("目录里没有 summary.json —— "
                                       "多半是被中断或启动就失败了")
        else:
            out["finished"] = True
            out["outcome"] = "failed"
            out["failed_hint"] = ("没有产生结果目录 —— 多半是启动就失败了"
                                  "（参数校验不过 / 服务起不来 / 配置有问题）")
    else:
        out["finished"] = False
    return out


# ══════════════════════════════════════════════════════════════════════════
#  给网页的表单数据
# ══════════════════════════════════════════════════════════════════════════

def form_data():
    """测试页需要的全部数据，一次给全（避免前端连着发好几个请求）"""
    from . import models_config

    # 模型（只列已安装的）
    models = []
    reg, rerr = models_config.load_registry()
    if not rerr:
        for m in reg.get("models", []):
            path, ferr = config.find_model_path_by_name(m["file"])
            models.append({
                "file": m["file"], "id": m["id"],
                "params": m.get("params"), "quant": m.get("quant"),
                "installed": path is not None,
                "error": ferr,
            })

    # 模板配置（负载来源）
    templates = []
    if CONFIGS_DIR.is_dir():
        for p in sorted(CONFIGS_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            templates.append({
                "name": p.name,
                "label": d.get("label"),
                "n_tasks": len(d.get("task_ids") or []),
                "rounds": d.get("rounds"),
                "concurrency": d.get("concurrency"),
                "n_requests": (len(d.get("task_ids") or [])
                               * (d.get("rounds") or 0)
                               * len(d.get("concurrency") or [])),
                # ⭐ 模板里的输出上限也要给前端 —— 网页上这个字段的默认值
                #    就取它，保证"不改就是原来的行为"
                "max_tokens": d.get("max_tokens"),
                "note": d.get("note"),
            })

    cur, perr = _query_props()
    return {
        "models": models,
        "templates": templates,
        "kv_choices": [
            {"value": "f16", "desc": "16 位浮点，默认，精度最高、显存最大"},
            {"value": "q8_0", "desc": "约 8.5 位，显存约省一半，质量损失很小"},
            {"value": "q5_0", "desc": "约 5.5 位"},
            {"value": "q4_0", "desc": "约 4.5 位，显存约省 3/4，质量有可见下降"},
            {"value": "bf16", "desc": "bfloat16，16 位，范围大精度低"},
        ],
        "ctx_choices": [4096, 8192, 16384, 32768, 40960],
        "service": cur if cur else {"error": perr},
        "running": active(),
        "leftovers": list_leftovers(),
        "results_root": str(RESULTS_ROOT),
    }
