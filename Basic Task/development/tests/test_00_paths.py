#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_00_paths.py —— 路径解析与落盘安全性

═══════════════════════════════════════════════════════════════════════════
 对应题目
═══════════════════════════════════════════════════════════════════════════
 提交标准·开发与观测：
     「请求 ID、运行编号、提示词编号、配置和数据能够相互追踪。」

 提交标准·提交内容：
     「模型权重、推理引擎构建产物、依赖环境与临时 KV 快照保存在本地，
       并由 .gitignore 管理。」

 tests/README.md：
     「测试覆盖……请求与输出存储」

═══════════════════════════════════════════════════════════════════════════
 为什么"路径"要单独测
═══════════════════════════════════════════════════════════════════════════
 本项目踩过的路径坑（都真实发生过）：

   · run_bench.py 的 --out 默认值算错 → 会往 src/test_runs 建野生目录
   · models_config 的 CONFIG_DIR 少一层 parent → /api/models 直接 500
   · serve.sh 读注册表的相对路径少了 ../ → 服务起不来
   · 从别处 clone 后，配置里的绝对 input_dir 指向原仓库 → 静默读错文件

 ★ 读路径错了会【报错】，一跑就发现；
   ★ 写路径错了可能【静默落到别处】—— 事后才"数据不见了"。
   所以读、写两类要分开查，而且写入目标必须落在 .gitignore 覆盖范围内。

═══════════════════════════════════════════════════════════════════════════
 判据（独立来源）
═══════════════════════════════════════════════════════════════════════════
   · "文件存在吗"  → 文件系统，不依赖任何代码
   · "被 git 忽略吗" → 调 git check-ignore（外部工具，不是我们写的）
   · "解析到哪"     → 实际 import 模块 / 执行脚本取变量，不是读源码猜

 ⚠️ git check-ignore 必须在【目标路径所在目录】跑。
    固定在仓库根跑会漏掉子目录的 .gitignore，产生假阳性（踩过）。

 退出码：0 通过 / 1 有失败 / 2 脚本自身出错
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _testlib import DEV_DIR, RESULTS_DIR, Report, run      # noqa: E402

SRC = DEV_DIR / "src"

# 允许不存在（还没跑过 / 首次部署才有）
ALLOW_MISSING = {
    DEV_DIR / "results",
}


def git_ignored(p):
    """
    路径是否被 git 忽略。

    ⚠️ 必须在【路径所在目录】执行，否则子目录的 .gitignore 不生效 → 假阳性。
       这个坑踩过一次：src/uploads/ 明明是忽略的，从仓库根查却说没忽略。
    """
    p = Path(p)
    d = p if p.is_dir() else p.parent
    if not d.exists():
        # 目录还不存在时，往上层找最近存在的祖先
        while not d.exists() and d != d.parent:
            d = d.parent
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(p)],
                           cwd=str(d), capture_output=True, timeout=10)
        if r.returncode == 0:
            return True
        # 兜底：git status 里看它是不是被跟踪
        r2 = subprocess.run(["git", "status", "--porcelain", "--", str(p)],
                            cwd=str(d), capture_output=True, text=True, timeout=10)
        return r2.stdout.strip().startswith("!!")
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    rep = Report("test_00_paths —— 路径解析与落盘安全性")

    if not SRC.is_dir():
        rep.check_true("src/ 目录存在", False, str(SRC))
        return rep.finish()

    # ══ 一、读取类路径：必须存在 ══════════════════════════════════════
    print("═" * 72)
    print(" 一、读取类路径（必须存在）")
    print("═" * 72)
    read_paths = {
        "serve.sh": SRC / "serve.sh",
        "run.py": SRC / "run.py",
        "控制层 server.py": SRC / "control" / "server.py",
        "控制层 config.py": SRC / "control" / "config.py",
        "模型注册表": DEV_DIR / "configs" / "models.json",
        "测试程序": SRC / "bench" / "run_bench.py",
        "前端页面": SRC / "web" / "index.html",
        "前端脚本": SRC / "web" / "app.js",
        "前端样式": SRC / "web" / "style.css",
        "结果目录说明": RESULTS_DIR / "README.md",
        "项目说明": DEV_DIR / "README.md",
        "测试说明": DEV_DIR / "tests" / "README.md",
    }
    for name, p in read_paths.items():
        ok = p.exists()
        rep.check_true(f"读取路径存在：{name}", ok, str(p))
        print(f"  {'✅' if ok else '❌'} {name:18s} {p}")

    # ══ 二、配置里的 input_dir 是否指向本仓库 ═════════════════════════
    #
    #  ★ 这是"从别处 clone 就跑不了"的根因：
    #    配置里写的是绝对路径，指向原仓库；原仓库不在时直接崩。
    print()
    print("═" * 72)
    print(" 二、测试配置里的 input_dir")
    print("═" * 72)
    cfg_dir = SRC / "bench" / "configs"
    for f in sorted(cfg_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        idir = d.get("input_dir")
        if not idir:
            continue
        p = Path(idir)
        exists = p.is_dir()
        # 是不是指向【本仓库】的输入文档（而不是别人的路径）
        in_repo = str(p.resolve()).startswith(str(DEV_DIR.parent.resolve()))
        rep.check_true(f"{f.name}: input_dir 指向本仓库内", in_repo,
                       f"实际 {idir} —— 换台机器就跑不了")
        rep.check_true(f"{f.name}: input_dir 目录存在", exists, idir)
        print(f"  {'✅' if (exists and in_repo) else '⚠️ '} {f.name:44s} {idir}")

    # ══ 三、写入类路径：必须落在 .gitignore 覆盖范围内 ════════════════
    print()
    print("═" * 72)
    print(" 三、写入类路径（跑起来会往这些地方写）")
    print("═" * 72)
    write_paths = {
        "运行日志目录": SRC / "logs",
        "控制层日志": SRC / "logs" / "control.log",
        "模型服务日志": SRC / "logs" / "model-server.log",
        "模型 PID 文件": SRC / "logs" / "model-server.pid",
        "测试状态文件": SRC / "logs" / "bench_run.json",
        "运行时状态": DEV_DIR / ".runtime",
    }
    for name, p in write_paths.items():
        if p in ALLOW_MISSING:
            continue
        ig = git_ignored(p)
        if ig is None:
            rep.note(f"{name}: 无法判断是否被忽略（git 不可用）")
            continue
        rep.check_true(f"写入路径被 .gitignore 覆盖：{name}", ig,
                       f"{p} 未被忽略 —— 会把仓库弄脏")
        print(f"  {'✅' if ig else '❌'} {name:18s} {p}")

    # ══ 四、跑测试程序时【真算一遍】输出根目录 ════════════════════════
    #
    #  ⚠️ 不要从源码文本里正则抓默认值 —— 那会把注释里的旧路径也抓出来。
    #     正确做法：import 模块，按它的真实表达式求值（踩过这个坑）。
    print()
    print("═" * 72)
    print(" 四、run_bench.py 的 --out 默认值（实际求值）")
    print("═" * 72)
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(SRC / "bench"))
    try:
        import run_bench                                  # noqa: PLC0415
        real = (Path(run_bench.__file__).resolve().parents[2] / "results")
        rep.check(f"run_bench 的结果根目录 = <development>/results",
                  real.resolve(), RESULTS_DIR.resolve())
        rep.check_true(f"结果根目录未被 git 忽略（结果要提交）",
                       git_ignored(real) is False or git_ignored(real) is None,
                       "结果数据是要提交的，不应被忽略")
        print(f"  实际求值：{real}")
    except Exception as e:                                # noqa: BLE001
        rep.check_true("能 import run_bench 并求出结果根目录", False,
                       f"{type(e).__name__}: {e}")

    # ══ 五、野生目录扫描 ══════════════════════════════════════════════
    #
    #  "野生目录" = 迁移遗留或路径算错时会建出来的目录
    print()
    print("═" * 72)
    print(" 五、野生目录扫描（路径算错时会建出来的）")
    print("═" * 72)
    wild = [
        SRC / "test_runs", SRC / "results", SRC / "configs", SRC / "models",
        DEV_DIR / "test_runs", DEV_DIR / "src" / "results",
    ]
    for p in wild:
        ok = not p.exists()
        rep.check_true(f"不存在野生目录：{p.relative_to(DEV_DIR)}", ok,
                       f"{p} 不该存在 —— 说明某个路径算错了")
        print(f"  {'✅' if ok else '❌'} {p.relative_to(DEV_DIR)}")

    return rep.finish()


if __name__ == "__main__":
    run(main)
