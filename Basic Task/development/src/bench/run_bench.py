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
        import urllib.request
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


def load_config(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d.setdefault("base_url", "http://127.0.0.1:8080")
    d.setdefault("input_dir", str(
        Path.home() / "antlab/ANTLab-Infra-Tasks/Basic Task/"
                      "提示词与输入文档/输入文档"))
    d.setdefault("model_file", "Qwen3-0.6B-Q8_0.gguf")
    d.setdefault("task_ids", tasks.all_ids())
    d.setdefault("concurrency", [1, 4])
    d.setdefault("rounds", 3)
    d.setdefault("max_tokens", 2048)
    d.setdefault("temperature", 0.0)
    d.setdefault("seed", 20261009)
    d.setdefault("order", "shuffled")
    d.setdefault("timeout_s", 600)
    d.setdefault("warmup", {"enabled": True, "n": 2, "task_id": "P01"})
    # 兼容旧字段名：以前叫 run_id，现在叫 label（语义是"逻辑标签"）
    if "label" not in d and "run_id" in d:
        d["label"] = d.pop("run_id")
    d.setdefault("label", "run")
    return d


def prepare_inputs(cfg, log):
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


def main():
    ap = argparse.ArgumentParser(description="可重复运行的性能测试程序")
    ap.add_argument("--config", required=True, help="运行配置 JSON 文件")
    ap.add_argument("--out", default=None, help="结果根目录（默认 _work/test_runs）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只组装输入并打印计划，不发请求")
    ap.add_argument("--force", action="store_true",
                    help="同一秒内重复运行时，覆盖而不是加序号（一般不用）")
    args = ap.parse_args()

    cfg = load_config(args.config)

    # ⭐ 先校验备注是否符合"只写本轮参数"的规范。
    #    放在建目录之前 —— 否则校验失败会留下一个空目录。
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

    out_root = Path(args.out) if args.out else \
        Path(__file__).resolve().parent.parent / "test_runs"
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
