#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_all_paths.py —— 迁移后全仓库路径审计（读取 + 写入）

为什么要分两类查：
    读路径错了会【报错】，一跑就发现（比如"找不到注册表"）。
    写路径错了更危险 —— 它可能：
      · 静默写到别处（比如多了一层 src/，结果落在没人看的地方）
      · 创建出一堆野生目录，把仓库弄脏
      · 或者写到 /tmp 之类的地方，跑完就"数据不见了"
    所以写路径要【显式列出来逐个判断】，不能等出问题再找。

用法：python3 tools/audit_all_paths.py
"""

import importlib
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
DEV = SRC.parent
REPO = DEV.parent.parent
sys.path.insert(0, str(SRC))

print("=" * 92)
print(" 全仓库路径审计（迁移后）")
print("=" * 92)
print(f" 仓库根     : {REPO}")
print(f" development: {DEV}")
print(f" src        : {SRC}")
print()

bad = []
warn = []

# ══════════════════════════════════════════════════════════════════════════
#  第一部分：读取类路径（必须存在）
# ══════════════════════════════════════════════════════════════════════════
print("─" * 92)
print(" 一、读取类路径（必须已存在，不存在就是算错了）")
print("─" * 92)
print()

READ_CHECKS = [
    ("control.config",        "WEB_DIR",           "dir",  "前端静态目录"),
    ("control.models_config", "CONFIG_DIR",        "dir",  "配置目录"),
    ("control.models_config", "MODELS_FILE",       "file", "模型注册表"),
    ("control.bench_store",   "RUNS_ROOT",         "dir",  "测试结果根目录"),
    ("control.files",         "TASK_INPUT_DIR",    "dir",  "题包输入文档目录"),
]

for mod_name, attr, kind, desc in READ_CHECKS:
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        print(f"  ❌ {mod_name} 导入失败：{type(e).__name__}: {e}")
        bad.append((f"{mod_name}.{attr}", "模块导入失败"))
        continue
    if not hasattr(mod, attr):
        print(f"  ⚠️  {mod_name}.{attr} 不存在")
        warn.append((f"{mod_name}.{attr}", "属性不存在"))
        continue
    p = Path(getattr(mod, attr))
    ok = p.is_dir() if kind == "dir" else p.is_file()
    print(f"  {'✅' if ok else '❌'} {desc}")
    print(f"       {mod_name}.{attr}")
    print(f"       {p}")
    if not ok:
        bad.append((f"{mod_name}.{attr}", str(p)))
    print()

# ══════════════════════════════════════════════════════════════════════════
#  第二部分：写入类路径（关键 —— 逐个判断会写到哪）
# ══════════════════════════════════════════════════════════════════════════
print("─" * 92)
print(" 二、写入类路径（判断会写到哪、在不在 gitignore 覆盖范围内）")
print("─" * 92)
print()

import subprocess


def git_ignored(path):
    """
    这个路径是否被 gitignore 覆盖。

    ⚠️ 必须在【路径所在目录】里跑 git check-ignore，不能固定用仓库根。
       踩过的坑：从仓库根跑 `git check-ignore "Basic Task/development/src/uploads/x.md"`，
       git 会把整个路径当成相对根的字符串去做匹配，
       而 development/.gitignore 里的 `uploads/` 规则是按"相对它自己所在目录"匹配的，
       于是匹配不上 —— 报出"未被忽略"的假阳性。
       判据要以【git status 是否看得到】为准。
    """
    p = Path(path)
    # 从离它最近的存在目录开始跑
    start = p if p.is_dir() else p.parent
    while not start.exists() and start != start.parent:
        start = start.parent
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", str(p)],
                       cwd=str(start), capture_output=True)
    if r.returncode == 0:
        return True
    # 兜底判据：git status 能否看到它
    r2 = subprocess.run(["git", "status", "--porcelain", "--", str(p)],
                        cwd=str(REPO), capture_output=True, text=True)
    return not (r2.stdout or "").strip()


WRITE_CHECKS = [
    ("control.model_manager", "LOG_DIR",    "日志目录（模型服务日志/PID 写这里）"),
    ("control.model_manager", "SERVER_LOG", "模型服务日志文件"),
    ("control.model_manager", "PID_FILE",   "模型服务 PID 文件"),
    ("control.files",         "UPLOAD_DIR", "用户上传目录"),
    ("control.models_config", "RUNTIME_DIR",  "运行时状态目录"),
    ("control.models_config", "RUNTIME_STATE", "当前模型状态文件"),
]

for mod_name, attr, desc in WRITE_CHECKS:
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        print(f"  ❌ {mod_name} 导入失败：{e}")
        continue
    p = Path(getattr(mod, attr))
    ig = git_ignored(p)
    # 是否在仓库内
    try:
        inside = REPO in p.resolve().parents or p.resolve() == REPO
    except Exception:
        inside = False
    print(f"  {desc}")
    print(f"       {p}")
    print(f"       在仓库内 : {'是' if inside else '否'}")
    print(f"       git 忽略 : {'✅ 是（不会弄脏仓库）' if ig else '⚠️ 否（会被提交进去！）'}")
    if inside and not ig:
        bad.append((f"{mod_name}.{attr}", "在仓库内但没被 gitignore 覆盖"))
    print()

# ══════════════════════════════════════════════════════════════════════════
#  第三部分：脚本级别的落盘路径（serve.sh 等）
# ══════════════════════════════════════════════════════════════════════════
print("─" * 92)
print(" 三、脚本里的落盘路径（从 serve.sh 实际解析）")
print("─" * 92)
print()

sh = SRC / "serve.sh"
# ⚠️ 两个坑，都踩过：
#   ① 不能用 `source <(sed ...)` 进程替换 —— BASH_SOURCE 会变成 /dev/fd/63，
#      DIR 解析成 /dev/fd，所有路径跟着错。
#   ② 也不能简单 cp 到 /tmp 再 source —— serve.sh 里
#      DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"，
#      复制品的 BASH_SOURCE 在 /tmp，于是 DIR=/tmp，还是错的。
#   ③ 正确做法：按【原文件所在目录】去 source，也就是 cwd 设为 SRC，
#      并把 BASH_SOURCE 摆对 —— 最稳的是直接在 SRC 里执行一个
#      只含"打印变量"的小脚本，让它 source 同目录的真实 serve.sh。
probe = SRC / "_probe_paths.sh"
probe.write_text(
    'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
    'source "$DIR/serve.sh" >/dev/null 2>&1\n'
    'echo "DIR=$DIR"; echo "LOGDIR=$LOGDIR";\n'
    'echo "MODEL_LOG=$MODEL_LOG"; echo "CONTROL_LOG=$CONTROL_LOG";\n'
    'echo "MODEL_PIDFILE=$MODEL_PIDFILE"; echo "CONTROL_PIDFILE=$CONTROL_PIDFILE"\n',
    encoding="utf-8")
r = subprocess.run(["bash", str(probe)], capture_output=True, text=True, cwd=str(SRC))
probe.unlink()
# DIR 是脚本所在目录本身，不是写入目标 —— 它当然不该被 gitignore，
# 所以不参与"是否被忽略"的判断（早先把它也算进去，是个假阳性）。
SKIP_IGNORE_CHECK = {"DIR"}
for line in (r.stdout or "").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        if not v or v == "/dev/fd":
            continue
        p = Path(v)
        if k in SKIP_IGNORE_CHECK:
            print(f"  {k:16s} {v}")
            print(f"  {'':16s} （脚本所在目录，不是写入目标，跳过忽略检查）")
            continue
        ig = git_ignored(p)
        print(f"  {k:16s} {v}")
        print(f"  {'':16s} git 忽略: {'✅' if ig else '⚠️ 否'}")
        if not ig:
            bad.append((f"serve.sh::{k}", f"{v} 未被 gitignore 覆盖"))
print()

# ══════════════════════════════════════════════════════════════════════════
#  第四部分：测试程序的输出目录默认值（改脚本时要特别看这里）
# ══════════════════════════════════════════════════════════════════════════
print("─" * 92)
print(" 四、bench/run_bench.py 的输出目录")
print("─" * 92)
print()

sys.path.insert(0, str(SRC / "bench"))
try:
    # ⚠️ 不要从源码文本里正则抓默认值 —— 那会把【注释里的旧路径】
    #    也抓出来，报出已经修好的假阳性（我第一版就这么干的，
    #    结果明明改对了还报"默认值是 src/test_runs"）。
    #    正确做法：直接 import 模块，按它的真实表达式算一遍。
    import run_bench
    real_default = (Path(run_bench.__file__).resolve().parents[2] / "results")
    print("  run_bench.py 里 --out 的默认表达式（按 __file__ 真实求值）：")
    print(f"       parents[2] / \"results\"")
    print(f"     = {real_default}")
    print(f"       存在吗  : {'✅ 是' if real_default.is_dir() else '❌ 否'}")
    print(f"       git 忽略: {'✅ 是' if git_ignored(real_default) else '⚠️ 否'}")
    print()
    # 反向检查：help 文案里是否还残留旧路径
    import inspect
    src = inspect.getsource(run_bench.main)
    stale = [ln.strip() for ln in src.splitlines()
             if "help=" in ln and ("_work" in ln or "test_runs" in ln)]
    if stale:
        print("  ⚠️ help 文案里还有旧路径：")
        for s in stale:
            print(f"       {s}")
            warn.append(("run_bench --out help", s))
    else:
        print("  ✅ help 文案里没有旧路径")
    print()
    # 真正的判据：默认目录必须在结果根的正确位置
    want = (DEV / "results").resolve()
    if real_default.resolve() == want:
        print(f"  ✅ 默认输出目录正确（= {want}）")
    else:
        print(f"  ❌ 默认输出目录错了")
        print(f"       算出：{real_default}")
        print(f"       应为：{want}")
        bad.append(("bench/run_bench.py --out 默认值", str(real_default)))
except Exception as e:
    print(f"  ❌ 无法分析：{type(e).__name__}: {e}")
    bad.append(("bench/run_bench.py", f"分析失败 {e}"))
print()

# ══════════════════════════════════════════════════════════════════════════
#  第五部分：仓库里有没有迁移遗留的野生目录
# ══════════════════════════════════════════════════════════════════════════
print("─" * 92)
print(" 五、仓库里有没有迁移遗留的野生目录")
print("─" * 92)
print()

SUSPECT = ["src/test_runs", "src/configs", "src/models", "src/results",
           "src/uploads", "test_runs", "configs/models.json.bak"]
for rel in SUSPECT:
    p = DEV / rel
    if p.exists():
        n = len(list(p.rglob("*"))) if p.is_dir() else 1
        print(f"  ⚠️ 存在: {rel}  ({n} 项)")
        warn.append((rel, "迁移遗留的目录，可能造成混淆"))
    else:
        print(f"  ✅ 不存在: {rel}")
print()

# ══════════════════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════════════════
print("=" * 92)
if bad:
    print(f" ❌ 有 {len(bad)} 处问题需要修：")
    for a, why in bad:
        print(f"     {a}")
        print(f"         {why}")
else:
    print(" ✅ 所有路径解析正确，且写入路径都在 gitignore 覆盖范围内")
if warn:
    print()
    print(f" ⚠️  {len(warn)} 处需要人工确认：")
    for a, why in warn:
        print(f"     {a}：{why}")
print("=" * 92)
sys.exit(1 if bad else 0)
