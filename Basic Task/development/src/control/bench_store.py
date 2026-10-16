#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_store.py —— 读取测试结果（只读）

设计原则（这一条决定了整个模块的写法）：
    ⭐ 【真实目录是权威，runs.json 只是索引】

    为什么不直接读 runs.json：
      · 手动删掉一个 run 目录 → 登记表还留着 → 前端点进去打不开
      · runs.json 被误删/写坏 → 列表全空，但数据其实都在
      · 从别的机器拷来结果目录 → 登记表里没有 → 看不到

    所以：扫目录拿事实，再拿登记表补充 note/label，
    两边对不上的情况【显式标出来】，而不是静默出错。

    会遇到的四种状态：
      正常        目录齐全，summary.json 可读
      不完整      有目录但缺关键文件（跑到一半被中断）
      目录已丢失  登记表有记录，目录没了
      登记表缺失  有目录但登记表里没有（拷贝过来的）

安全：
    · 本模块【只读】，不删除、不修改任何结果数据
    · run_id 会做白名单校验（必须匹配已有目录名），不接受任意路径
"""

import csv
import json
import re
from pathlib import Path

from .logutil import log

# ══════════════════════════════════════════════════════════════════════════
#  结果根目录
#
#  ⚠️⚠️ 这里曾经是一个【一直没被发现】的 bug：
#     原值 parent.parent / "test_runs"，也就是 <development>/src/test_runs ——
#     那是文件布局迁移【之前】的位置。迁移到 development/results 之后，
#     这个常量没跟着改。
#
#     为什么一直没被发现：目录列表接口 bench_store.list_runs() 读的是
#     runs.json 这个【登记表文件】（它会被 run_bench 写进 RUNS_ROOT），
#     所以列表照常显示；而凡是需要"真的去读某个运行目录"的接口
#     （逐请求记录、错误记录、单条详情、GPU 采样）全部 404：
#         运行目录不存在：<run_id>
#
#     路径层级：本文件在 <development>/src/control/ 下
#         parents[0]=control  [1]=src  [2]=development
#     与 bench_runner.py 里的 _DEV 保持一致。
# ══════════════════════════════════════════════════════════════════════════
_DEV = Path(__file__).resolve().parents[2]
RUNS_ROOT = _DEV / "results"

# run_id 只允许这些字符（它同时是目录名，必须防止路径穿越）
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

# 一个完整运行应有的文件
REQUIRED_FILES = ["run-config.json", "meta.json", "requests.jsonl",
                  "summary.json", "prompt-index.json"]
OPTIONAL_FILES = ["errors.json", "run.log"]

MAX_PAGE = 500          # 逐请求分页上限


class BenchStoreError(Exception):
    """读取测试结果失败"""


# ── 基础工具 ───────────────────────────────────────────────────────────────

def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_jsonl(path, limit=None):
    out = []
    try:
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if limit and len(out) >= limit:
                    break
    except OSError:
        return None
    return out


def validate_run_id(run_id):
    """
    校验 run_id 是否是合法的、且真实存在的目录名。

    ⚠️ 必须做这一步：run_id 会被拼进路径，
       不校验就变成任意文件读取漏洞（../../etc/passwd）。
    """
    if not isinstance(run_id, str) or not run_id:
        raise BenchStoreError("run_id 不能为空")
    if not RUN_ID_RE.match(run_id):
        raise BenchStoreError(f"run_id 含非法字符：{run_id!r}")
    if ".." in run_id:
        raise BenchStoreError("run_id 不允许出现 ..")
    d = RUNS_ROOT / run_id
    try:
        real = d.resolve(strict=True)
    except (OSError, RuntimeError):
        raise BenchStoreError(f"运行目录不存在：{run_id}")
    root_real = RUNS_ROOT.resolve()
    if root_real != real and root_real not in real.parents:
        raise BenchStoreError("路径越出了结果目录")
    if not real.is_dir():
        raise BenchStoreError(f"不是目录：{run_id}")
    return real


def _registry():
    """读登记表；坏了就当空（不能影响数据展示）"""
    d = _read_json(RUNS_ROOT / "runs.json", default=[])
    return d if isinstance(d, list) else []


# ── 列表 ───────────────────────────────────────────────────────────────────

def list_runs():
    """
    列出所有运行。

    ⭐ 以真实目录为准，登记表只做补充。
    返回的每一项都带 status 字段，说明它的完整程度。
    """
    if not RUNS_ROOT.is_dir():
        return {"runs": [], "root": str(RUNS_ROOT), "root_exists": False}

    reg = {r.get("run_id"): r for r in _registry() if r.get("run_id")}
    seen = set()
    out = []

    # ① 扫真实目录（权威）
    for d in sorted(RUNS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        run_id = d.name
        seen.add(run_id)
        item = _describe_run_dir(d, reg.get(run_id))
        out.append(item)

    # ② 登记表里有、目录没了的（显式标出，不静默丢）
    for rid, r in reg.items():
        if rid in seen:
            continue
        out.append({
            "run_id": rid,
            "label": r.get("label"),
            "note": r.get("note"),
            "status": "missing_dir",
            "status_text": "目录已丢失（登记表里有记录）",
            "started_at": r.get("started_at"),
            "usable": False,
        })

    out.sort(key=lambda x: (x.get("started_at") or ""), reverse=True)

    return {
        "runs": out,
        "root": str(RUNS_ROOT),
        "root_exists": True,
        "count": len(out),
        "count_usable": sum(1 for x in out if x.get("usable")),
    }


def _describe_run_dir(d, reg_entry):
    """描述一个运行目录，供列表使用"""
    files = {f.name for f in d.iterdir() if f.is_file()}
    missing = [f for f in REQUIRED_FILES if f not in files]

    item = {
        "run_id": d.name,
        "dir": d.name,
        "files": sorted(files),
        "missing_files": missing,
        "usable": False,
    }

    summary = _read_json(d / "summary.json")
    meta = _read_json(d / "meta.json")
    cfg = _read_json(d / "run-config.json")

    # label：优先取 summary / meta 里记的，其次登记表，最后从目录名截
    if isinstance(summary, dict) and summary.get("label"):
        item["label"] = summary["label"]
    elif isinstance(meta, dict) and meta.get("label"):
        item["label"] = meta["label"]
    elif reg_entry and reg_entry.get("label"):
        item["label"] = reg_entry["label"]
    else:
        item["label"] = re.sub(r"-\d{8}-\d{6}(-\d+)?$", "", d.name)

    item["started_at"] = (meta or {}).get("started_at") or (
        reg_entry or {}).get("started_at")
    item["note"] = (cfg or {}).get("note") or (reg_entry or {}).get("note")

    if isinstance(meta, dict):
        item["model"] = (meta.get("model") or {}).get("model_name")
        item["quant"] = (meta.get("model") or {}).get("quant")
        item["n_ctx"] = (meta.get("model") or {}).get("n_ctx")
        item["total_slots"] = (meta.get("model") or {}).get("total_slots")
        item["server_args"] = (meta.get("server_config") or {}).get("server_args")
        item["vram_after_load_mib"] = (meta.get("server_config") or {}).get(
            "vram_after_load_mib")
        mv = meta.get("model_verified") or {}
        item["model_verified"] = mv.get("matches")

    if isinstance(cfg, dict):
        item["task_ids"] = cfg.get("task_ids")
        item["rounds"] = cfg.get("rounds")
        item["concurrency"] = cfg.get("concurrency")
        item["max_tokens"] = cfg.get("max_tokens")
        item["temperature"] = cfg.get("temperature")

    if isinstance(summary, dict):
        item["duration_s"] = summary.get("duration_s")
        ov = summary.get("overall") or {}
        item["n_requests"] = ov.get("n_requests")
        item["success_rate"] = ov.get("success_rate")
        item["status_breakdown"] = ov.get("status_breakdown")
        # 列表行只显示并发 1 和最大并发的摘要，避免数据太杂
        formal = summary.get("formal") or {}
        brief = {}
        for k in sorted(formal, key=lambda x: int(x)):
            s = formal[k]
            brief[k] = {
                "n_requests": s.get("n_requests"),
                "n_success": s.get("n_success"),
                "success_rate": s.get("success_rate"),
                "ttft_median_ms": (s.get("ttft_ms") or {}).get("median"),
                "ttft_p95_ms": (s.get("ttft_ms") or {}).get("p95"),
                "throughput_tps": (s.get("throughput") or {}).get(
                    "output_tokens_per_s"),
                "vram_peak_mib": s.get("vram_peak_mib"),
            }
        item["by_concurrency_brief"] = brief

    # ── 状态判定 ──
    #
    # ⚠️ 这里有三种情况，早先全归成"数据不完整"一句话，
    #    结果"中途被停的测试"在界面上只显示一个报错，看不到任何请求。
    #    现在按 run-state.json / summary.json 的 state 字段细分：
    #        finished         正常跑完
    #        aborted          中途被停止（有已完成的部分，值得看）
    #        running          正在跑（也有已完成的部分）
    #        (没有 state)     旧数据，按缺文件的情况推断
    state = None
    if isinstance(summary, dict):
        state = summary.get("state")
    rs = _read_json(d / "run-state.json")
    if not state and isinstance(rs, dict):
        state = rs.get("state")
    item["run_state"] = state

    # 有没有逐请求记录 —— 决定"能不能看请求列表"
    # ⚠️ 用 exists() 判断，不要直接 stat()：文件不存在时 stat() 会抛异常
    rq = d / "requests.jsonl"
    has_requests = rq.is_file() and rq.stat().st_size > 0
    item["has_requests"] = has_requests

    # ══════════════════════════════════════════════════════════════════
    #  ★ 服务中断检测（这一条决定了该不该显示"完整"）
    #
    #  ⚠️ 为什么需要它（实测遇到的问题）：
    #     有几次测试【所有请求都跑完了】，summary 里 state=finished，
    #     界面上就显示"完整"。可实际上那次测试期间模型服务掉过、
    #     被拉起来过 —— 中间那几条请求是失败的（service_lost / incomplete），
    #     而且重启前后 KV 池与前缀缓存都被清空，两段数据特性不同，
    #     混在一起的中位数没法解释。
    #     这种运行不该叫"完整"。
    #
    #  ⚠️ 判据用【服务被拉起的动作】，不用"比较首尾 PID"：
    #     早先想过用 service_instance.pid_at_start != pid_at_end，
    #     但实测不可靠 —— web-1016-20261016-183636 的 pid_at_end 是
    #     None（那条请求记 PID 时进程已经没了），比不出来。
    #     而请求记录里的 service_instance_pid 也可能是 None，
    #     PID 本身还可能被系统回收复用。
    #     "有没有被拉起过"是直接记录的事实，最牢靠。
    # ══════════════════════════════════════════════════════════════════
    starts = []
    sf = d / "service-starts.jsonl"
    if sf.is_file():
        try:
            for line in sf.read_text(encoding="utf-8",
                                     errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        starts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            starts = []
    item["n_service_starts"] = len(starts)
    item["service_starts"] = starts

    # 兜底判据：请求记录里出现了多个不同的 PID，也说明服务换过进程。
    # ⚠️ 这是"补充"不是"主判据" —— 它的已知弱点见上面。
    pids = set()
    if has_requests:
        try:
            for line in rq.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    pid = json.loads(line).get("service_instance_pid")
                except json.JSONDecodeError:
                    continue
                if pid is not None:
                    pids.add(pid)
        except OSError:
            pass
    item["service_pids"] = sorted(pids)
    interrupted = bool(starts) or len(pids) > 1
    item["service_interrupted"] = interrupted

    # ⚠️ 这里要特别处理"summary.json 根本不存在"的情况：
    #    中途被停止的运行【不会】有 summary.json（停止得太早），
    #    而 run-state.json 里明确写着 state=aborted。
    #    早先只看 summary，于是这种情况掉进"缺 summary.json"那支，
    #    界面显示成"数据不完整"，跟"目录坏了"分不开。
    if not isinstance(summary, dict) and not state:
        state = "aborted" if isinstance(rs, dict) else None
        item["run_state"] = state

    if state == "aborted":
        # 中途被停：已完成的部分是有效数据，要能看
        item["status"] = "aborted"
        item["n_records"] = (rs or {}).get("n_records_so_far")
        n = item.get("n_requests_actual")
        item["status_text"] = (
            f"⚠️ 测试未完成（中途被停止）"
            + (f"，已记录 {n} 条请求" if n else "")
            + "；可查看已跑完的部分，但不要当作完整结果")
        # ⚠️ usable 仍为 False：它不是一次可用的正式测量，
        #    但【不影响查看】—— 前端据此决定"能不能对比/引用"，
        #    而不是"能不能打开"。
    elif state == "running":
        item["status"] = "running"
        item["status_text"] = "⏳ 正在运行（下面是已跑完的部分）"
    elif missing and not has_requests:
        item["status"] = "incomplete"
        item["status_text"] = ("没有逐请求记录：缺 " + "、".join(missing)
                               + "（运行刚开始就被中断，或不是本程序产生的目录）")
    elif missing:
        # 有请求记录、只是缺汇总 —— 仍然可以看请求
        item["status"] = "incomplete"
        item["status_text"] = ("逐请求记录在，但缺 " + "、".join(missing)
                               + "（运行未正常收尾）")
    elif not isinstance(summary, dict):
        item["status"] = "incomplete"
        item["status_text"] = "summary.json 无法解析"
    elif interrupted:
        # ★ 请求都跑完了，但期间服务掉过 → 不是"完整"
        #
        # ⚠️ 两种证据来源，文案要分开说 —— 否则会出现
        #    「中断过 0 次」这种自相矛盾的显示（实测踩过）：
        #      · service-starts.jsonl 有记录 → 知道确切次数
        #      · 只靠请求记录里的 PID 不一致 → 只知道"换过进程"，
        #        不知道换了几次（那是本功能上线前的旧运行）
        item["status"] = "interrupted"
        if starts:
            head = (f"⚠️ 跑完了，但测试期间【模型服务中断过 "
                    f"{len(starts)} 次】（已自动拉起）")
        else:
            head = ("⚠️ 跑完了，但测试期间【模型服务换过进程】"
                    "（请求记录里出现了多个服务进程号）")
        item["status_text"] = (
            head + " —— 重启前后的 KV 池与前缀缓存被清空，"
                   "两段数据特性不同，不能当作一次完整测量，"
                   "也不要与其他配置直接对比")
        # ⚠️ usable 保持 False：它的整体中位数不可靠（理由同上）
    else:
        item["status"] = "ok"
        item["status_text"] = "完整"
        item["usable"] = True
        if item.get("model_verified") is False:
            item["status_text"] = "完整（但模型校验未通过）"

    return item


# ── 详情 ───────────────────────────────────────────────────────────────────

def get_run(run_id):
    """读一次运行的完整信息（不含逐请求明细）"""
    d = validate_run_id(run_id)
    summary = _read_json(d / "summary.json")
    meta = _read_json(d / "meta.json")
    cfg = _read_json(d / "run-config.json")
    idx = _read_json(d / "prompt-index.json")
    errs = _read_json(d / "errors.json", default=[])

    if summary is None and meta is None:
        raise BenchStoreError(f"该运行没有可读的结果：{run_id}")

    files = sorted(f.name for f in d.iterdir() if f.is_file())
    # 逐请求条数（不整份读进来，只数行）
    n_req = None
    try:
        with (d / "requests.jsonl").open(encoding="utf-8") as f:
            n_req = sum(1 for line in f if line.strip())
    except OSError:
        pass

    return {
        "run_id": run_id,
        "files": files,
        "summary": summary,
        "meta": meta,
        "run_config": cfg,
        "prompt_index": idx,
        "n_requests_recorded": n_req,
        "n_errors": len(errs) if isinstance(errs, list) else None,
        "gpu_samples": sorted(f for f in files if f.startswith("gpu-samples")),
    }


def _partial_note(d):
    """
    判断这次运行是不是"中途停止/未完成"，返回给前端的提示文字（或 None）。

    ⚠️ 为什么需要它：请求列表原来在缺 requests.jsonl 时直接抛错，
       界面上只显示一句"加载失败"。而"中途被停的测试"其实
       【有已跑完的请求】值得看 —— 应该显示出来并标明不完整，
       而不是当成一个错误。
    """
    summary = _read_json(d / "summary.json")
    rs = _read_json(d / "run-state.json")
    state = None
    if isinstance(summary, dict):
        state = summary.get("state")
    if not state and isinstance(rs, dict):
        state = rs.get("state")
    if state == "aborted":
        return "⚠️ 这次测试被中途停止，下面只是停止前已完成的部分，不是完整结果。"
    if state == "running":
        return "⏳ 这次测试还在运行，下面是目前已跑完的部分。"
    if state == "finished":
        return None
    # 没有状态文件（旧数据）：按缺不缺汇总文件推断
    if isinstance(summary, dict):
        return None
    if isinstance(rs, dict):
        # 有状态文件但状态不是上面那几种 —— 至少说明是"本程序产生的、
        # 还没收尾的运行"，而不是目录坏了
        return (f"⚠️ 这次运行没有正常收尾"
                f"（状态 {rs.get('state') or '未知'}），"
                f"下面是已记录的请求。")
    return "⚠️ 这次运行没有正常收尾（缺 summary.json），下面是已记录的请求。"


def get_requests(run_id, offset=0, limit=100, only=None, task_id=None):
    """
    分页读逐请求记录。

    only: "bad" 只返回非成功记录；None 返回全部
    task_id: 只看某个任务

    ⚠️ 与早先的行为差异：requests.jsonl 不存在时【不再抛错】，
       而是返回空列表 + 说明。这样界面能区分
       "真的一条都没有" 和 "接口坏了"。
    """
    d = validate_run_id(run_id)
    rows = _read_jsonl(d / "requests.jsonl")
    note = _partial_note(d)
    if rows is None:
        return {"run_id": run_id, "total": 0, "offset": 0, "limit": limit,
                "rows": [],
                "note": note or ("这次运行没有逐请求记录"
                                 "（requests.jsonl）—— 可能在发第一个请求前"
                                 "就被停止了。")}

    if only == "bad":
        rows = [r for r in rows if r.get("status") != "success"]
    if task_id:
        rows = [r for r in rows if r.get("task_id") == task_id]

    total = len(rows)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_PAGE))
    page = rows[offset:offset + limit]

    # 列表不需要完整输出文本（几百 KB），只在取单条详情时给
    light = []
    for r in page:
        c = dict(r)
        txt = c.pop("output_text", None) or ""
        c["output_preview"] = txt[:160]
        c["output_chars"] = c.get("output_chars") or len(txt)
        c["has_full_output"] = bool(txt)
        light.append(c)

    return {"run_id": run_id, "total": total, "offset": offset,
            "limit": limit, "rows": light, "note": note}


def get_request_detail(run_id, request_id):
    """取单条请求的完整记录（含模型输出原文）"""
    d = validate_run_id(run_id)
    rows = _read_jsonl(d / "requests.jsonl")
    if rows is None:
        raise BenchStoreError(
            f"这次运行没有逐请求记录（requests.jsonl）—— "
            f"可能在发第一个请求前就被停止了：{run_id}")
    for r in rows:
        if r.get("request_id") == request_id:
            return r
    raise BenchStoreError(f"没有这条请求记录：{request_id}")


def get_errors(run_id):
    """错误记录（术语④）

    ⚠️ 中途被停止的运行也会走到这里。errors.json 可能不存在
       （没跑完就没写），那就从 requests.jsonl 现场筛 ——
       所以"被停的测试"现在也能看到它停止前产生的错误/截断记录。
    """
    d = validate_run_id(run_id)
    note = _partial_note(d)
    errs = _read_json(d / "errors.json", default=None)
    if errs is None:
        # errors.json 缺失时，从 requests.jsonl 现场筛一遍
        rows = _read_jsonl(d / "requests.jsonl") or []
        errs = [r for r in rows if r.get("status") != "success"]
    # 去掉大文本，列表用不上
    light = []
    for r in errs:
        c = {k: v for k, v in r.items() if k != "output_text"}
        light.append(c)
    return {"run_id": run_id, "n": len(light), "errors": light, "note": note}


def get_gpu_samples(run_id):
    """显存采样序列（用于画曲线）"""
    d = validate_run_id(run_id)
    out = {}
    for f in sorted(d.iterdir()):
        if not f.name.startswith("gpu-samples") or f.suffix != ".csv":
            continue
        key = f.stem.replace("gpu-samples-", "")
        rows = []
        try:
            with f.open(encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append({
                            "t_s": float(r["t_s"]),
                            "mem": int(r["memory_used_mib"]),
                            "util": int(r["gpu_util_pct"]),
                        })
                    except (KeyError, ValueError):
                        continue
        except OSError:
            continue
        # 抽稀：最多 200 个点，避免前端画太多
        if len(rows) > 200:
            step = len(rows) // 200 + 1
            rows = rows[::step]
        out[key] = rows
    return {"run_id": run_id, "series": out}


# ── 对比 ───────────────────────────────────────────────────────────────────

def compare(run_a, run_b):
    """
    两个运行的对比：汇总指标 + 配置差异。

    对应题目要求：
      · 「基线和优化后结果、配置差异及汇总统计」
      · 「保持模型、输入、输出限制、硬件和统计方法一致，比较配置变化的效果」
    """
    da = validate_run_id(run_a)
    db = validate_run_id(run_b)
    sa = _read_json(da / "summary.json")
    sb = _read_json(db / "summary.json")
    ca = _read_json(da / "run-config.json") or {}
    cb = _read_json(db / "run-config.json") or {}
    ma = _read_json(da / "meta.json") or {}
    mb = _read_json(db / "meta.json") or {}

    if not isinstance(sa, dict) or not isinstance(sb, dict):
        raise BenchStoreError("两个运行都必须有可读的 summary.json")

    # ── 指标对比（按并发档对齐）──
    metrics = {}
    all_c = sorted(set(sa.get("formal", {})) | set(sb.get("formal", {})),
                   key=lambda x: int(x))
    for c in all_c:
        fa = (sa.get("formal") or {}).get(c)
        fb = (sb.get("formal") or {}).get(c)

        def pick(s, *path):
            cur = s
            for p in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(p)
            return cur

        row = {"concurrency": int(c)}
        for name, path in [
            ("n_requests", ("n_requests",)),
            ("n_success", ("n_success",)),
            ("success_rate", ("success_rate",)),
            ("ttft_median_ms", ("ttft_ms", "median")),
            ("ttft_p95_ms", ("ttft_ms", "p95")),
            ("tpot_median_ms", ("tpot_excl_first_ms", "median")),
            ("total_median_ms", ("total_ms", "median")),
            ("total_p95_ms", ("total_ms", "p95")),
            ("throughput_tps", ("throughput", "output_tokens_per_s")),
            ("vram_peak_mib", ("vram_peak_mib",)),
            ("n_incomplete", ("n_incomplete",)),
            ("n_failed", ("n_failed",)),
        ]:
            va = pick(fa, *path) if fa else None
            vb = pick(fb, *path) if fb else None
            row[name] = {
                "a": va, "b": vb,
                # 变化百分比（只在两边都有且基数非 0 时给）
                "delta_pct": (round((vb - va) / va * 100, 1)
                              if (isinstance(va, (int, float))
                                  and isinstance(vb, (int, float)) and va)
                              else None),
                # 对"越小越好"的指标，标出改善方向
                "better": (("b" if vb < va else "a" if vb > va else "same")
                           if (isinstance(va, (int, float))
                               and isinstance(vb, (int, float))) else None),
            }
        metrics[c] = row

    # ── 配置差异 ──
    diff = _diff_config(ca, cb)

    # ── 控制变量检查（题目要求"保持…一致"）──
    #
    # ⚠️ 必须从 meta.json 的 load_config 读，不能读 run-config.json：
    #    run-config.json 是【原始配置文件】，像 task_ids / rounds / concurrency
    #    这些字段可能没写（靠 load_config 里的 setdefault 补默认值），
    #    于是两边都是 None，比出来"全都变了" —— 假阳性。
    #    meta.json 里的 load_config 是【实际生效值】，才可比。
    la = ma.get("load_config") or {}
    lb = mb.get("load_config") or {}
    cfg_keys = ("task_ids", "rounds", "concurrency_levels", "seed", "order",
                "max_tokens", "temperature", "base_url")
    controlled, changed = [], []
    for k in cfg_keys:
        va, vb = la.get(k, ca.get(k)), lb.get(k, cb.get(k))
        (changed if va != vb else controlled).append(
            {"key": k, "a": va, "b": vb})
    # 模型单独比（题目的核心控制变量）
    mna = (ma.get("model") or {}).get("model_name")
    mnb = (mb.get("model") or {}).get("model_name")
    (changed if mna != mnb else controlled).append(
        {"key": "model", "a": mna, "b": mnb})

    # 服务端启动参数差异（真正的"优化改动"通常在这里）
    sa_args = ((ma.get("server_config") or {}).get("server_args")) or {}
    sb_args = ((mb.get("server_config") or {}).get("server_args")) or {}
    server_changed = []
    for k in sorted(set(sa_args) | set(sb_args)):
        if sa_args.get(k) != sb_args.get(k):
            server_changed.append({"key": k, "a": sa_args.get(k),
                                   "b": sb_args.get(k)})

    return {
        "a": {"run_id": run_a, "label": sa.get("label"),
              "note": ca.get("note"), "started_at": (ma or {}).get("started_at")},
        "b": {"run_id": run_b, "label": sb.get("label"),
              "note": cb.get("note"), "started_at": (mb or {}).get("started_at")},
        "metrics": metrics,
        "config_diff": diff,
        "controlled_same": controlled,
        "controlled_changed": changed,
        "server_args_changed": server_changed,
        "model": {
            "a": ((ma.get("model") or {}).get("model_name")),
            "b": ((mb.get("model") or {}).get("model_name")),
        },
        # ⚠️ 注意参数顺序：第一个是【被改动的】控制变量。
        #    我第一版把 controlled（一致的）传了进去，
        #    结果"自己跟自己比"却提示"有 9 个控制变量被改动"，正好说反。
        "conclusion_hint": _hint(changed, server_changed),
    }


def _diff_config(a, b, prefix=""):
    """递归比较两份配置，返回差异列表"""
    out = []
    keys = sorted(set(a) | set(b))
    for k in keys:
        if k.startswith("_"):
            continue        # 跳过 _说明 之类的注释字段
        va, vb = a.get(k), b.get(k)
        path = f"{prefix}{k}"
        if isinstance(va, dict) and isinstance(vb, dict):
            out.extend(_diff_config(va, vb, prefix=f"{path}."))
        elif va != vb:
            out.append({"path": path, "a": va, "b": vb})
    return out


def _hint(controlled_changed, server_changed):
    """
    自动生成一句结论提示，帮使用者判断这次对比可不可信。

    判断顺序很重要（我第一版写反了，导致"自己跟自己比"却提示
    "有控制变量被改动"，正好说反）：
      ① 先看控制变量有没有被改 —— 这是可比性的前提
      ② 再看服务端参数改了没有 —— 一个都没改就不算对比实验
      ③ 改了一个才是干净的对比；改多个说不清是谁的功劳
    """
    if controlled_changed:
        return ("⚠️ 有控制变量被改动（%s），"
                "严格意义上这次对比不满足「只改一个变量」"
                % "、".join(c["key"] for c in controlled_changed))
    if not server_changed:
        return ("⚠️ 两次运行的服务端参数完全相同 —— "
                "控制变量虽然一致，但这不是一次对比实验（没改任何东西）")
    if len(server_changed) == 1:
        return ("✅ 只改了服务端参数 %s，其余控制变量一致，对比有效"
                % server_changed[0]["key"])
    return ("⚠️ 同时改了 %d 个服务端参数（%s），无法区分各参数的贡献"
            % (len(server_changed), "、".join(c["key"] for c in server_changed)))
