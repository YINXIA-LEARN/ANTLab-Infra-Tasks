#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_09_web.py —— 前端元素完备性（静态检查）

═══════════════════════════════════════════════════════════════════════════
 对应题目（原文）
═══════════════════════════════════════════════════════════════════════════
 任务介绍·前端展示：
     「前端能够展示★服务状态、★测试配置、★请求执行状态、★模型输出、
       ★性能统计及★错误记录，并支持查看基线与优化后结果。
       至少覆盖单并发和多并发结果。」

 tests/README.md：
     「测试覆盖……请求与输出存储」

═══════════════════════════════════════════════════════════════════════════
 这个测试怎么判（独立来源）
═══════════════════════════════════════════════════════════════════════════
 判据 = 【题目原文列的那 6 项】逐条对上页面里的元素 id。

 独立来源是题目的要求清单：
     "前端能展示 X" → 页面上必须有一个 id 承载 X → 断言那个 id 存在。
 这跟我们的 JS 代码无关，只跟"页面有没有那块地方"有关。

 另外检查 JS 与 HTML 的一致性：
     脚本里 getElementById('foo') 引用的 id，HTML 里必须真的存在。
 这是最容易出的错（改了 id 忘了改另一边），而且页面会静默失灵。

 ⚠️ 只做静态检查（读文件），不启动浏览器 ——
    所以它随时能跑，不依赖服务在不在。

═══════════════════════════════════════════════════════════════════════════
 关键边界样例
═══════════════════════════════════════════════════════════════════════════
   ① 四个页签齐全（对话 / 测试 / 结果 / 服务）
   ② "未实现"角标不能贴在已实现的页签上（曾经错过一次）
   ③ JS 引用的 id 全部存在
   ④ JS 括号配对（曾经因为编辑失误把函数头删掉，语法照样过）

 退出码：0 通过 / 1 有失败 / 2 脚本自身出错
"""

import re
import sys
from pathlib import Path

# ⚠️ 本文件在 tests/core/ 下，_testlib.py 在上一层 tests/。
#    两层都要加：本层用于同目录的模块，上一层用于 _testlib。
#    （_testlib 必须留在 tests/ —— 它的 DEV_DIR/RESULTS_DIR
#      是按自己位置算的，挪下来会整体错一层）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _testlib import DEV_DIR, Report, run      # noqa: E402

WEB = DEV_DIR / "src" / "web"

# ── 题目原文列的 6 项展示能力 → 承载它的元素 id ───────────────────────
#   每一项都注明题目的原话，方便审查时对照
REQUIRED = {
    "服务状态": ["st-health", "st-model", "st-ctx", "st-vram"],
    "测试配置": ["bench-template", "bench-model", "bench-ctx", "bench-kv",
                 "bench-preview"],
    "请求执行状态": ["runs-list", "bench-progress"],
    "模型输出": ["messages", "results-body"],
    "性能统计": ["bench-svc", "results-tabs"],
    "错误记录": ["status-errors", "bench-done"],
}

# 四个页签
TABS = ["chat", "bench", "results", "service"]


def main():
    rep = Report("test_09_web —— 前端元素完备性")

    idx = WEB / "index.html"
    js = WEB / "app.js"
    css = WEB / "style.css"

    for f in (idx, js, css):
        rep.check_true(f"文件存在：{f.name}", f.is_file(), str(f))
    if not all(f.is_file() for f in (idx, js, css)):
        return rep.finish()

    html = idx.read_text(encoding="utf-8")
    script = js.read_text(encoding="utf-8")
    style = css.read_text(encoding="utf-8")

    print(f"  index.html {len(html):6d} 字符")
    print(f"  app.js     {len(script):6d} 字符")
    print(f"  style.css  {len(style):6d} 字符")
    print()

    # ── ① 四个页签 ───────────────────────────────────────────────────
    have_tabs = re.findall(r'data-tab="([^"]+)"', html)
    for t in TABS:
        rep.check_true(f"页签存在：{t}", t in have_tabs, f"实际 {have_tabs}")
    # 每个页签都要有对应的面板
    for t in TABS:
        rep.check_true(f"页签 {t} 有对应面板 panel-{t}",
                       f'id="panel-{t}"' in html)
    print(f"  页签：{have_tabs}")

    # ── ② "未实现"角标不能贴在已实现的页签上（曾经错过）─────────────
    #
    # 历史：测试页做完了，角标还挂在上面，用户一眼就看出不对。
    # 现在把"哪些页签算已实现"写死在测试里，改了实现就要同步改这里。
    #
    # ⚠️ 服务页从"未实现"变成"已实现"时，这一行也要跟着改 ——
    #    这个常量就是"实现进度"的单一记账处。
    IMPLEMENTED = {"chat", "bench", "results", "service"}
    for m in re.finditer(r'<button class="tab[^"]*" data-tab="([^"]+)"[^>]*>(.*?)</button>',
                         html, re.S):
        tab, body = m.group(1), m.group(2)
        has_badge = "badge-todo" in body
        if tab in IMPLEMENTED:
            rep.check_true(f"页签 {tab} 已实现，不应有『未实现』角标",
                           not has_badge, "角标还挂着")
    # 未实现的反而【应该】有角标，否则审查的人以为能用
    for m in re.finditer(r'<button class="tab[^"]*" data-tab="([^"]+)"[^>]*>(.*?)</button>',
                         html, re.S):
        tab, body = m.group(1), m.group(2)
        if tab not in IMPLEMENTED:
            rep.check_true(f"页签 {tab} 未实现，应有『未实现』角标",
                           "badge-todo" in body, "缺角标")

    # ── ②b 服务页的关键元素（这次新做的）─────────────────────────────
    #
    # ⚠️ 为什么单列一段：服务页的核心不是"长得对"，而是
    #    【两个启停按钮 + 一个开关 + 一句"当前会不会动作"的说明】。
    #    少任何一个，用户就不知道"我开了开关为什么没反应"。
    for eid, what in (
        ("btn-model-start", "单独启动模型服务按钮"),
        ("btn-model-stop", "单独停止模型服务按钮"),
        ("svc-watch-enabled", "测试期间自动重启开关"),
        ("svc-watch-will", "『当前会不会动作』的说明位"),
        ("svc-events", "服务事件时间线容器"),
        ("svc-model-status", "模型服务状态区"),
    ):
        rep.check_true(f"服务页有{eid}（{what}）",
                       f'id="{eid}"' in html)

    # 页面上必须写明"只在测试运行中才动作"这条规则 ——
    # 这是本功能最容易误解的地方，不写用户必然困惑
    rep.check_true("服务页写明了『只在测试运行中才动作』这条规则",
                   ("测试运行中" in html or "测试开始之前" in html)
                   and "不会" in html,
                   "页面没说明自动重启的作用范围")

    # ── ③ 题目要求的 6 项展示能力 ────────────────────────────────────
    for ability, ids in REQUIRED.items():
        missing = [i for i in ids if f'id="{i}"' not in html]
        rep.check_true(f"展示能力「{ability}」有承载元素 {ids}",
                       not missing, f"缺 {missing}")

    # ── ④ JS 引用的 id 必须存在于 HTML ───────────────────────────────
    #
    #  这是最容易出的错：改了 id 忘了改另一边 → 页面静默失灵，不报错。
    #  $('xxx') 是本项目取元素的写法（见 app.js 顶部）
    refs = set(re.findall(r"\$\('([A-Za-z0-9_\-]+)'\)", script))
    refs |= set(re.findall(r"getElementById\('([A-Za-z0-9_\-]+)'\)", script))
    html_ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
    # 动态创建的弹窗元素不算缺失（JS 里 innerHTML 造出来的）
    dynamic = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', script))
    missing = sorted(refs - html_ids - dynamic)
    rep.check_true(
        f"app.js 引用的 {len(refs)} 个 id 都在 HTML 或动态创建里",
        not missing, f"未找到：{missing[:12]}")

    # 反向：HTML 里的交互元素应该被 JS 用到（避免死元素）
    #  只查 btn- 开头的按钮，其余 id 可能是纯展示
    html_btns = {i for i in html_ids if i.startswith("btn-")}
    unused = sorted(html_btns - refs - dynamic)
    if unused:
        rep.note(f"HTML 里有 {len(unused)} 个按钮 id 没被 JS 引用：{unused[:8]}")

    # ── ⑤ JS 里的关键函数必须在（曾因编辑失误整个删掉过）─────────────
    need_fns = ["initTabs", "boot", "initChat", "initResults", "initBench",
                "benchStart", "benchPollOnce", "refreshStatus", "sendMessage"]
    for fn in need_fns:
        rep.check_true(f"app.js 里定义了 {fn}()",
                       f"function {fn}(" in script)
    # boot() 里要真的调用它们
    boot = re.search(r"function boot\(\)\s*\{(.*?)\n\}", script, re.S)
    if boot:
        body = boot.group(1)
        for fn in ["initTabs", "initChat", "initResults", "initBench"]:
            rep.check_true(f"boot() 调用了 {fn}()", f"{fn}(" in body)

    # ── ⑥ CSS：JS 用到的类名要有定义（避免裸样式）────────────────────
    #  只查测试页那几个新增类，其余历史类可能有内联样式
    for cls in ["bench-layout", "bench-form", "bench-side", "bench-block",
                "bench-actions"]:
        rep.check_true(f"CSS 定义了 .{cls}",
                       f".{cls}" in style)

    # ── ⑦ 静态资源的引用路径必须与后端路由一致 ───────────────────────
    #
    #  ⚠️ 这个错过一次：以为资源在 /app.js，实际后端挂在 /static/app.js。
    #     测试 URL 写错会让人误判成"页面坏了"。
    refs_assets = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', html)
    for a in refs_assets:
        rep.check_true(f"静态资源路径以 /static/ 开头：{a}",
                       a.startswith("/static/"),
                       "后端只挂载 /static/")
    print(f"  静态资源引用：{refs_assets}")

    return rep.finish()


if __name__ == "__main__":
    run(main)
