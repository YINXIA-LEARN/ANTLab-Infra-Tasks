# results/ 索引

本目录是**原始记录**，报告里的每个数字都能在这里追到。
每次运行一个子目录，目录名 = `<场景>-<组>-<时间戳>`。

## 一、保留的 9 次运行

| 运行编号 | 组 | 轮数 | 请求 | 命中率 | 复用 | prefill 合计 | 用途 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `S1-单文档重复-baseline-20261019-161645` | 基线 | 3 | 30/30 | 0.0% | 96.4% | 773.9 ms | 对照组：工作集装得下 |
| `S1-单文档重复-proxy-20261019-161734` | 代理 | 3 | 30/30 | 96.7% | 96.4% | 725.9 ms | 同上（−6%） |
| `S2-小文档轮转-baseline-20261019-161815` | 基线 | 3 | 72/72 | 0.0% | **2.1%** | 3 993.2 ms | **主战场**：工作集 > 槽位 |
| `S2-小文档轮转-proxy-20261019-161931` | 代理 | 3 | 72/72 | 88.9% | **87.9%** | 1 045.7 ms | 同上（**−74%**） |
| `S3-大文档竞争-baseline-20261019-162320` | 基线 | 3 | 45/45 | 0.0% | **99.7%** | 583.8 ms | 负面场景：代理反而更慢 |
| `S3-大文档竞争-proxy-20261019-155701` | 代理 | 1 | 15/15 | 86.7% | 86.4% | 894.2 ms | 同上（**+358%**） |
| `S4-冷热混合-proxy-20261019-155514` | 代理 | 1 | 35/35 | 71.4% | 60.9% | 1 237.8 ms | 冷启动：三层全空 |
| `S4-冷热混合-proxy-20261019-155939` | 代理 | 1 | 35/35 | 100.0% | 72.8% | 1 034.3 ms | 跨层：含 6 次 L3 命中 |
| `S5-版本更新-proxy-20261019-155548` | 代理 | 1 | 6/6 | 100.0% | 99.8% | 45.5 ms | v1→v2 失效与前缀复用 |

## 二、每个运行目录里的文件

| 文件 | 内容 |
| --- | --- |
| `requests.jsonl` | **逐请求原始记录**：计划/实际到达时刻、`arrival_dev_ms`、命中模式、槽位、`prompt_n` / `prompt_total`、复用 token 数、prefill 与生成耗时、完整回答 |
| `tiers.csv` | 每 250 ms 采样一次的三层容量（L1 槽位占用 / L2 / L3 / 条目数 / 请求数） |
| `summary.json` | 汇总：模式分布、命中率、复用比例、prefill 的 p50/p95、逐轮明细、逐文档拆分 |
| `run-config.json` | 本次全部参数 + 语料清单（复现用） |
| `run.log` | 过程日志（含预热、每轮开始/结束） |

## 三、`_summary/` 与 `_live/`

| 路径 | 内容 |
| --- | --- |
| `_summary/matrix.md` | 基线 vs 优化的对比表（由 `tools/make_matrix_table.py` 从上述运行生成） |
| `_summary/matrix.json` | 同上，机器可读 |
| `_live/events.jsonl` | **策略事件流**（运行时实时写入）：`hit` / `miss` / `save` / `restore` / `promote` / `migration` / `spill_done` / `erase` / `evict` / `stale_slot` / `stale_config_evict` / `prefix_probe` / `skip_cache` / `restore_failed` |
| `_live/status.json` | 面板轮询用的快照 |

`_live/` 是运行时状态，**不入库**（见 `.gitignore`）。

## 四、统计口径（读数据前先看这个）

1. **命中率按请求数算**，复用占比按 **token** 算 —— 两者不能混。一个 100 token 的命中
   和一个 5 000 token 的命中，对命中率贡献相同。
2. **命中与否看服务端 `prompt_n`，不看耗时** —— 耗时会被生成长度污染。
   L1/L2/L3 命中时 `prompt_n` 通常 1～18；miss 时等于 `prompt_total`。
3. **prefill 只用服务端 `timings.prompt_ms`**，不含网络、排队、代理开销；
   代理开销在 `requests.jsonl` 的 `proxy_wall_ms − (prompt_ms + predicted_ms)` 里单列。
4. **基线不是「零缓存」** —— llama.cpp 原生有槽内前缀复用，所以 S1/S3 的基线复用率
   高达 96.4% / 99.7%。基线口径是「原生行为」。
5. **失败与取消**都记成完整记录（`status="error"` / `"timeout"`），不静默丢弃；
   没有重试机制（重试会改变到达序列，破坏可比性）。

## 五、想复现哪一组

```bash
cd ~/antlab/ANTLab-Infra-Tasks/"Advanced Task/development"

# 清空三层缓存，回到冷状态
bash tools/stop_adv.sh
rm -rf /dev/shm/antlab_kv kv_cache/L3 kv_index.json results/_live
bash tools/start_adv.sh

# 基线组（直连，不经过代理）
python3 tools/replay.py --workload configs/workloads/S2.json --target baseline --rounds 3

# 优化组（经过代理）
python3 tools/replay.py --workload configs/workloads/S2.json --rounds 3

# 重新生成对比表
python3 tools/make_matrix_table.py

# 只核对结论、不重新推理
python3 tools/verify_claims.py
```

## 六、已知的数据缺口

- **S3 / S4 / S5 没有 3 轮基线对照**。跑批时 llama-server 在 S3 基线的某个请求上
  偶发挂住（客户端等到 900 s 超时，代理与模型都空闲），两次重跑在同一位置复现，
  问题未定位。回放器已加看门狗与单请求 240 s 超时，但这三组的对照数据确实缺。
- **S6 跨重启**未跑完整对照。
- 3 轮只给中位数与波动范围，**没有做统计显著性检验**。
