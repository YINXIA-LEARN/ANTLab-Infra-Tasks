#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_paths.py —— 逐个检查所有路径常量解析后是否真实存在

为什么需要它：
    迁移时我只按"已知清单"改了路径，结果漏了 models_config.py 的
    CONFIG_DIR（configs/ 迁到了 development/ 下，而代码还按 src/ 下的写）。
    教训：不能靠"我记得有哪几处"，要让程序把【所有】路径常量跑一遍，
    挨个 stat 看存不存在。

用法：python3 tools/audit_paths.py
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

print("=" * 88)
print(" 路径常量审计")
print("=" * 88)
print(f" src 目录: {SRC}")
print()

# (模块名, 属性名, 期望类型, 说明)
#
# 期望类型：
#   dir / file  = 必须已存在，不存在就是路径算错了 → 记为错误
#   maybedir    = 允许不存在（程序会自动创建，或代码里有 is_dir() 守卫）
#                 → 只提示，不算错误
#   path        = 运行期生成的文件，允许不存在
#   list        = 目录列表，逐项检查
CHECKS = [
    ("control.config",      "WEB_DIR",            "dir",      "前端静态目录"),
    ("control.config",      "MODEL_SEARCH_DIRS",  "list",     "模型搜索目录列表"),
    ("control.models_config", "CONFIG_DIR",       "dir",      "配置目录"),
    ("control.models_config", "MODELS_FILE",      "file",     "模型注册表"),
    ("control.model_manager", "LOG_DIR",          "maybedir", "日志目录（首次启动时创建）"),
    ("control.model_manager", "PID_FILE",         "path",     "模型服务 PID 文件"),
    ("control.model_manager", "SERVER_LOG",       "path",     "模型服务日志"),
    ("control.bench_store", "RUNS_ROOT",          "dir",      "测试结果根目录"),
    ("control.files",       "PHASE1_DIR",         "dir",      "源码根目录"),
    ("control.files",       "UPLOAD_DIR",         "maybedir", "用户上传目录（没人传过就不存在）"),
]


def describe(p, kind):
    p = Path(p)
    if kind in ("dir", "maybedir"):
        ok = p.is_dir()
    elif kind == "file":
        ok = p.is_file()
    else:                                    # path：运行期生成的文件
        ok = p.exists()
    if ok:
        return "✅ 存在", p, True
    if kind == "maybedir":
        return "○  尚未创建（允许）", p, True
    if kind == "path":
        return "○  尚未生成（允许）", p, True
    return "❌ 不存在", p, False


bad = []

for mod_name, attr, kind, desc in CHECKS:
    try:
        mod = __import__(mod_name, fromlist=[attr])
    except Exception as e:
        print(f"  ❌ {mod_name} 导入失败：{type(e).__name__}: {e}")
        bad.append((mod_name, attr, "导入失败"))
        continue
    if not hasattr(mod, attr):
        print(f"  ⚠️  {mod_name}.{attr} 不存在（可能改了名）")
        continue

    val = getattr(mod, attr)
    print(f"  {desc}")
    print(f"     {mod_name}.{attr}")

    if kind == "list":
        for v in val:
            if not v:
                print("       · (空项，跳过)")
                continue
            st, p, ok = describe(v, "maybedir")   # 搜索目录允许不存在
            print(f"       {st}  {p}")
    else:
        st, p, ok = describe(val, kind)
        print(f"       {st}  {p}")
        if not ok:
            bad.append((mod_name, attr, str(p)))
    print()

print("=" * 88)
if bad:
    print(f" ❌ 有 {len(bad)} 处路径解析错误：")
    for m, a, p in bad:
        print(f"     {m}.{a}  →  {p}")
    print()
    print(" 判据：类型标为 dir/file 的常量【必须】已存在。")
    print("       不存在就说明从 __file__ 往上找层级的写法算错了。")
else:
    print(" ✅ 所有必须存在的路径常量解析正确")
print("=" * 88)
sys.exit(1 if bad else 0)
