#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_report_assets.py —— 从 results/ 的原始 summary.json 生成报告配图

为什么要用脚本而不是手写数字：
    手写的图会和数据脱节。这里每次重跑都从 summary.json 读，
    数字变了图跟着变，报告里的文字也有 verify_claims.py 兜底。
"""

import json
import os
from pathlib import Path

DEV = Path.home() / "antlab/ANTLab-Infra-Tasks/Advanced Task/development"
RES = DEV / "results"
OUT = DEV / "reports/img"

# 保留的 9 次运行里，用于核心对比的三组
PAIRS = [
    ("S1\n单文档重复", "S1-单文档重复-baseline-20261019-161645",
     "S1-单文档重复-proxy-20261019-161734"),
    ("S2\n小文档轮转", "S2-小文档轮转-baseline-20261019-161815",
     "S2-小文档轮转-proxy-20261019-161931"),
    ("S3\n大文档竞争", "S3-大文档竞争-baseline-20261019-162320",
     "S3-大文档竞争-proxy-20261019-155701"),
]


def stats(run):
    p = RES / run / "summary.json"
    if not p.is_file():
        return None
    s = json.loads(p.read_text(encoding="utf-8"))
    rounds = [r.get("prefill_ms_sum") or 0 for r in (s.get("per_round") or [])]
    if not rounds:
        return None
    return {
        "rounds": rounds,
        "avg": sum(rounds) / len(rounds),
        "lo": min(rounds),
        "hi": max(rounds),
        "hit": s.get("hit_rate") or 0,
        "reuse": s.get("reuse_ratio") or 0,
        "n": s.get("n_total"),
    }


def make_svg():
    W, H = 860, 460
    L, R, T, B = 78, 24, 48, 96
    pw, ph = W - L - R, H - T - B
    rows = []
    for label, base, prox in PAIRS:
        a, b = stats(base), stats(prox)
        if a and b:
            rows.append((label, a, b))
    if not rows:
        raise SystemExit("没有可用的运行数据")

    vmax = max(max(a["hi"], b["hi"]) for _, a, b in rows)
    # 取整到 500
    vmax = (int(vmax / 500) + 1) * 500

    def y(v):
        return T + ph - v / vmax * ph

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{L}" y="26" font-size="15" font-weight="600" fill="#111">'
        f'每轮 prefill 耗时（越低越好）· 基线 vs 代理</text>',
        f'<text x="{L}" y="42" font-size="11" fill="#666">'
        f'柱高 = 各轮平均值，须线 = 各轮最小～最大；数据来自 results/*/summary.json</text>',
    ]

    # Y 轴网格
    steps = 5
    for i in range(steps + 1):
        v = vmax * i / steps
        yy = y(v)
        out.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L+pw}" y2="{yy:.1f}" '
                   f'stroke="#e5e7eb" stroke-width="1"/>')
        out.append(f'<text x="{L-10}" y="{yy+4:.1f}" font-size="11" fill="#666" '
                   f'text-anchor="end">{v:.0f}</text>')
    out.append(f'<text x="22" y="{T+ph/2:.0f}" font-size="11" fill="#666" '
               f'transform="rotate(-90 22 {T+ph/2:.0f})" text-anchor="middle">'
               f'prefill (ms)</text>')
    out.append(f'<line x1="{L}" y1="{T+ph}" x2="{L+pw}" y2="{T+ph}" '
               f'stroke="#9ca3af" stroke-width="1.5"/>')

    gw = pw / len(rows)
    bw = min(56, gw * 0.28)
    for i, (label, a, b) in enumerate(rows):
        cx = L + gw * (i + 0.5)
        for j, (st, color, name) in enumerate(
                ((a, "#9ca3af", "基线"), (b, "#2563eb", "代理"))):
            x = cx - bw - 6 + j * (bw + 12)
            top = y(st["avg"])
            out.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                       f'height="{T+ph-top:.1f}" fill="{color}" rx="2"/>')
            # 须线
            for v in (st["lo"], st["hi"]):
                out.append(f'<line x1="{x-4:.1f}" y1="{y(v):.1f}" '
                           f'x2="{x+bw+4:.1f}" y2="{y(v):.1f}" stroke="#374151" '
                           f'stroke-width="1"/>')
            out.append(f'<text x="{x+bw/2:.1f}" y="{top-5:.1f}" font-size="10" '
                       f'fill="#111" text-anchor="middle">{st["avg"]:.0f}</text>')
        out.append(f'<text x="{cx:.0f}" y="{T+ph+22}" font-size="12" fill="#111" '
                   f'text-anchor="middle">{label.split(chr(10))[0]}</text>')
        out.append(f'<text x="{cx:.0f}" y="{T+ph+38}" font-size="11" fill="#666" '
                   f'text-anchor="middle">{label.split(chr(10))[1]}</text>')
        # 提升百分比
        pct = (b["avg"] / a["avg"] - 1) * 100
        sign = "−" if pct < 0 else "+"
        col = "#16a34a" if pct < 0 else "#dc2626"
        nr = len(b["rounds"])
        note = f"{sign}{abs(pct):.0f}%"
        if nr < 3:
            note += f"（代理仅 {nr} 轮）"
        out.append(f'<text x="{cx:.0f}" y="{T+ph+58}" font-size="12" '
                   f'font-weight="600" fill="{col}" text-anchor="middle">{note}</text>')

    # 图例
    lx = L + 8
    for k, (color, name) in enumerate((("#9ca3af", "基线（直连，原生行为）"),
                                       ("#2563eb", "代理（三层缓存）"))):
        x = lx + k * 190
        out.append(f'<rect x="{x}" y="{T+ph+72}" width="11" height="11" '
                   f'fill="{color}" rx="2"/>')
        out.append(f'<text x="{x+16}" y="{T+ph+81}" font-size="11" '
                   f'fill="#374151">{name}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    svg = make_svg()
    (OUT / "baseline-vs-proxy.svg").write_text(svg, encoding="utf-8")
    print(f"  ✅ 写入 {OUT}/baseline-vs-proxy.svg")

    # 面板截图
    src = DEV / "web/screenshot.png"
    if src.is_file():
        (OUT / "dashboard.png").write_bytes(src.read_bytes())
        print(f"  ✅ 复制面板截图 → {OUT}/dashboard.png")
    else:
        print("  ⚠️ 没有 web/screenshot.png")


if __name__ == "__main__":
    main()
