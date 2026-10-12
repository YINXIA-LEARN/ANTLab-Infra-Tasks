#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_bench.py —— 可重复运行的性能测试程序

对应题目「2. 性能测试与文件存储」：
    「开发可重复运行的测试程序，支持选择提示词、关联文件、运行配置、
      重复次数和并发数。结果按运行编号保存。」

结果目录结构（按【运行编号】组织）：
    <out>/
        runs.json           ⭐ 运行登记表：所有运行的一行摘要（前端读它列列表）
        <运行编号>/
            run-config.json     本次配置快照（复现用）
            meta.json           硬件/软件/模型/服务配置/负载配置
            requests.jsonl      ⭐ 逐请求原始记录（题目要求必须保留）
            summary.json        汇总统计（含中位数、P95）
            gpu-samples-c<N>.csv 显存采样
            errors.json         失败/超时/取消请求的详细记录
            prompt-index.json   提示词与文件校验值索引
            run.log             运行日志

运行编号格式： <标签>-<时间戳>
    baseline-c1-r1-20261010-085812
      └─ 标签 ─┘ └── 时间戳 ──┘

    ⭐ 带时间戳是为了【绝不覆盖】已有结果。
       只靠人写标签，重跑同名就会静默覆盖旧数据 ——
       报告里的数字和原始记录对不上，评委一核对就露馅。

六类必录字段的落地位置（题目「至少包括」）：
    ① 硬件信息、软件版本、模型信息、服务配置及测试负载 → meta.json
    ② 请求 ID、提示词编号、文件校验值、实际输入 token 数、
       输出 token 数、输出内容                          → requests.jsonl
    ③ 提交、首个有效生成内容、完成等时间点及相关耗时     → requests.jsonl
    ④ 成功、失败、超时、取消等状态，错误类型与信息        → requests.jsonl + errors.json
    ⑤ 基线和优化后结果、配置差异及汇总统计               → summary.json（config_diff 见本文件）
    ⑥ 逐请求原始记录保留，以便复核汇总                   → requests.jsonl
"""

import argparse
import csv
import json
import os
import platform
import queue
import random
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.request                             # noqa: F401
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import client                      # noqa: E402
from common import file_input, tasks          # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
#  环境与配置采集
# ══════════════════════════════════════════════════════════════════════════

def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:                                         # noqa: BLE001
        return None


def collect_hardware():
    """硬件信息（题意①）"""
    hw = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "mem_total_mib": None,
    }
    # CPU 型号在 Linux 上更准
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    hw["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    hw["mem_total_mib"] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass

    # GPU
    q = "name,memory.total,driver_version,compute_cap"
    out = _run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"])
    hw["gpu"] = out.splitlines() if out else None

    # CUDA 版本
    out = _run(["nvidia-smi"])
    if out:
        for line in out.splitlines():
            if "CUDA Version" in line:
                hw["cuda_version"] = line.split("CUDA Version:")[1].split()[0]
                break
    return hw


def collect_model_info(base_url):
    """模型信息（题意①）"""
    try:
        # urllib.request 已在文件顶部 import —— 早先这里写的是局部 import，
        # 结果新加的 current_service_state() 在另一个函数里用同名模块就
        # NameError 了。局部 import 只对它所在的函数可见，别在这里写。
        with urllib.request.urlopen(base_url.rstrip("/") + "/props", timeout=10) as r:
            d = json.loads(r.read())
        dgs = d.get("default_generation_settings") or {}
        return {
            "model_path": d.get("model_path"),
            "model_name": (d.get("model_path") or "").rsplit("/", 1)[-1],
            "quant": d.get("model_ftype"),
            "n_ctx": dgs.get("n_ctx"),
            "total_slots": d.get("total_slots"),
            "build_info": d.get("build_info"),
            "chat_template_present": bool(d.get("chat_template")),
        }
    except Exception as e:                                    # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def read_server_config():
    """
    服务配置（题意①）—— 从注册表和进程命令行里取。
    这些参数直接决定性能，必须随结果一起存。

    ⚠️ 路径层级：本文件在 <development>/src/bench/ 下，
       所以 parents[0]=bench [1]=src [2]=development [3]=仓库根。
       注册表在 <development>/configs/models.json —— 用 parents[2]。
       （迁移到 development/ 之后布局变了，早先写的 parent.parent 会
         指到 src/configs，读不到。）

    ⚠️ 键名：注册表里现在存的是 default_active（默认值），
       运行中的选择在 .runtime/active-model.json。
       这里两个键都认，是为了兼容还没改名的旧注册表。
    """
    cfg = {"models_registry": None, "active_model": None,
           "server_args": None, "reasoning": None}
    dev = Path(__file__).resolve().parents[2]
    reg = dev / "configs" / "models.json"
    if reg.is_file():
        try:
            d = json.loads(reg.read_text(encoding="utf-8"))
            cfg["models_registry"] = str(reg)
            active = d.get("default_active") or d.get("active")
            cfg["active_model"] = active
            for m in d.get("models", []):
                if m["id"] == active:
                    cfg["server_args"] = m.get("server_args")
                    break
        except Exception as e:                                # noqa: BLE001
            cfg["reasoning"] = f"读注册表失败：{e}"

    # ★ 运行时选择的模型（.runtime/active-model.json，不进 git）
    rt = dev / ".runtime" / "active-model.json"
    if rt.is_file():
        try:
            cfg["runtime_active_model"] = json.loads(
                rt.read_text(encoding="utf-8")).get("active")
        except Exception:                                     # noqa: BLE001
            pass

    # 模型服务自己的启动参数（从进程命令行取，最权威）
    #
    # ⭐ 为什么要存这个：
    #    题目要求「提供模型计算层和 KV 在 GPU 的日志与监控证据」，
    #    而 GPU 驻留行为由启动参数决定（-ngl / -nv / KV 位置 /
    #    是否关掉主机 RAM prompt cache）。
    #    只从注册表读 server_args 是不够的 —— 那只是"打算怎么起"，
    #    进程命令行才是"实际怎么起的"，两者可能不一致
    #    （比如注册表改了但服务没重启）。
    #    把它随结果存下来，报告里就能逐条对上，也能事后判断
    #    两次运行的口径是否真的可比。
    out = _run(["bash", "-c",
                "ps -o args= -p $(pgrep -f llama-server | head -1) 2>/dev/null"])
    cfg["process_cmdline"] = out

    # 从命令行里解析出几个关键开关，方便报告直接引用
    if out:
        toks = out.split()
        flags = {}

        def _val(name):
            if name in toks:
                i = toks.index(name)
                if i + 1 < len(toks):
                    return toks[i + 1]
            return None

        flags["ngl"] = _val("-ngl") or _val("--n-gpu-layers")
        flags["ctx"] = _val("-c") or _val("--ctx-size")
        flags["np"] = _val("-np") or _val("--parallel")
        flags["cache_ram"] = _val("--cache-ram") or _val("-cram")
        flags["cache_type_k"] = _val("-ctk") or _val("--cache-type-k")
        flags["cache_type_v"] = _val("-ctv") or _val("--cache-type-v")
        flags["flash_attn"] = _val("-fa") or _val("--flash-attn")
        flags["verbose"] = ("-v" in toks) or ("--verbose" in toks)
        # cache_ram 为 "0" 表示主机 RAM prompt cache 已关闭
        flags["prompt_cache_disabled"] = (flags["cache_ram"] == "0")
        cfg["server_flags"] = flags

    # 显存（模型加载后，题意"记录模型加载后"）
    out = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits"])
    if out:
        parts = [p.strip() for p in out.split(",")]
        cfg["vram_after_load_mib"] = int(parts[0]) if parts else None
        cfg["vram_total_mib"] = int(parts[1]) if len(parts) > 1 else None

    return cfg


class GpuSampler(threading.Thread):
    """
    显存采样线程（题意"记录…测试过程峰值"）。

    ⚠️ 属性名不能叫 _stop：
       threading.Thread 内部有个私有方法 Thread._stop()，
       用 self._stop = Event() 会把它覆盖掉，
       之后 t.join() 会去调用这个 Event 对象并抛
       TypeError: 'Event' object is not callable。
       这个坑只在 join() 时才暴露，容易拖到最后才发现。
    """

    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop_evt = threading.Event()
        self._t0 = None

    def run(self):
        self._t0 = time.perf_counter()
        while not self._stop_evt.is_set():
            out = _run(["nvidia-smi",
                        "--query-gpu=memory.used,utilization.gpu",
                        "--format=csv,noheader,nounits"], timeout=10)
            if out:
                try:
                    used, util = [int(x.strip()) for x in out.split(",")[:2]]
                    self.samples.append(
                        (round(time.perf_counter() - self._t0, 2), used, util))
                except ValueError:
                    pass
            self._stop_evt.wait(self.interval)

    def stop(self):
        self._stop_evt.set()

    def peak_mib(self):
        return max((s[1] for s in self.samples), default=None)


# ══════════════════════════════════════════════════════════════════════════
#  负载组织
# ══════════════════════════════════════════════════════════════════════════

def build_plan(task_ids, rounds, concurrency, seed, order="shuffled"):
    """
    组织固定负载（题意「用固定列表或固定随机种子组织负载」）。

    返回 [(wave_index, [task_id, ...]), ...]
      每个 wave 内最多 concurrency 个任务同时发出。

    ⚠️ 用固定种子打乱顺序，保证：
       · 同一 run 内不同 wave 的任务顺序不同（避免某个任务总在第一个）
       · 不同 run 之间顺序一致（种子相同 → 可比较）
    """
    rng = random.Random(seed)
    seq = list(task_ids) * rounds
    if order == "shuffled":
        rng.shuffle(seq)
    elif order == "fixed":
        pass
    else:
        raise ValueError(f"未知 order：{order}")

    waves = []
    for i in range(0, len(seq), concurrency):
        waves.append(seq[i:i + concurrency])
    return waves


# ══════════════════════════════════════════════════════════════════════════
#  汇总统计
# ══════════════════════════════════════════════════════════════════════════

def pct(values, p):
    """百分位数（线性插值），值列表为空返回 None"""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(records, wall_time_s=None):
    """
    汇总统计（题意⑤）。

    ⚠️ 吞吐量的窗口【必须】用外部传入的 wall_time_s，不能从记录里推：
       client.py 记的 t_submit / t_done 是【每条请求各自的相对时间】（各自从 0 开始），
       它们不在同一时间轴上。
       我一开始用 max(t_done) - min(t_sub) 当窗口，结果算出的是
       "最慢那条请求的耗时"（并发1只有 8.74s），而实际跑了 103.85s，
       吞吐被高估约 12 倍（2308 vs 194 tok/s）。
       这种错误很危险：数字看着很漂亮，但一验算就穿帮。

    ⚠️ 成功率的分母写清楚：是【发出的全部请求】。
       失败请求不计入延迟统计（否则会把均值拉飞），
       也不计入 token 吞吐（它们没产生输出）。
    """
    total = len(records)
    by_status = {}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    # ⚠️ 只有 status == "success" 才算成功。
    #    "incomplete" 是被中途掐断的（没有 finish_reason），
    #    它既不是成功（没答完），也不能混进延迟统计（否则均值被拉低）。
    ok = [r for r in records if r["status"] == "success"]
    ok_with_ttft = [r for r in ok if r["ttft_ms"] is not None]

    def col(rs, key):
        return [r[key] for r in rs if r.get(key) is not None]

    out_tokens = col(ok, "output_tokens")
    window = wall_time_s

    s = {
        "n_requests": total,
        "n_success": len(ok),
        "n_failed": by_status.get("failed", 0),
        "n_timeout": by_status.get("timeout", 0),
        "n_cancelled": by_status.get("cancelled", 0),
        "n_incomplete": by_status.get("incomplete", 0),
        "n_harness_error": by_status.get("harness_error", 0),
        "status_breakdown": by_status,
        "success_rate": (len(ok) / total) if total else None,
        "success_rate_note": ("分母 = 本次运行的请求总数（含失败与中断）；"
                              "只有拿到 finish_reason 的才算成功；"
                              "harness_error 表示测试程序自身的 bug，不是模型问题"),

        "ttft_ms": {
            "n": len(ok_with_ttft),
            "mean": statistics.fmean(col(ok_with_ttft, "ttft_ms")) if ok_with_ttft else None,
            "median": pct(col(ok_with_ttft, "ttft_ms"), 50),
            "p95": pct(col(ok_with_ttft, "ttft_ms"), 95),
            "min": min(col(ok_with_ttft, "ttft_ms")) if ok_with_ttft else None,
            "max": max(col(ok_with_ttft, "ttft_ms")) if ok_with_ttft else None,
        },
        "total_ms": {
            "n": len(ok),
            "mean": statistics.fmean(col(ok, "total_ms")) if ok else None,
            "median": pct(col(ok, "total_ms"), 50),
            "p95": pct(col(ok, "total_ms"), 95),
        },
        "tpot_excl_first_ms": {
            "n": len([r for r in ok if r.get("tpot_excl_first_ms") is not None]),
            "mean": statistics.fmean(col(ok, "tpot_excl_first_ms"))
                    if col(ok, "tpot_excl_first_ms") else None,
            "median": pct(col(ok, "tpot_excl_first_ms"), 50),
            "p95": pct(col(ok, "tpot_excl_first_ms"), 95),
            "note": "公式：(完成 − 首字) / (输出token − 1)，排除首 token",
        },
        "tokens": {
            "output_total": sum(out_tokens) if out_tokens else 0,
            "output_mean": statistics.fmean(out_tokens) if out_tokens else None,
            "prompt_total": sum(col(ok, "prompt_tokens")) or 0,
            "cache_hit_total": sum(col(ok, "cache_hit_tokens")) or 0,
        },
        "throughput": {
            "window_s": window,
            "window_note": "该并发档的实测墙钟时间（从首个 wave 开始到最后一个 wave 结束，"
                           "含排队与传输）",
            "output_tokens_per_s": (sum(out_tokens) / window)
                                   if (out_tokens and window) else None,
            "requests_per_s": (total / window) if window else None,
            "failed_counted": False,
            "note": "失败请求不计入 token 吞吐（未产生输出）；"
                    "分母用墙钟时间，不用逐请求相对时间推算",
        },
        "finish_reason": {},
    }
    for r in ok:
        fr = r.get("finish_reason") or "unknown"
        s["finish_reason"][fr] = s["finish_reason"].get(fr, 0) + 1

    # 非成功请求按任务分布 —— 一眼看出是哪些任务在撞上下文上限
    bad_by_task = {}
    for r in records:
        if r["status"] != "success":
            t = r.get("task_id") or "?"
            bad_by_task.setdefault(t, {})
            bad_by_task[t][r["status"]] = bad_by_task[t].get(r["status"], 0) + 1
    s["problems_by_task"] = bad_by_task

    # 按任务看输入规模 —— 定位"哪个任务把 KV 池撑爆"
    biggest = sorted(
        ((r.get("task_id"), r.get("prompt_tokens") or 0) for r in records),
        key=lambda x: -x[1])[:3]
    s["largest_inputs"] = [{"task_id": t, "prompt_tokens": n} for t, n in biggest]
    return s


# ══════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════

def verify_note_scope(cfg):
    """
    校验 note 只描述【本轮自己】的参数，不得与其他运行产生关联。

    ── 为什么定这条规矩 ─────────────────────────────────────────────
      我原来给优化轮写的备注是：
          "优化轮-1：-c 16384 → 32768（唯一改动），其余同基线"
      这句话有两个问题：
        ① 它把自己【锚定在某一轮基线】上。等以后又跑基线第 2 轮、
           或再跑别的优化轮，这句"唯一改动/其余同基线"就失效了 ——
           新基线不一定还是 16384，那份备注会变成错误信息。
        ② note 会被原样抄进 run-config.json、runs.json、对比页和报告。
           一旦它写了跨轮次的话，看的人就分不清"同基线"是同哪一轮。

      所以规定：note 只写【本轮自己的参数】，做成自洽的事实陈述。
      轮次之间的关系交给对比功能去算（它比的是机器读到的 server_args）。

    返回 (是否合规, 问题说明)。不合规时调用方应终止运行。
    """
    note = (cfg.get("note") or "").strip()
    if not note:
        return False, "note 不能为空（用来记录本轮参数，报告里要引用）"

    problems = []

    # ① 箭头：暗示"从 A 改成 B"，是跨轮次表述
    for arrow in ("->", "→", "-->", "=>"):
        if arrow in note:
            problems.append(f"出现箭头 {arrow!r}（暗示与别的轮次比较）")

    # ② 相对性措辞
    for w in ("同基线", "同上", "其余同", "唯一改动", "相比", "对比上",
              "较上", "相比之前", "前一轮", "上一轮", "同前", "与…相同"):
        if w in note:
            problems.append(f"出现相对性措辞 {w!r}（依赖别的运行才能理解）")

    # ③ 直接点名别的运行编号
    for w in ("baseline-", "opt-", "baseline_", "opt_"):
        if w in note:
            problems.append(f"提到了别的运行编号前缀 {w!r}")
    if "run-" in note and "run-" + time.strftime("%Y") not in note:
        problems.append("提到了别的运行编号")

    # ④ 提到"基线/优化轮"这类角色词
    for w in ("基线", "优化轮", "对照组", "实验组"):
        if w in note:
            problems.append(
                f"出现角色词 {w!r}（那是相对别的运行才成立的说法，"
                f"请改成只描述本轮参数）")

    if problems:
        return False, ("note 只能写本轮自己的参数，不能与其他运行关联。\n"
                       + "\n".join("        · " + p for p in problems)
                       + "\n      当前 note：" + note
                       + "\n      建议改成，例如：\n"
                       "        \"0.6B Q8_0 · -c 16384 · ngl 99 · "
                       "10 任务 × 3 轮 × 并发[1,4,10] · seed 20261009 · "
                       "max_tokens 2048 · temp 0\"")
    return True, note


def load_config_from_defaults():
    """
    不给 --config 时的内置默认值（交互模式用）。

    ⚠️ 值必须和 bench/configs/baseline-full.json 保持一致 ——
       否则"交互模式"和"配置模式"跑出来的负载不是一回事，没法对比。
       所以这里不另发明一套默认值。
    """
    return load_config_dict({
        "label": "run",
        "task_ids": tasks.all_ids(),
        "rounds": 3,
        "concurrency": [1, 4, 10],
        "seed": 20261009,
        "order": "shuffled",
        "max_tokens": 2048,
        "temperature": 0.0,
        "timeout_s": 900,
        "warmup": {"enabled": True, "n": 2, "task_id": "P01"},
    })


def load_config_dict(d):
    """把 load_config 的那套默认值补齐逻辑抽出来，供 dict 入口复用"""
    d = dict(d)
    d.setdefault("base_url", "http://127.0.0.1:8080")
    d.setdefault("input_dir", str(
        Path.home() / "antlab/ANTLab-Infra-Tasks/Basic Task/"
                      "提示词与输入文档/输入文档"))
    d.setdefault("model_file", "Qwen3-0.6B-Q8_0.gguf")
    d.setdefault("task_ids", tasks.all_ids())
    d.setdefault("concurrency", [1, 4, 10])
    d.setdefault("rounds", 3)
    d.setdefault("max_tokens", 2048)
    d.setdefault("temperature", 0.0)
    d.setdefault("seed", 20261009)
    d.setdefault("order", "shuffled")
    d.setdefault("timeout_s", 900)
    d.setdefault("warmup", {"enabled": True, "n": 2, "task_id": "P01"})
    if "label" not in d and "run_id" in d:
        d["label"] = d.pop("run_id")
    d.setdefault("label", "run")
    return d


def load_config(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_config_dict(d)


def prepare_inputs(cfg, log):
    """
    预先组装所有任务的 messages 与 token 数。
    同时产出 prompt-index.json（含文件校验值）。

    ⚠️ 用 prepare_all 而不是循环调 prepare：
       前者把所有任务的计数集中成少数几次模型加载，
       10 个任务约 2 秒；后者每个任务加载一次，要 15 秒以上。

    ⚠️ 这个函数曾经被我误删过（编辑时 old_string 用它的 def 行做锚点，
       替换后函数头没了，只剩调用处）。所以下面的"函数完整性自查"
       要一直留着 —— 它能提前发现这类"定义消失"的问题。
    """
    res = file_input.prepare_all(
        cfg["task_ids"], cfg["input_dir"], cfg["model_file"],
        n_ctx=999999, max_output_tokens=cfg["max_tokens"],
        base_url=cfg["base_url"], want_probe=True)

    index = {}
    for tid, info in res.items():
        tk = info["tokens"]
        # ⚠️ 两个口径都在，别混：
        #   local_sum   —— 本地用 llama-tokenize 分段数的和（不含聊天模板包装）
        #   server_prompt_tokens —— 服务端实测的"聊天模板处理后"的输入总数
        #   题目要求记录的是后者；前者用来解释 token 花在哪一段。
        index[tid] = {
            "task_id": tid,
            "title": info["task_title"],
            "kind": info["task_kind"],
            "file": (info["doc"] or {}).get("name"),
            "file_sha256": (info["doc"] or {}).get("sha256"),
            "file_bytes": (info["doc"] or {}).get("bytes"),
            "system_message": info["system_message"],
            "tokens": {
                "system": tk["system"],
                "doc": tk["doc"],
                "task": tk["task"],
                "sum_without_template": tk["local_sum"],
                "template_overhead": (tk["server_prompt_tokens"] - tk["local_sum"])
                                     if tk["server_prompt_tokens"] else None,
                "counted_total_input": tk["server_prompt_tokens"],
                "counted_total_source": ("server usage.prompt_tokens"
                                         if tk["server_prompt_tokens"] else None),
            },
        }
        srv = tk["server_prompt_tokens"]
        log(f"  组装 {tid}: 本地分段合计 {tk['local_sum']} token"
            + (f"，服务端实测输入 {srv}（模板开销 {srv - tk['local_sum']}）"
               if srv else "，服务端实测失败")
            + (f"，文件 {index[tid]['file']}" if index[tid]["file"] else ""))
    return res, index


# ══════════════════════════════════════════════════════════════════════════
#  函数完整性自查
#
#  ⚠️ 为什么要有这个：
#     我用编辑工具改代码时，曾把 prepare_inputs 整个函数头吃掉
#     （old_string 正好是它的 def 行），结果语法照样通过、
#     --help 照样能跑，直到真正执行到那一行才 NameError。
#     这个自查把"应该存在的函数"列出来，import 时立刻验证，
#     把这类问题从"跑到一半才炸"提前到"一启动就报"。
# ══════════════════════════════════════════════════════════════════════════

_REQUIRED_FUNCS = [
    # 配置与校验
    "load_config", "load_config_dict", "load_config_from_defaults",
    "verify_note_scope", "verify_model_matches_config",
    "verify_service_matches_config",
    # 输入与负载
    "prepare_inputs", "build_plan",
    # 服务
    "read_server_config", "current_service_state",
    "restart_with_params", "plan_service_change", "find_serve_sh",
    # 交互
    "collect_interactive", "answers_to_config",
    # 执行与汇总
    "run_one_wave", "summarize",
    # 落盘
    "make_run_id", "update_runs_registry", "make_registry_record",
    # 入口
    "main",
]


def _self_check():
    """启动时验证关键函数都在 —— 缺了就立刻报，别等跑到那行才 NameError"""
    missing = [n for n in _REQUIRED_FUNCS if n not in globals()]
    if missing:
        sys.stderr.write(
            "❌ run_bench.py 自身不完整，缺少函数：\n"
            + "".join(f"     · {n}\n" for n in missing)
            + "   （多半是编辑时误删了定义 —— 从 git 历史取回来）\n")
        raise SystemExit(90)


# ══════════════════════════════════════════════════════════════════════════
#  交互式配置（--interactive）
#
#  设计要点（都是"避免残留"和"保持可追踪"考虑过的）：
#    ① 只在这里【收集答案】，不在这里启动服务 ——
#       启动/重启统一由 service_apply() 负责，而且放在【建结果目录之前】，
#       这样重启失败不会留下任何空目录或半截数据。
#    ② 收集完的答案走同一条 load_config() 路径（这里手工补默认值），
#       后面的 note 校验 / 模型校验 / 输入组装 / 落盘全都不用改。
#    ③ note 自动从答案生成，天然满足"只写本轮参数"的规范。
#    ④ label 给默认值，可改 —— 因为不同参数的运行要有可区分的编号。
# ══════════════════════════════════════════════════════════════════════════

# KV 精度档位（llama.cpp 的合法取值，实测确认）
KV_CHOICES = [
    ("1", "f16",  "16 位浮点，默认值，精度最高、显存最大"),
    ("2", "q8_0", "约 8.5 位，显存约省一半，质量损失很小"),
    ("3", "q5_0", "约 5.5 位"),
    ("4", "q4_0", "约 4.5 位，显存约省 3/4，质量有可见下降"),
    ("5", "bf16", "bfloat16，16 位，范围大精度低"),
]
# 常见上下文档位（0.6B 的上限是 40960，1.7B 也是）
CTX_CHOICES = ["4096", "8192", "16384", "32768", "40960"]


def _ask(prompt, default=None, validator=None, hint=None):
    """
    问一个问题，返回字符串。

    ⚠️ 四个必须处理的边界，否则脚本在非交互环境下会炸、被卡住或输出乱掉：
       · EOF（管道输入用完 / Ctrl+D）→ 用默认值，别再问
       · 空输入                       → 用默认值
       · 校验不过                     → 重问（最多 5 次，防死循环）
       · **input() 后要自己补一个换行**：
         交互终端里用户按的回车会回显，但【管道输入不会】——
         那个 \\n 被当作行结束符吃掉了，不打印。
         于是用管道跑时会出现 "问题:       ❌ 错误信息" 挤在同一行。
         补一个 print() 让两种输入方式输出一致。
    """
    for attempt in range(5):
        if hint:
            print(f"      {hint}")
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            print()                                     # 补换行
            if default not in (None, ""):
                print(f"      （输入结束，采用默认值 {default}）")
                return str(default)
            print("      ❌ 输入结束且没有默认值")
            raise SystemExit("交互输入中断")
        print()                                          # 补换行（见上）
        if not raw and default not in (None, ""):
            return str(default)
        if validator:
            ok, why = validator(raw)
            if not ok:
                print(f"      ❌ {why}")
                continue
        return raw
    raise SystemExit("连续 5 次输入无效，已中止")


def _ask_choice(prompt, choices, default_key=None):
    """
    从固定选项里选一个。choices 是 [(key, value, 说明), ...]
    返回 value。输入原始值（如 'f16'）也接受。
    """
    for k, v, desc in choices:
        mark = " ←默认" if k == default_key else ""
        print(f"        {k}) {v:6s} {desc}{mark}")
    for _ in range(5):
        raw = _ask(prompt, default=default_key)
        for k, v, _d in choices:
            if raw == k or raw.lower() == v.lower():
                return v
        print(f"      ❌ 只认上面列出的编号或取值")
    raise SystemExit("连续 5 次输入无效，已中止")


def collect_interactive(cfg_base, log=None):
    """
    依次问一遍参数，返回一个 answers dict（不启动服务、不建目录）。

    cfg_base 是配置模板（来自 --config 或内置默认），
    用它填默认值 —— 这样"回车"就能得到和原来一样的配置。
    """
    print()
    print("=" * 72)
    print(" 交互式测试配置")
    print("=" * 72)
    print(" （直接回车 = 用方括号里的默认值）")
    print()

    # ── 模型 ─────────────────────────────────────────────────────────
    reg, err = _read_registry_for_menu()
    if err:
        print(f"  ⚠️ 读不到注册表：{err}")
        print("     将只提供默认模型。")
        models = [(cfg_base.get("model_file", "Qwen3-0.6B-Q8_0.gguf"), "")]
    else:
        models = [(m["file"], f"{m.get('params','?')} / {m.get('quant','?')}")
                  for m in reg.get("models", [])]

    # ⚠️ 模型清单【不要】在这里打印 ——
    #    _ask_choice() 内部会按统一格式打印一遍（含"←默认"标记）。
    #    早先这里也打了一遍，结果列表出现两次。
    default_idx = next((str(i) for i, (f, _) in enumerate(models)
                        if f == cfg_base.get("model_file")), "0")
    model_file = _ask_choice(
        "你想使用什么模型",
        [(str(i), f, d) for i, (f, d) in enumerate(models)],
        default_key=default_idx)

    # ── 上下文 ───────────────────────────────────────────────────────
    print()
    print(f"  常见上下文档位：{', '.join(CTX_CHOICES)}")
    print("        （KV 池总容量，单位 token；4 个 slot 共享这一个池）")

    def _ctx_ok(s):
        if not s.isdigit():
            return False, "要输入整数"
        v = int(s)
        if v < 512:
            return False, "太小了（至少 512）"
        if v > 40960:
            return False, "超过模型上限 40960（0.6B/1.7B 都是这个上限）"
        return True, ""

    ctx = int(_ask("上下文为多少", default=cfg_base.get("ctx_default", 16384),
                   validator=_ctx_ok))

    # ── KV 精度（K/V 合并成一个设置）────────────────────────────────
    print()
    print("  可选 KV 精度（K 和 V 用同一档）：")
    kv = _ask_choice("KV 量化为", KV_CHOICES, default_key="1")

    # 进阶：允许分开设（默认不用）
    print()
    sep = _ask("要不要给 K 和 V 分别设精度？(y/N)", default="n")
    ctk = ctv = kv
    if sep.lower().startswith("y"):
        print("  K 的精度（决定「该关注谁」，对输出质量更敏感）：")
        ctk = _ask_choice("K", KV_CHOICES, default_key="1")
        print("  V 的精度（参与加权求和，相对不敏感）：")
        ctv = _ask_choice("V", KV_CHOICES, default_key="1")

    # ── 编号 ─────────────────────────────────────────────────────────
    print()
    label = _ask("运行标签（决定结果目录名前缀）",
                 default=cfg_base.get("label", "run"))

    return {
        "model_file": model_file,
        "ctx": ctx,
        "ctk": ctk,
        "ctv": ctv,
        "label": label,
    }


def _read_registry_for_menu():
    """给交互菜单读注册表（模型清单 + 默认 ctx）。返回 (registry, err)"""
    dev = Path(__file__).resolve().parents[2]
    p = dev / "configs" / "models.json"
    if not p.is_file():
        return None, f"找不到 {p}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def answers_to_config(answers, base):
    """
    把交互答案合成一份完整配置（走和 load_config 一样的默认值补齐）。

    ⚠️ 为什么不让调用方各自补默认值：
       补默认值的地方一多，就会出现"交互模式漏补了某字段"这类问题。
       所以统一走这里，并且复用 load_config 的那套 setdefault。
    """
    d = dict(base)
    d.pop("_说明", None)              # 配置里的注释不进快照
    d.pop("_note_规范", None)
    d["model_file"] = answers["model_file"]
    d["label"] = answers["label"]
    # ctx / KV 精度不属于"请求参数"，但必须记进配置 ——
    # 否则 run-config.json 里看不出这次用的是哪个上下文和 KV 精度。
    d["server_ctx"] = answers["ctx"]
    d["kv_type_k"] = answers["ctk"]
    d["kv_type_v"] = answers["ctv"]
    # note 自动生成：只写本轮参数，天然满足规范
    d["note"] = (
        f"{Path(answers['model_file']).stem} · ctx {answers['ctx']} · "
        f"KV {answers['ctk']}/{answers['ctv']} · "
        f"{len(d.get('task_ids') or [])} 任务 x {d.get('rounds')} 轮 x "
        f"并发{d.get('concurrency')} · seed {d.get('seed')} · "
        f"max_tokens {d.get('max_tokens')} · temp {d.get('temperature')}"
    )
    return d
    """
    预先组装所有任务的 messages 与 token 数。
    同时产出 prompt-index.json（含文件校验值）。

    ⚠️ 用 prepare_all 而不是循环调 prepare：
       前者把所有任务的计数集中成少数几次模型加载，
       10 个任务约 2 秒；后者每个任务加载一次，要 15 秒以上。
    """
    res = file_input.prepare_all(
        cfg["task_ids"], cfg["input_dir"], cfg["model_file"],
        n_ctx=999999, max_output_tokens=cfg["max_tokens"],
        base_url=cfg["base_url"], want_probe=True)

    index = {}
    for tid, info in res.items():
        tk = info["tokens"]
        # ⚠️ 两个口径都在，别混：
        #   local_sum   —— 本地用 llama-tokenize 分段数的和（不含聊天模板包装）
        #   server_prompt_tokens —— 服务端实测的"聊天模板处理后"的输入总数
        #   题目要求记录的是后者；前者用来解释 token 花在哪一段。
        index[tid] = {
            "task_id": tid,
            "title": info["task_title"],
            "kind": info["task_kind"],
            "file": (info["doc"] or {}).get("name"),
            "file_sha256": (info["doc"] or {}).get("sha256"),
            "file_bytes": (info["doc"] or {}).get("bytes"),
            "system_message": info["system_message"],
            "tokens": {
                "system": tk["system"],
                "doc": tk["doc"],
                "task": tk["task"],
                "sum_without_template": tk["local_sum"],
                "template_overhead": (tk["server_prompt_tokens"] - tk["local_sum"])
                                     if tk["server_prompt_tokens"] else None,
                "counted_total_input": tk["server_prompt_tokens"],
                "counted_total_source": ("server usage.prompt_tokens"
                                         if tk["server_prompt_tokens"] else None),
            },
        }
        srv = tk["server_prompt_tokens"]
        log(f"  组装 {tid}: 本地分段合计 {tk['local_sum']} token"
            + (f"，服务端实测输入 {srv}（模板开销 {srv - tk['local_sum']}）"
               if srv else "，服务端实测失败")
            + (f"，文件 {index[tid]['file']}" if index[tid]["file"] else ""))
    return res, index


def run_one_wave(wave_tasks, cache, cfg, client_id, results):
    """并发发出一个 wave 的所有请求，等全部完成"""
    q = queue.Queue()
    for tid in wave_tasks:
        q.put(tid)

    def worker(worker_idx):
        while True:
            try:
                tid = q.get_nowait()
            except queue.Empty:
                return
            info = cache[tid]
            try:
                r = client.run_one(
                    cfg["base_url"], info["messages"],
                    max_tokens=cfg["max_tokens"],
                    temperature=cfg["temperature"],
                    timeout=cfg["timeout_s"],
                    client_id=f"{client_id}-w{worker_idx}",
                )
            except Exception as e:                            # noqa: BLE001
                # ⚠️ 不能把这里的异常当成"请求失败"记下来 —— 那会把
                #    【我的代码 bug】伪装成【服务端/网络问题】。
                #    我踩过这个坑：RequestResult 少了个 get() 方法，
                #    结果 16 条记录写着 "failed"，错误信息却是
                #    "AttributeError: ... has no attribute 'get'"，
                #    排查了半天才发现问题在自己代码里。
                #    所以这里单独标记为 harness_error，并带完整堆栈。
                r = client.RequestResult(client.make_request_id(), tid)
                r["status"] = "harness_error"
                r["error_type"] = "HarnessError"
                r["error_message"] = f"{type(e).__name__}: {e}"
                r["error_traceback"] = traceback.format_exc()
                r["client_id"] = f"{client_id}-w{worker_idx}"
            r["task_id"] = tid
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(len(wave_tasks))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ══════════════════════════════════════════════════════════════════════════
#  运行编号
# ══════════════════════════════════════════════════════════════════════════

def make_run_id(label, out_root, force=False):
    """
    生成运行编号，并保证【绝不覆盖已有结果】。

    编号格式： <标签>-<时间戳>
        标签    = 配置里的 label，形如 baseline-c1-r1（配置名-并发-轮次）
        时间戳  = YYYYmmdd-HHMMSS

    例子： baseline-c1-r1-20261010-085812

    ── 为什么用时间戳而不是随机数（这里绕过一个设计选择，记下来）──────
      最初想过加两位随机数（-0858）来防重名，但随机数有三个问题：
        ① 两位只有 100 种可能，跑十来次就有约 50% 概率撞上 —— 仍是覆盖
        ② 不能排序：-0858 和 -0025 哪个先跑的，看不出来
        ③ 看着像序号其实是随机，会误导人
      时间戳一次解决三个问题：确定不重复、ls 出来就是时间序、自带时间信息。

    ── 为什么不直接拒绝重名，而是加时间戳 ──────────────────────────
      拒绝重名（目录存在就报错）更严格，但每次重跑都要人手动处理，
      实测中很容易变成"随手删掉旧目录" —— 那反而更容易丢数据。
      加时间戳后：想重跑就直接跑，旧数据一定留着。

    ── 关于轮次 r ─────────────────────────────────────────────────
      约定 r 轮次【唯一】：r1/r2/r3 是三次独立测量。
      如果某轮作废要重跑，占用【新的】轮次号（比如 r4），
      而不是复用 r1 —— 否则"r1 出现两次"，报告里说不清用的是哪次。
    """
    now = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{label}-{now}"
    out = out_root / run_id

    # 同一秒内跑两次（或 label 里已带时间戳）的极端情况：加序号兜底
    if out.exists():
        n = 2
        while (out_root / f"{run_id}-{n}").exists():
            n += 1
        if not force:
            print(f"⚠️  {out.name} 已存在（同一秒内重复运行？）"
                  f"，自动改用 {run_id}-{n}")
        run_id = f"{run_id}-{n}"
        out = out_root / run_id

    return run_id, label, out


def update_runs_registry(out_root, record):
    """
    维护 test_runs/runs.json —— 所有运行的登记表。

    ── 为什么需要它 ──────────────────────────────────────────────────
      加了时间戳之后目录不会覆盖，但也带来新问题：跑多了找不到。
      30 个目录，想知道"优化后并发 4 的结果在哪"，得一个个翻 meta.json。
      登记表把每次运行的一行摘要集中起来，前端直接读它列列表。
      而且按 label 分组就能【自动配对】基线 vs 优化，不用人工找。

    写入是读-改-写的，所以先读旧的、追加、再整体写回。
    """
    path = Path(out_root) / "runs.json"
    runs = []
    if path.is_file():
        try:
            runs = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(runs, list):
                runs = []
        except (ValueError, OSError):
            # 登记表坏了不能影响测试结果，重建即可（结果目录本身才是权威）
            runs = []

    runs = [r for r in runs if r.get("run_id") != record["run_id"]]
    runs.append(record)
    runs.sort(key=lambda r: r.get("started_at") or "")

    try:
        path.write_text(json.dumps(runs, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return True
    except OSError:
        return False


def make_registry_record(run_id, label, cfg, meta, summaries, overall,
                         out_dir, duration_s):
    """把一次运行压成登记表里的一行"""
    by_conc = {}
    for k, s in summaries.items():
        by_conc[k] = {
            "n_requests": s["n_requests"],
            "n_success": s["n_success"],
            "success_rate": s["success_rate"],
            "ttft_median_ms": s["ttft_ms"]["median"],
            "ttft_p95_ms": s["ttft_ms"]["p95"],
            "tpot_median_ms": s["tpot_excl_first_ms"]["median"],
            "total_median_ms": s["total_ms"]["median"],
            "throughput_tps": s["throughput"]["output_tokens_per_s"],
            "vram_peak_mib": s.get("vram_peak_mib"),
        }
    return {
        "run_id": run_id,
        "label": label,
        "note": cfg.get("note"),
        "dir": str(out_dir.name),
        "started_at": meta["started_at"],
        "duration_s": round(duration_s, 1),
        "model": meta["model"].get("model_name"),
        "quant": meta["model"].get("quant"),
        "n_ctx": meta["model"].get("n_ctx"),
        "total_slots": meta["model"].get("total_slots"),
        "server_args": (meta.get("server_config") or {}).get("server_args"),
        "vram_after_load_mib": (meta.get("server_config") or {}).get(
            "vram_after_load_mib"),
        "task_ids": cfg["task_ids"],
        "rounds": cfg["rounds"],
        "concurrency": cfg["concurrency"],
        "seed": cfg["seed"],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        "by_concurrency": by_conc,
        "overall": {
            "n_requests": overall["n_requests"],
            "n_success": overall["n_success"],
            "success_rate": overall["success_rate"],
        },
    }


def verify_model_matches_config(cfg, model_info, log):
    """
    校验【配置里声称的模型】与【服务实际加载的模型】是否一致。

    ── 为什么必须校验（这是个真隐患）────────────────────────────────
      测试程序不会自己切换模型，它只会把请求发到 base_url 指向的服务上，
      然后照实记录【实际在跑的那个模型】。

      所以如果"配置写着 0.6B、实际在跑 1.7B"：
        · 数据本身是真实的（确实测了 1.7B）
        · 但 run-config.json 写着 0.6B  →【标签和事实不符】
        · 而且 prompt-index.json 的 token 数是按 cfg.model_file 算的（0.6B 词表）
        · 报告里就会把 1.7B 的成绩写成 0.6B 的

      这类错误不会报错，只会让整批数据失去意义，所以必须在开跑前拦住。

    返回 (是否一致, 说明)。不一致时调用方应终止运行。
    """
    want = cfg.get("model_file")
    got = model_info.get("model_name")

    if not got:
        return False, ("读不到服务实际加载的模型（模型服务没运行？）"
                       + (f"；服务返回：{model_info.get('error')}"
                          if model_info.get("error") else ""))
    if not want:
        return True, f"配置未指定 model_file，按服务实际加载的 {got} 记录"

    if want != got:
        return False, (
            f"模型不匹配：\n"
            f"        配置里写的是   {want}\n"
            f"        服务实际在跑   {got}\n"
            f"      解决办法（二选一）：\n"
            f"        ① 把模型切回配置指定的那个"
            f"（前端下拉框，或改 configs/models.json 的 active 后跑 serve.sh model）\n"
            f"        ② 或者把配置文件里的 model_file 改成 {got}\n"
            f"      注意：token 计数用的是配置里的词表，模型不匹配时 token 数也会不对")
    return True, f"模型一致：{got}"


def find_serve_sh():
    """定位 serve.sh（本文件在 <development>/src/bench/ 下）"""
    p = Path(__file__).resolve().parents[1] / "serve.sh"
    return p if p.is_file() else None


def current_service_state(base_url):
    """
    问服务当前实际状态：模型 / ctx / KV 精度 / kv_unified。
    用来判断"要不要重启"，以及事后核对参数真的生效了。
    """
    out = {"model_file": None, "ctx": None, "slots": None,
           "kv_unified": None, "ctk": None, "ctv": None, "cmdline": None,
           "error": None}
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/props")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        dgs = d.get("default_generation_settings") or {}
        out["model_file"] = (d.get("model_path") or "").rsplit("/", 1)[-1] or None
        out["ctx"] = dgs.get("n_ctx")
        out["slots"] = d.get("total_slots")
        out["kv_unified"] = dgs.get("kv_unified")
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    # 命令行里的 KV 精度（/props 不给，从进程命令行读）
    cmd = _run(["bash", "-c",
                "ps -o args= -p $(pgrep -f llama-server | head -1) 2>/dev/null"])
    out["cmdline"] = cmd
    if cmd:
        toks = cmd.split()

        def _val(*names):
            for n in names:
                if n in toks and toks.index(n) + 1 < len(toks):
                    return toks[toks.index(n) + 1]
            return None
        out["ctk"] = _val("-ctk", "--cache-type-k")
        out["ctv"] = _val("-ctv", "--cache-type-v")
    return out


def restart_with_params(model_file, ctx, ctk, ctv, log):
    """
    用指定的模型/上下文/KV 精度重启模型服务，并等就绪。

    返回 (是否成功, 说明)。失败时服务可能处于"没起来"的状态 ——
    调用方必须【终止整个运行】，不能带着坏服务继续跑。

    ⚠️ 为什么走 serve.sh 而不是自己拼 llama-server 命令行：
       启动逻辑（停旧进程、PID 文件、就绪判定、-v/--cache-ram 0）
       已经集中在 serve.sh 的 model_start() 里。测试脚本再拼一份，
       就会出现【两处启动逻辑】，迟早不一致
       （我们已经被 serve.sh 硬编码 ctx 和注册表不一致坑过一次）。
       所以这里只负责"把参数告诉它"，怎么起由它决定。
    """
    serve = find_serve_sh()
    if serve is None:
        return False, f"找不到 serve.sh（应在 {serve}）"

    override = json.dumps({
        "file": model_file,
        "ctx": ctx,
        "ctk": ctk,
        "ctv": ctv,
    }, ensure_ascii=False)

    log(f"  重启模型服务：{model_file} / ctx {ctx} / KV {ctk}-{ctv}")
    env = dict(os.environ)
    env["ANTLAB_OVERRIDE"] = override

    try:
        r = subprocess.run(
            ["bash", str(serve), "model"],
            cwd=str(serve.parent), env=env,
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "serve.sh model 超过 180 秒未返回"
    except Exception as e:                                    # noqa: BLE001
        return False, f"调用 serve.sh 失败：{type(e).__name__}: {e}"

    combined = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "就绪" not in combined:
        tail = "\n".join(combined.strip().splitlines()[-12:])
        return False, f"服务未就绪（退出码 {r.returncode}）：\n{tail}"

    # serve.sh 说就绪了，但还要独立核对一遍【参数是否真的生效】——
    # 不能只信它打印了"就绪"三个字。
    st = current_service_state("http://127.0.0.1:8080")
    if st["model_file"] != model_file:
        return False, (f"重启后模型不对：期望 {model_file}，"
                       f"实际 {st['model_file']}")
    if st["ctx"] != ctx:
        return False, f"重启后 ctx 不对：期望 {ctx}，实际 {st['ctx']}"
    # KV 精度：只有显式给了才可能出现在命令行里；没给就是 llama 默认 f16
    want_ctk = ctk if ctk != "f16" else None
    if want_ctk and st["ctk"] != want_ctk:
        return False, (f"重启后 ctk 不对：期望 {want_ctk}，实际 {st['ctk']}")
    return True, (f"就绪：{st['model_file']} / ctx {st['ctx']} / "
                  f"slots {st['slots']} / KV {st['ctk'] or 'f16(默认)'}"
                  f"-{st['ctv'] or 'f16(默认)'}")


def plan_service_change(cfg, answers, log):
    """
    算一下"要不要重启服务"，返回 (需要重启吗, 说明)。
    只比较能通过 /props 观察到的项，避免无谓重启（每次约 5 秒）。
    """
    st = current_service_state(cfg["base_url"])
    if st["error"]:
        return True, f"服务当前不可达（{st['error']}），需要启动"
    same_model = (st["model_file"] == cfg["model_file"])
    same_ctx = (st["ctx"] == answers["ctx"])
    # KV：只有期望值不是 f16 时才需要显式带上参数
    want_ctk = None if answers["ctk"] == "f16" else answers["ctk"]
    want_ctv = None if answers["ctv"] == "f16" else answers["ctv"]
    same_kv = (st["ctk"] == want_ctk) and (st["ctv"] == want_ctv)
    if same_model and same_ctx and same_kv:
        return False, (f"服务已在目标配置上（{st['model_file']} / "
                       f"ctx {st['ctx']} / KV {st['ctk'] or 'f16'}"
                       f"-{st['ctv'] or 'f16'}），跳过重启")
    why = []
    if not same_model:
        why.append(f"模型 {st['model_file']} → {cfg['model_file']}")
    if not same_ctx:
        why.append(f"ctx {st['ctx']} → {answers['ctx']}")
    if not same_kv:
        why.append(f"KV {st['ctk'] or 'f16'}-{st['ctv'] or 'f16'} → "
                   f"{answers['ctk']}-{answers['ctv']}")
    return True, "需要重启：" + "；".join(why)


def verify_service_matches_config(cfg, log):
    """
    校验【服务实际参数】与【配置声称的参数】是否一致 ——
    模型、上下文、KV 精度三样都要对。

    ── 为什么必须校验（网页触发后风险更高）──────────────────────────
      原来只校验模型，ctx 和 KV 精度不校验。但这两个直接决定：
        · ctx  —— KV 池总容量。配置写 16384、服务跑 8192 时，
                  长输入会被截断或报错，而 run-config.json 里写的是 16384，
                  事后完全看不出这次到底用的哪个。
        · KV 精度 —— 影响显存占用和输出质量。题目的容量表要求
                  「说明 KV 精度」，写错就是数据与标签不符。

      网页触发测试时，服务参数由控制层改，改动更隐蔽（不像命令行能看见），
      所以这道校验更重要：宁可拒绝运行，也不要产出一批口径不明的数据。

      注意：模型那项仍有独立的 verify_model_matches_config()，因为它的
      报错信息要引导用户"怎么切回来"，措辞不同（且 token 计数依赖词表，
      模型不符时 token 数也是错的）。这里只补 ctx 与 KV。

    返回 (是否一致, 说明)。
    """
    problems = []

    st = current_service_state(cfg["base_url"])
    if st["error"]:
        return False, f"读不到服务状态（模型服务没运行？）：{st['error']}"

    # ── ctx ──────────────────────────────────────────────────────────
    want_ctx = cfg.get("server_ctx")
    got_ctx = st.get("ctx")
    if want_ctx is not None and got_ctx is not None and int(want_ctx) != int(got_ctx):
        problems.append(
            f"上下文不一致：\n"
            f"        配置里写的是   {want_ctx}\n"
            f"        服务实际在跑   {got_ctx}\n"
            f"      影响：KV 池总容量不同，长输入的行为会不一样，"
            f"而 run-config.json 记的是配置值 —— 数据与标签不符")

    # ── KV 精度 ───────────────────────────────────────────────────────
    # 配置没写就不比（老配置没有这两个字段）
    for key, got_key, flag in (("kv_type_k", "ctk", "-ctk"),
                               ("kv_type_v", "ctv", "-ctv")):
        want = cfg.get(key)
        if not want:
            continue
        # 服务没显式给这个参数时，就是 llama.cpp 的默认值 f16
        got = st.get(got_key) or "f16"
        if str(want).lower() != str(got).lower():
            problems.append(
                f"KV 精度不一致（{flag}）：配置 {want}，服务实际 {got}")

    if problems:
        return False, ("服务端参数与配置不符：\n"
                       + "\n".join("      · " + p for p in problems)
                       + "\n      解决办法：\n"
                         "        ① 让服务按配置的参数重启"
                         "（bash serve.sh model 会用注册表的参数；"
                         "或前端「测试」页启动，它会自己对齐）\n"
                         "        ② 或者把配置里的 server_ctx / kv_type_k / "
                         "kv_type_v 改成服务实际的值")
    if cfg.get("server_ctx") is None:
        # 老配置没有这个字段 —— 不能默默放过，但也不该拦住运行
        log("  ⚠️ 配置里没有 server_ctx / kv_type_k / kv_type_v，"
            "本次无法校验上下文与 KV 精度是否与服务一致。")
        log(f"     服务实际：ctx {got_ctx} / KV "
            f"{st.get('ctk') or 'f16'}-{st.get('ctv') or 'f16'}")
        return True, (f"服务参数未校验（配置缺字段）：ctx {got_ctx} / KV "
                      f"{st.get('ctk') or 'f16'}-{st.get('ctv') or 'f16'}")
    return True, (f"服务参数一致：ctx {got_ctx} / KV "
                  f"{st.get('ctk') or 'f16'}-{st.get('ctv') or 'f16'}")


def main():
    # ⭐ 先自查文件完整性 —— 缺函数就立刻退出（退出码 90），
    #    而不是等跑到那一行才 NameError（那样已经建了目录、改了服务）。
    _self_check()

    ap = argparse.ArgumentParser(description="可重复运行的性能测试程序")
    ap.add_argument("--config", default=None,
                    help="运行配置 JSON 文件（--interactive 时作为默认值模板，可省略）")
    ap.add_argument("--interactive", action="store_true",
                    help="交互式：依次询问模型 / 上下文 / KV 精度 / 标签，"
                         "并在需要时重启模型服务")
    ap.add_argument("--out", default=None,
                    help="结果根目录（默认 <development>/results）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只组装输入并打印计划，不发请求")
    ap.add_argument("--no-restart", action="store_true",
                    help="交互模式下不自动重启服务；只在参数与服务当前状态一致时继续")
    ap.add_argument("--force", action="store_true",
                    help="同一秒内重复运行时，覆盖而不是加序号（一般不用）")
    args = ap.parse_args()

    if not args.config and not args.interactive:
        ap.error("必须给 --config，或者用 --interactive")

    # ══════════════════════════════════════════════════════════════════════
    #  阶段 A：确定配置（不改任何东西、不建目录、不重启服务）
    # ══════════════════════════════════════════════════════════════════════
    # 先读模板配置（交互模式的默认值来源）
    if args.config:
        base = load_config(args.config)
        config_src = args.config
    else:
        # 没给配置：用内置默认值当模板（任务集全 10 个、并发 [1,4,10] 等）
        base = load_config_from_defaults()
        config_src = "（内置默认值）"

    answers = None
    if args.interactive:
        # 交互模式：把注册表里的默认 ctx 也喂给菜单当默认值
        reg, _err = _read_registry_for_menu()
        if reg:
            act = reg.get("default_active") or reg.get("active")
            for m in reg.get("models", []):
                if m.get("id") == act:
                    base.setdefault("ctx_default",
                                    (m.get("server_args") or {}).get("ctx", 16384))
        answers = collect_interactive(base)
        cfg = answers_to_config(answers, base)
        config_src = args.config or "（内置默认值 + 交互答案）"
    else:
        cfg = base

    # ⭐ 校验备注是否符合"只写本轮参数"的规范。
    #    放在【建目录、重启服务之前】—— 校验失败不该改变任何东西。
    note_ok, note_msg = verify_note_scope(cfg)
    if not note_ok:
        print("=" * 72)
        print("❌ 配置里的 note 不符合规范，未开始运行：")
        print()
        for ln in note_msg.splitlines():
            print("   " + ln)
        print()
        print("   规范：note 只描述【本轮自己】的参数，不与其他运行产生关联。")
        print("   原因：note 会被抄进结果、前端和报告；一旦写成跨轮次的说法，")
        print("         以后换了基线或再跑别的轮次，它就会变成错误信息。")
        print("=" * 72)
        sys.exit(3)

    # ══════════════════════════════════════════════════════════════════════
    #  阶段 B：交互模式下，确认摘要 + 按需重启服务
    #  ⚠️ 这两步都在【建结果目录之前】——
    #     这样"用户反悔"或"服务起不来"都不会留下任何残留目录。
    # ══════════════════════════════════════════════════════════════════════
    if args.interactive:
        need, why = plan_service_change(cfg, answers, lambda m: None)
        print()
        print("=" * 72)
        print(" 即将执行")
        print("=" * 72)
        print(f"  模型      : {cfg['model_file']}")
        print(f"  上下文    : {cfg['server_ctx']} token（KV 池总容量，4 slot 共享）")
        print(f"  KV 精度   : K={cfg['kv_type_k']}  V={cfg['kv_type_v']}")
        print(f"  标签      : {cfg['label']}")
        print(f"  任务/轮次 : {len(cfg['task_ids'])} 个 × {cfg['rounds']} 轮")
        print(f"  并发档    : {cfg['concurrency']}")
        print(f"  请求总数  : "
              f"{len(cfg['task_ids']) * cfg['rounds'] * len(cfg['concurrency'])}")
        print(f"  输出上限  : {cfg['max_tokens']} token")
        print(f"  服务      : {why}")
        print(f"  备注      : {cfg['note']}")
        print("=" * 72)
        if need and not args.no_restart:
            print("  ⚠️ 会重启模型服务（约 5 秒），期间无法推理。")
        elif need and args.no_restart:
            print("  ❌ 参数与服务当前状态不一致，但指定了 --no-restart。")
            print("     请先手工把服务切到目标配置，或去掉 --no-restart。")
            sys.exit(4)
        ok = _ask("确认开始？(y/N)", default="n")
        if not ok.lower().startswith("y"):
            print("  已取消，未做任何改动。")
            sys.exit(0)

        if need:
            print()
            # 重启需要 log 函数，但此时还没有 log —— 先建一个最小版本
            _tmp_lines = []

            def _pre_log(m):
                s = f"[{time.strftime('%H:%M:%S')}] {m}"
                print(s, flush=True)
                _tmp_lines.append(s)
            good, msg = restart_with_params(
                cfg["model_file"], answers["ctx"],
                answers["ctk"], answers["ctv"], _pre_log)
            if not good:
                print()
                print("=" * 72)
                print("❌ 模型服务未能按目标配置就绪，已终止（未产生任何结果目录）：")
                print()
                for ln in msg.splitlines():
                    print("   " + ln)
                print()
                print("   排查建议：")
                print("     · 显存不够 → 把上下文调小，或把 KV 精度降到 q8_0 / q4_0")
                print("     · 想看服务日志 → bash serve.sh logs")
                print("     · 想手工恢复 → bash serve.sh model")
                print("=" * 72)
                sys.exit(5)
            print(f"  ✅ {msg}")
        else:
            print(f"  ✅ {why}")

    # ══════════════════════════════════════════════════════════════════════
    #  阶段 C：建目录、开跑
    # ══════════════════════════════════════════════════════════════════════
    # 默认输出目录：<development>/results
    #
    # ⚠️ 目录层级（本文件在 <development>/src/bench/ 下）：
    #       parents[0]=bench [1]=src [2]=development
    #    早先写的是 parent.parent（= src/）+"test_runs"，
    #    迁移到 development/ 布局后会算成 <development>/src/test_runs ——
    #    那个目录不存在、也没被 .gitignore 覆盖，一旦不带 --out 跑
    #    就会在 src/ 下建一个野生结果目录，把仓库弄脏且数据找不着。
    #    注意 write 时用 out_root（结果根），实际每次运行写在 out_root/<run_id>/。
    out_root = Path(args.out) if args.out else \
        Path(__file__).resolve().parents[2] / "results"
    out_root.mkdir(parents=True, exist_ok=True)

    # ⭐ 编号：<标签>-<时间戳>，绝不覆盖已有结果
    label = cfg.get("label") or cfg.get("run_id") or "run"
    run_id, label, out = make_run_id(label, out_root, force=args.force)
    out.mkdir(parents=True, exist_ok=False)

    t_run_start = time.perf_counter()

    lines = []

    def log(msg):
        s = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(s, flush=True)
        lines.append(s)

    log("=" * 72)
    log(f"运行编号：{run_id}")
    log(f"  标签：{label}   （报告里引用这个）")
    log(f"  目录：{out}")
    log(f"配置：{args.config}")
    log("=" * 72)

    # ── 保存配置快照（复现的关键）────────────────────────────────────────
    (out / "run-config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 采集环境（题意①）──────────────────────────────────────────────
    log("采集环境信息…")
    meta = {
        "run_id": run_id,
        "label": label,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hardware": collect_hardware(),
        "model": collect_model_info(cfg["base_url"]),
        "server_config": read_server_config(),
        "load_config": {
            "task_ids": cfg["task_ids"],
            "rounds": cfg["rounds"],
            "concurrency_levels": cfg["concurrency"],
            "seed": cfg["seed"],
            "order": cfg["order"],
            "max_tokens": cfg["max_tokens"],
            "temperature": cfg["temperature"],
            "warmup": cfg["warmup"],
            "base_url": cfg["base_url"],
        },
    }
    log(f"  模型：{meta['model'].get('model_name')} "
        f"量化 {meta['model'].get('quant')} "
        f"ctx {meta['model'].get('n_ctx')} slots {meta['model'].get('total_slots')}")
    log(f"  GPU：{meta['hardware'].get('gpu')}")
    log(f"  显存（加载后）：{meta['server_config'].get('vram_after_load_mib')} MiB")

    # ⭐ 校验"配置声称的模型"与"实际在跑的模型"是否一致
    #    测试程序不会自己切模型，只会照实记录实际在跑的那个。
    #    若配置与事实不符，数据标签就错了（见 verify_model_matches_config 注释），
    #    所以不匹配时直接终止，不产生数据。
    matched, msg = verify_model_matches_config(cfg, meta["model"], log)
    meta["model_verified"] = {
        "expected_from_config": cfg.get("model_file"),
        "actual_running": meta["model"].get("model_name"),
        "matches": matched,
    }

    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if not matched:
        log("")
        log("❌ 模型校验失败，已终止（不产生数据）：")
        for ln in msg.splitlines():
            log("   " + ln)
        log("")
        log(f"   目录 {out.name} 里只留了 meta.json 和 ABORTED.txt，可删除")
        (out / "ABORTED.txt").write_text(
            f"运行中止：模型不匹配\n\n{msg}\n\n" + "\n".join(lines),
            encoding="utf-8")
        sys.exit(2)
    log(f"  ✅ {msg}")

    # ── 再校验【上下文与 KV 精度】（模型校验只管模型）──────────────────
    #    ⚠️ 这两项直接决定 KV 池容量和输出质量，配错会让
    #       run-config.json 记的参数与实际情况不符 —— 数据失去意义。
    svc_ok, svc_msg = verify_service_matches_config(cfg, log)
    if not svc_ok:
        log("")
        log("❌ 服务端参数校验失败，已终止（不产生数据）：")
        for ln in svc_msg.splitlines():
            log("   " + ln)
        log("")
        log(f"   目录 {out.name} 里只留了 meta.json 和 ABORTED.txt，可删除")
        (out / "ABORTED.txt").write_text(
            f"运行中止：服务端参数与配置不符\n\n{svc_msg}\n\n" + "\n".join(lines),
            encoding="utf-8")
        sys.exit(6)
    log(f"  ✅ {svc_msg}")

    # ── 预先组装输入 ─────────────────────────────────────────────────
    log("组装任务输入…")
    cache, prompt_index = prepare_inputs(cfg, log)
    (out / "prompt-index.json").write_text(
        json.dumps(prompt_index, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 打印计划 ─────────────────────────────────────────────────────
    plan_preview = {}
    for c in cfg["concurrency"]:
        waves = build_plan(cfg["task_ids"], cfg["rounds"], c,
                           cfg["seed"], cfg["order"])
        plan_preview[c] = {"n_waves": len(waves),
                           "n_requests": sum(len(w) for w in waves)}
    log(f"负载计划：{json.dumps(plan_preview, ensure_ascii=False)}")

    if args.dry_run:
        log("dry-run：不发请求，退出")
        (out / "run.log").write_text("\n".join(lines), encoding="utf-8")
        return

    # ── 冷启动 / 预热 / 正式（题目要求区分记录）──────────────────────
    cold_start = None
    warmup_records = []
    if cfg["warmup"].get("enabled"):
        wid = cfg["warmup"].get("task_id", "P01")
        n = cfg["warmup"].get("n", 2)
        log(f"冷启动 + 预热（任务 {wid} × {n}）…")
        for i in range(n):
            r = client.run_one(cfg["base_url"], cache[wid]["messages"],
                               max_tokens=cfg["max_tokens"],
                               temperature=cfg["temperature"],
                               timeout=cfg["timeout_s"])
            r["task_id"] = wid
            r["phase"] = "cold_start" if i == 0 else "warmup"
            if i == 0:
                cold_start = r.to_dict()
                log(f"  冷启动 TTFT {r['ttft_ms']:.0f} ms，"
                    f"总耗时 {r['total_ms']:.0f} ms")
            else:
                warmup_records.append(r.to_dict())
                log(f"  预热{i} TTFT {r['ttft_ms']:.0f} ms")

    # ── 正式测试 ─────────────────────────────────────────────────────
    all_records = []
    summaries = {}
    for conc in cfg["concurrency"]:
        waves = build_plan(cfg["task_ids"], cfg["rounds"], conc,
                           cfg["seed"], cfg["order"])
        n_req = sum(len(w) for w in waves)
        log(f"── 并发 {conc}：{len(waves)} 个 wave，共 {n_req} 个请求 ──")

        sampler = GpuSampler(0.5)
        sampler.start()

        results = []
        t_start = time.perf_counter()
        for wi, wave in enumerate(waves):
            run_one_wave(wave, cache, cfg, f"c{conc}-w{wi}", results)
            log(f"  wave {wi + 1}/{len(waves)} 完成（累计 {len(results)}）")
        wall = time.perf_counter() - t_start

        sampler.stop()
        sampler.join(timeout=5)

        recs = [r.to_dict() for r in results]
        for r in recs:
            r["concurrency"] = conc
            r["phase"] = "formal"
        all_records.extend(recs)

        # ⭐ 把墙钟时间传给 summarize —— 吞吐窗口必须用它，不能用逐请求相对时间推
        s = summarize(recs, wall_time_s=wall)
        s["concurrency"] = conc
        s["wall_time_s"] = wall
        s["vram_peak_mib"] = sampler.peak_mib()
        summaries[str(conc)] = s

        log(f"  完成：成功 {s['n_success']}/{s['n_requests']}"
            f"（{s['success_rate'] * 100:.1f}%）"
            f"  TTFT 中位 {s['ttft_ms']['median']:.0f} ms"
            f"  P95 {s['ttft_ms']['p95']:.0f} ms"
            f"  吞吐 {s['throughput']['output_tokens_per_s'] or 0:.1f} tok/s"
            f"  峰值显存 {sampler.peak_mib()} MiB")

        # 显存采样落盘
        with (out / f"gpu-samples-c{conc}.csv").open(
                "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "memory_used_mib", "gpu_util_pct"])
            w.writerows(sampler.samples)

    # ── 落盘：逐请求原始记录（题意⑥）──────────────────────────────────
    with (out / "requests.jsonl").open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 非成功请求单独一份，便于排查（题意④）
    # ⚠️ 包括 incomplete（流被中断）—— 它也是"没答完"，必须能查到
    bad = [r for r in all_records if r["status"] != "success"]
    (out / "errors.json").write_text(
        json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 汇总 ─────────────────────────────────────────────────────────
    duration_s = time.perf_counter() - t_run_start
    # overall 的吞吐窗口 = 各并发档墙钟时间之和（不含冷启动与预热）
    formal_wall = sum(s["wall_time_s"] for s in summaries.values())
    final = {
        "run_id": run_id,
        "label": label,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(duration_s, 1),
        "cold_start": cold_start,
        "warmup": warmup_records,
        "formal": summaries,
        "warmup_note": "冷启动与预热单独记录，不混入正式测试统计（题目要求）",
        "overall_window_note": "overall 的统计窗口是各并发档墙钟时间之和，不含冷启动与预热",
        "overall": summarize(all_records, wall_time_s=formal_wall),
    }
    (out / "summary.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 更新登记表（方便前端列出与自动配对对比）────────────────────────
    rec = make_registry_record(run_id, label, cfg, meta, summaries,
                               final["overall"], out, duration_s)
    if update_runs_registry(out_root, rec):
        log(f"登记表已更新：{out_root / 'runs.json'}")
    else:
        log("⚠️ 登记表写入失败（不影响本次结果）")

    log("=" * 72)
    log(f"✅ 完成。运行编号 {run_id}   总耗时 {duration_s:.1f}s")
    log(f"   结果目录：{out}")
    for fn in sorted(p.name for p in out.iterdir()):
        log(f"     · {fn}")
    log("=" * 72)

    (out / "run.log").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
