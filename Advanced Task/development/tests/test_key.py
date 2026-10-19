#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_key.py —— 缓存键与请求拆分

断言什么
  1. 同样的前缀 + 同样的配置 → 同一个键（可复现）
  2. 文档内容变一个字 → 键就变（不能互相命中）
  3. 配置指纹变 → 键就变（旧缓存必须作废）
  4. 系统消息变化也算前缀变化
  5. 拆分规则：最后一条是"问题"，前面都是前缀
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib as T                                            # noqa: E402

sys.path.insert(0, str(T.DEV / "proxy"))
import proxy as P                                            # noqa: E402


def make_proxy(fp="fp-A"):
    """只用来测 cache_key，不启动任何东西"""
    obj = object.__new__(P.Proxy)
    obj.fp = fp
    return obj


def t_deterministic():
    T.case("键的确定性")
    px = make_proxy()
    k1 = px.cache_key("文档正文")
    k2 = px.cache_key("文档正文")
    T.eq(k1, k2, "同样输入两次得到同一个键")
    T.check(len(k1) == 16, "键是 16 位十六进制", f"len={len(k1)}")
    T.check(all(c in "0123456789abcdef" for c in k1), "键只含十六进制字符")


def t_content_change():
    T.case("文档内容变一个字 → 键必须变")
    px = make_proxy()
    a = px.cache_key("设备借用期限：7 天")
    b = px.cache_key("设备借用期限：14 天")
    T.check(a != b, "改一个数字键就不同（v1/v2 不会互相命中）",
            f"{a} vs {b}")
    # 反过来：只有空白差异也算不同（保守是对的）
    c = px.cache_key("设备借用期限：7 天 ")
    T.check(a != c, "末尾多一个空格也算不同")


def t_fingerprint():
    T.case("配置指纹进键 → 换配置旧缓存作废")
    a = make_proxy("qwen|np4|c32768|f16").cache_key("同一份文档")
    b = make_proxy("qwen|np8|c32768|f16").cache_key("同一份文档")
    T.check(a != b, "np4 与 np8 的键不同",
            f"{a} vs {b}")
    c = make_proxy("qwen|np4|c32768|q8_0q8_0").cache_key("同一份文档")
    T.check(a != c, "f16 与 q8_0 的键不同")


def t_split():
    T.case("请求拆分：最后一条是问题，前面是前缀")
    msgs = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "文档正文"},
        {"role": "user", "content": "问题"},
    ]
    prefix, rest = P.Proxy.split_messages(msgs)
    T.eq(prefix, "系统提示\n\n文档正文", "前缀 = 除最后一条外的全部")
    T.eq(len(rest), 1, "剩余只有一条")
    T.eq(rest[0]["content"], "问题", "剩下的就是问题")

    # 问题变了，前缀不变 → 键必须不变
    px = make_proxy()
    msgs2 = msgs[:2] + [{"role": "user", "content": "另一个问题"}]
    p2, _ = P.Proxy.split_messages(msgs2)
    T.eq(px.cache_key(prefix), px.cache_key(p2),
         "换问题不影响键（这正是缓存能命中的原因）")

    # 文档变了 → 键要变
    msgs3 = [msgs[0], {"role": "user", "content": "另一份文档"}, msgs[2]]
    p3, _ = P.Proxy.split_messages(msgs3)
    T.check(px.cache_key(prefix) != px.cache_key(p3), "换文档键就变")


def t_edge():
    T.case("边界情况")
    px = make_proxy()
    T.eq(P.Proxy.split_messages([]), ("", []), "空消息列表不崩")
    p, r = P.Proxy.split_messages([{"role": "user", "content": "只有一条"}])
    T.eq(p, "只有一条", "只有一条时它当前缀")
    T.eq(r, [], "没有剩余")
    T.eq(px.cache_key(""), px.cache_key(""), "空前缀也能算键")


def main():
    for fn in (t_deterministic, t_content_change, t_fingerprint,
               t_split, t_edge):
        T.run(fn)
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
