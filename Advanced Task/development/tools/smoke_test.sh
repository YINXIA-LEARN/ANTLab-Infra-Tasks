#!/usr/bin/env bash
# 冒烟测试：验证代理的主路径
#   ① 首算 → miss + save
#   ② 再问 → L1 命中
#   ③ 塞满 6 份文档（> 4 个槽）→ 强制腾槽 → 再问第 1 份 → L2 命中
#   ④ 轻微改动（v1 → v2）
set -u
DEV="$HOME/antlab/ANTLab-Infra-Tasks/Advanced Task/development"
P="http://127.0.0.1:8091"
cd "$DEV" || exit 1

say() { echo ""; echo "── $* ──"; }

ask() {  # $1=doc文件 $2=问题
    python3 - "$1" "$2" <<'PY'
import json, sys, time, urllib.request
doc = open(sys.argv[1], encoding="utf-8").read()
body = {"messages": [
    {"role": "system", "content": "你是澄禾工作室的资料助手，回答要简洁。"},
    {"role": "user", "content": doc},
    {"role": "user", "content": sys.argv[2]}],
    "max_tokens": 96, "temperature": 0.0,
    "chat_template_kwargs": {"enable_thinking": False}}
req = urllib.request.Request("http://127.0.0.1:8091/v1/chat/completions",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
except Exception as e:
    print(f"  ❌ {type(e).__name__}: {e}")
    sys.exit(0)
wall = (time.time()-t0)*1000
px = d.get("_proxy") or {}
t = d.get("timings") or {}
u = d.get("usage") or {}
print(f"  {px.get('mode','?'):6s} slot={px.get('slot')}  "
      f"prompt_n={t.get('prompt_n')}/{u.get('prompt_tokens')}  "
      f"复用={px.get('reused_tokens'):>5}  prefill={(t.get('prompt_ms') or 0):6.0f}ms  "
      f"墙钟={wall:6.0f}ms")
msg = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
print(f"         → {msg.strip()[:78].replace(chr(10),' ')}")
PY
}

stat_now() { curl -s "$P/admin/status" | python3 -c "
import json,sys
d=json.load(sys.stdin); s=d['stat']
print('  slots :', d['slots'])
print('  tiers : L2 %.0f/%.0f MiB  L3 %.0f/%.0f MiB' % (
  d['tiers']['l2_used']/1048576, d['tiers']['l2_quota']/1048576,
  d['tiers']['l3_used']/1048576, d['tiers']['l3_quota']/1048576))
print('  entries:', d['entries'])
print('  请求 %d | L1=%d L2=%d L3=%d 前缀=%d miss=%d | save=%d restore=%d' % (
  s['requests'], s['hit_l1'], s['hit_l2'], s['hit_l3'], s['hit_prefix'],
  s['miss'], s['saves'], s['restores']))
print('  命中率 %.0f%%  复用 token %d/%d = %.1f%%' % (
  d['hit_rate']*100, s['reused_tokens'], s['total_prompt_tokens'],
  d['reuse_token_ratio']*100))
"; }

say "初始状态"
stat_now

say "① alpha 第 1 次（预期 miss + save）"
ask corpus/small/alpha-客户对接规范.md "这份规范的核心要求是什么？"

say "② alpha 第 2 次（预期 L1 命中）"
ask corpus/small/alpha-客户对接规范.md "违规会怎么处理？"

say "③ 塞入 5 份不同文档，强制把 alpha 挤出显存"
for d in beta-代码评审流程 gamma-设备借用登记 delta-数据备份策略 epsilon-会议纪要模板 zeta-远程办公申请; do
    echo "  [$d]"
    ask "corpus/small/$d.md" "这条制度最关键的一点是什么？"
done

say "④ 再问 alpha（它的槽应该已被挤掉 → 预期 L2 命中）"
ask corpus/small/alpha-客户对接规范.md "登记要在几个工作日内完成？"

say "⑤ 轻微改动：先问 v1，再问 v2（v2 应复用绝大部分前缀）"
ask corpus/versions/知识库-v1.md "设备借用期限是几天？"
ask corpus/versions/知识库-v2.md "设备借用期限是几天？"

say "⑥ 状态"
stat_now

say "⑦ 事件（最后 14 条）"
tail -14 results/_live/events.jsonl 2>/dev/null | python3 -c "
import json,sys
for l in sys.stdin:
    try:
        e=json.loads(l)
        keep={k:v for k,v in e.items() if k not in ('t','ts','event')}
        print('  %-15s %s' % (e.get('event'), json.dumps(keep, ensure_ascii=False)[:125]))
    except Exception: pass
"
