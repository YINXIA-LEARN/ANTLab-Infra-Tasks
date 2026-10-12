#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
logutil.py —— 统一日志

单独成模块的原因：chat.py 和 server.py 都要记日志，
如果 chat 从 server 导入，会形成循环导入。
"""

import datetime
import sys


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    """带时间戳与前缀的日志，立刻 flush（方便 tail -f 实时看）"""
    print(f"[{_ts()}] [control] {msg}", flush=True)


def log_err(msg):
    """输出到 stderr，同样立刻 flush"""
    print(f"[{_ts()}] [control] {msg}", file=sys.stderr, flush=True)
