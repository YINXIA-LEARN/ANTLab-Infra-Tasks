# 测试结果

按运行编号保存结果。本目录下的每个子目录 = 一次完整的测试运行。

```text
results/
├─ runs.json                                 运行登记表（★ 派生索引，见下）
├─ report-data.txt                           汇总与单变量对比（分析脚本产出）
├─ code-eval.json                            编程任务逐运行判定
├─ office-eval.json                          办公任务逐运行命中情况
├─ kv-capacity-probe.txt                     KV 容量实测（单请求二分探测）
├─ ctx16k-f16-06b-r1-20261016-222516/        一次运行
│  ├─ run-config.json     配置快照（复现的关键）
│  ├─ meta.json           硬件、模型、服务端实际参数、控制变量
│  ├─ prompt-index.json   任务编号 ↔ 文件 ↔ sha256 ↔ token 数
│  ├─ requests.jsonl      逐请求原始记录（含模型输出原文）
│  ├─ summary.json        汇总统计（冷启动/预热/正式三段）
│  ├─ errors.json         非成功请求（按排查优先级排序）
│  ├─ gpu-samples-c*.csv  各并发档的显存与利用率采样
│  ├─ service-starts.jsonl  本运行期间服务被拉起的记录（有它 = 跨了实例）
│  ├─ run-state.json      运行状态
│  └─ run.log             该次运行的日志
└─ ctx32k-f16-06b-r1-20261016-223012/
```

> **⚠️ 目录名是【按真实配置】命名的**（`<上下文>-<KV精度>-<模型>-r<轮次>-<时间戳>`）。
> 早先的 label 曾与真实配置大面积错配（例如名为 `kv_baseline` 的那轮实际用的是
> q8_0、名为 `kv_opt` 的那轮实际用的是 f16），按 label 配对会把结论搞反。
> 命名规则与重命名脚本见 `../tools/analysis/fix_run_labels.py`。

---

## ⚠️ 数据口径：两次改动前后不可混用

性能数据只有在**服务端参数一致**时才可比。本目录下已有数据跨过一次口径变更：

| 运行 | 时间 | 主机 RAM prompt cache | `-v` 详细日志 |
|---|---|---|---|
| `baseline-full-r1-20261010-215643` | 10-10 | **开启**（默认，上限 8192 MiB） | ❌ 无 |
| `opt-ctx32k-full-r1-20261011-101250` | 10-11 | **开启** | ❌ 无 |
| `baseline-c1-r1-20261012-182935` | 10-12 | **开启** | ❌ 无 |
| （之后重跑的运行） | — | **关闭**（`--cache-ram 0`） | ✅ 有 |

**为什么改**：题目第 17 行要求「关闭主机 RAM prompt cache，使基础阶段的缓存工作集驻留显存」，
第 18 行把「KV 卸载到主机内存」划归进阶方向。所以基础阶段的正式基线必须是**纯显存内推理**。

**怎么判断某次运行属于哪种口径** —— 看 `meta.json` 的 `server_config.server_flags`：

```jsonc
// 新数据（重跑之后）会有这些字段
"server_flags": {
  "ngl": "99", "ctx": "32768", "np": null,
  "cache_ram": "0",                    // ⭐ "0" = 主机 prompt cache 已关
  "cache_type_k": null, "cache_type_v": null,
  "flash_attn": null, "verbose": true, // ⭐ -v 已开
  "prompt_cache_disabled": true        // ⭐ 直接可判
}
```

**旧数据没有 `server_flags` 字段** —— 看到它缺失，就说明那次是在 prompt cache 开启、
无 `-v` 的情况下跑的。写报告时不要把它和新数据放进同一张对比表。

此外 `meta.json` 的 `server_config` 里还有：

- `process_cmdline` —— 从活着的进程命令行读到的**实际**启动参数（最权威）
- `runtime_active_model` —— 运行时选择的模型（`.runtime/active-model.json`）
- `active_model` —— 注册表里的默认模型（`configs/models.json` 的 `default_active`）

---

## GPU 驻留证据在哪

题目要求「提供模型计算层和 KV 在 GPU 的日志与监控证据」。证据分两处：

### 日志（`src/logs/model-server.log`）

加了 `-v` 之后，每次启动约 4300 行，关键几行是：

```text
common_param:   - CUDA0   : NVIDIA GeForce RTX 5060 Laptop GPU (8150 MiB, 6946 MiB free)
load_tensors: layer   0 assigned to device CUDA0, is_swa = 0        ← 逐层
load_tensors: offloaded 29/29 layers to GPU                          ← 全部计算层
llama_kv_cache: layer   0: dev = CUDA0                               ← KV 逐层
llama_kv_cache:      CUDA0 KV buffer size =  3584.00 MiB             ← KV 在显存的量
load_tensors:        CUDA0 model buffer size =   604.15 MiB         ← 权重在显存的量
sched_reserve:       CUDA0 compute buffer size =    28.01 MiB       ← 计算缓冲
load_model: prompt cache is disabled - use `--cache-ram N` to enable it   ← 主机 cache 已关
```

**注意**：日志会被后续启动追加（用 `>>`，不覆盖），所以查某次运行的证据要按
`====...` 分隔线和 `启动：<完整命令行>` 那行定位该次的段落。

### 监控（每次运行目录内）

| 文件 / 字段 | 内容 |
|---|---|
| `gpu-samples-c<N>.csv` | `t_s,memory_used_mib,gpu_util_pct` 时间序列，每并发档一份 |
| `meta.json` → `server_config.vram_after_load_mib` | 模型加载后的显存占用 |
| `summary.json` → `formal.<并发>.vram_peak_mib` | 该并发档的显存峰值 |

**两者合起来才能把账对上**：日志说"哪些东西放在显存里、各占多少"，
监控说"显存实际涨了多少、GPU 真的在算"。

---

## runs.json 的作用与边界

`runs.json` 是**派生索引**，不是权威数据。

- 权威数据是**各运行目录本身** —— 手动删掉一个目录，前端和 `tools/show_runs.py`
  都会把它显示成「目录已丢失」而不是静默忽略。
- 反过来，目录在而登记表里没有（比如从别的机器拷来的结果），也照常能读。
- 因此**不要手工编辑 runs.json**；要改备注用 `src/tools/fix_run_note.py`。

### ⚠️ 三个字段曾经留过旧值（已修正，留档）

按真实配置重命名运行目录时，只同步了 `run_id`，另外三个字段留着旧值：

| 字段 | 曾经的值 | 问题 |
| --- | --- | --- |
| `dir` | 旧目录名 | **指向已不存在的目录** |
| `label` | 旧标签 | 与目录名不符 |
| `server_args.ctx` | 恒为注册表默认值 32768 | ★ 9 轮里有 8 轮实际跑的是 16384，**这个字段是错的** |

其中 `server_args.ctx` 最危险 —— 按它配对，上下文实验的结论会完全反过来。
现已改为从各运行目录的 `run-config.json` / `meta.json` 重算，
9 条与目录一一对应。修正脚本：`../tools/analysis/fix_runs_registry.py`
（该脚本写回前会自检条数、写回后会读回校验，防止把登记表写坏）。

**再次强调：`runs.json` 只是索引。判断某轮的真实配置，
一律看该轮目录里的 `meta.json → server_config.server_flags`
（那是从进程命令行读的，最权威）。**

---

## ⚠️ KV 容量的口径：三个数不能混

题目第 84 行要求「记录**配置总容量、各 slot 可用容量和实测可用容量**；
说明**共享池或分槽方式**、KV 精度、并发数及验证边界」。
这三个数是不同的东西，报告里混用会被扣分。

**现状**：不显式给 `-np`，slot 数取默认 4，且 **`kv_unified = true`**
—— 4 个 slot **共享一个 KV 池**，`-c` 就是这个池的总容量。

| 口径 | 当前值 | 怎么得到 |
|---|---|---|
| **配置总容量** | `-c` = 32768 token | 启动参数 |
| **各 slot 可用容量** | **动态**：最少约 8192（4 槽全忙）<br>最多 32768（只有 1 个请求） | 池总量 ÷ 同时在跑的请求数 |
| **实测可用容量** | 待测（用 `src/kv_capacity.py`） | 逐步加大输入直到失败 |
| **分配方式** | **共享池**（不是分槽） | 日志 `kv_unified = true` |
| **KV 精度** | `f16`（默认，未给 `-ctk` / `-ctv`） | 进程命令行 |
| **KV 位置** | `CUDA0`（全部层） | 日志 `llama_kv_cache: layer N: dev = CUDA0` |
| **KV 实占显存** | 3584 MiB @ ctx 32768 | 日志 `CUDA0 KV buffer size` |

**实测依据**（为什么确定是"共享池"而不是"每槽一份"）：

```
CUDA0 KV buffer size = 3584.00 MiB
3584 MiB ÷ 32768 token = 112 KiB/token  →  只够一个 32768 的池
若 4 槽各一份需约 14336 MiB，而显卡总共只有 8151 MiB —— 不可能
```

**⚠️ 容易误读的一行日志**：

```
load_model: initializing, n_slots = 4, n_ctx_slot = 32768, kv_unified = 'true'
```

`n_ctx_slot = 32768` 是**池的标称值**，不是"每槽能拿 32768"。
`src/serve.sh status` 也曾经打印成"每槽 ctx=32768"（**已修正**）。

**想改成"每槽固定一份"**：显式 `-np N`（`-c` 变总预算，每槽 = `-c ÷ N`，
`kv_unified=false`），或用 `--kv-unified-per-slot N`。**本阶段没用**。

---

## 字段说明

### 逐请求记录（`requests.jsonl`）

| 字段 | 含义 |
|---|---|
| `request_id` | 请求编号，形如 `HHMMSS-<8位十六进制>`，同一次运行内唯一 |
| `task_id` | 提示词编号（P01–P04 / D01–D04 / C01–C02） |
| `concurrency` / `wave` / `round` | 该请求属于哪个并发档、第几个波次、第几轮 |
| `status` | `success` / `incomplete` / `failed` / `timeout` / `harness_error` |
| `ttft_ms` | 首 token 时间（客户端计时，含排队与传输） |
| `tpot_excl_first_ms` | 排除首 token 的每 token 时间 |
| `prompt_tokens` / `output_tokens` | 来自 `usage`，是权威计数 |
| `prompt_n_computed` / `cache_hit_tokens` | 服务端本次**实际计算**的 token 数 / 命中前缀缓存的 token 数 |
| `output_text` | 模型输出原文 |
| `finish_reason` | `stop` / `length`；**只有拿到它才算 `success`** |

**⚠️ 关于成功率**：分母是该并发档的请求总数，只有拿到 `finish_reason`
（或流里出现过 `[DONE]`）的才算成功。服务端因 KV 溢出 `send_error`
断开连接时，已经发出响应头的请求会看到 HTTP 200 但没有结束标记 ——
这类必须记成 `incomplete`，不能算成功。

### 汇总统计（`summary.json`）

| 字段 | 口径 |
|---|---|
| `formal.<并发>.ttft_ms` | 该档所有成功请求 TTFT 的中位数与 P95 |
| `formal.<并发>.throughput.output_tokens_per_s` | 该档**实测墙钟时间**窗口内的输出 token 速率 |
| `cold_start` | 服务刚加载完、前缀缓存为空时的第一次请求 |
| `warmup` | 正式测试前的预热请求（让前缀缓存热起来） |

**吞吐窗口用墙钟时间，不用请求内的相对时间戳** ——
后者会低估窗口长度，曾经把吞吐高估约 12 倍。
