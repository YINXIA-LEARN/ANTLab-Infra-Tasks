# ANTLab 推理服务 —— 开发区域

> 基础阶段任务实现。本页是仓库入口，包含项目简介、系统结构、环境依赖、
> 模型获取与校验、运行方法、测试方法、前端访问方式，以及报告和数据位置。

---

## 一、项目简介

在 WSL 2 + llama.cpp 上搭建一套**可观测、可复现、可对比**的本地推理服务与性能测试台。

三件事：

| # | 做什么 | 对应目录 |
|---|---|---|
| 1 | **推理服务**：llama-server 加载 Qwen3 GGUF，GPU 全层推理，OpenAI 兼容接口 | `configs/` + 服务管理脚本 |
| 2 | **控制层 + 前端**：管理模型服务、组装输入、流式对话、浏览测试结果 | `src/control/` `src/web/` |
| 3 | **性能测试台**：按固定负载跑并发测试，逐请求记录指标，支持基线/优化对比 | `tests/core/` → `results/` |

**设计原则**：配置和结果都落在**磁盘文件**上，而不是浏览器或内存里。
这样每个运行都可追溯到具体的配置、输入文件和校验值。

---

## 二、系统结构

```
浏览器 ──► 控制层 127.0.0.1:8000 ──► llama-server 127.0.0.1:8080
             (src/run.py)                  (llama.cpp，GPU 推理)
                  │
                  └──► results/<运行编号>/   测试结果落盘
```

**两个服务是独立的**：控制层只管 Web 与代理，llama-server 只管推理。
控制层挂了不影响推理；llama-server 挂了控制层照常启动并报告不可达。

### src/ 内部结构

| 路径 | 职责 |
|---|---|
| `run.py` | 控制层入口：三项预检 + 启动 HTTP 服务 |
| `serve.sh` | **服务管理**：`start` / `stop` / `restart` / `status` / `logs` / `model` |
| `start.sh` `stop.sh` `restart.sh` | 只管理控制层的轻量脚本 |
| `control/` | Web 服务实现 |
| `control/config.py` | 端口、路径、默认参数、模型搜索目录 |
| `control/server.py` | 路由与静态文件（严格防路径穿越） |
| `control/chat.py` | 流式转发（SSE 透传） |
| `control/model_manager.py` | 模型服务启停、切换、失败回滚 |
| `control/models_config.py` | 模型注册表读写与校验 |
| `control/bench_store.py` | **测试结果只读读取**（以真实目录为准） |
| `control/files.py` | 资料文件白名单与读取（UTF-8 / GBK 回退） |
| `control/tokenizer.py` | 精确 token 计数（调用 `llama-tokenize`，按内容哈希缓存） |
| `common/` | 任务注册表与输入组装（被控制层和测试程序共用） |
| `bench/` | **性能测试程序**（见第五节） |
| `web/` | 前端（原生 JS，无框架）：对话 / 测试 / 结果 / 服务 四个页面 |
| `tools/` | 辅助脚本：结果汇总、备注校正、前端静态检查 |
| `prepare_inputs.py` | 单独核算输入 token（不改服务状态） |
| `kv_capacity.py` | KV 容量与显存实测 |
| `test_chat_stream.py` `compare_models.py` | 流式与模型对比的手工验证脚本 |

---

## 三、环境依赖

### 硬件（实测环境）

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU，**8151 MiB** |
| 驱动 | 573.22，CUDA 12.8，计算能力 sm_120（Blackwell） |
| CPU | 13th Gen Intel Core i7-13700HX |
| 内存 | 11814 MiB |
| 系统 | WSL 2（Ubuntu 24.04，内核 6.6.87.2-microsoft-standard-WSL2） |

### 软件

| 项 | 版本 | 位置 |
|---|---|---|
| Python | **3.12.3** | 系统自带 |
| llama.cpp | **0.5.0-dev (build 1, commit `9bf55f4`)** | `~/antlab/src/llama.cpp/build/bin/` |

**⭐ Python 零第三方依赖** —— 只用了标准库（`http.server`、`urllib`、`threading`、
`json`、`pathlib`、`subprocess` 等），不需要 pip 安装任何包。
这样评委在任何装了 Python 3.10+ 的机器上都能直接跑。

---

## 四、模型获取与校验

模型权重**不进仓库**（体积超 GitHub 限制），通过下面的信息复现。

| 模型 | 量化 | 大小 | SHA256 |
|---|---|---|---|
| `Qwen3-0.6B-Q8_0.gguf` | Q8_0 | 0.6B | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |
| `Qwen3-1.7B-Q8_0.gguf` | Q8_0 | 1.7B | `061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a` |

**获取方式**：从 Hugging Face 下载 Qwen3 系列 GGUF 的 Q8_0 量化版本，
放到下面任一位置即可（按顺序查找，先用先得）：

```
① 环境变量 $ANTLAB_MODEL_DIR 指定的目录
② ~/antlab/models/
③ 仓库内 models/（如果放了）
④ 当前工作目录的 models/
```

**校验**：

```bash
sha256sum ~/antlab/models/Qwen3-0.6B-Q8_0.gguf
# 应与上表一致
```

模型文件的 SHA256 同时记录在 `configs/models.json` 里，
测试程序开跑前会校验「配置指定的模型」与「服务实际加载的模型」是否一致，
不一致直接终止（避免用错模型跑出无效数据）。

---

## 五、运行方法

### 启动服务

```bash
cd "Basic Task/development/src"

bash serve.sh start      # 启动模型服务 + 控制层
bash serve.sh status     # 查看状态（模型、上下文、显存、PID）
bash serve.sh logs       # 实时看日志
bash serve.sh model      # 只重启模型服务（改了注册表后用）
bash serve.sh stop       # 全部停止
bash serve.sh restart    # 重启控制层（日志追加，不清空历史）
```

**端口**：控制层 `0.0.0.0:8000`，llama-server `127.0.0.1:8080`。

### 模型配置：配置与运行时状态是分开的两个文件

| 文件 | 进 git | 放什么 |
|---|---|---|
| `configs/models.json` | ✅ | 模型**定义**（有哪些模型、怎么启动、SHA256）+ `default_active` 默认值 |
| `.runtime/active-model.json` | ❌（`.gitignore` 覆盖） | **此刻正在用哪个模型** |

**读取优先级**：`.runtime/active-model.json` > 注册表的 `default_active` > 列表第一个模型。

**为什么拆开**：原先 `active` 写在注册表里，切一次模型 git 就脏一次，
提交历史混进"只是想换个模型试试"的噪音；而且把"配置"和"运行时状态"混在一个被跟踪的文件里，
语义也不清楚。拆开后注册表永远是干净的，切换仍然持久化（重启服务还记得）。

**想恢复成默认模型**：删掉 `.runtime/active-model.json` 即可。

**手动启动单个服务**（不通过 serve.sh）：

```bash
# 模型服务
~/antlab/src/llama.cpp/build/bin/llama-server \
  -m ~/antlab/models/Qwen3-0.6B-Q8_0.gguf \
  -ngl 99 -c 16384 --host 127.0.0.1 --port 8080

# 控制层
python3 run.py
```

### 模型服务固定带上的两个参数

`serve.sh` 与网页切换模型（`control/model_manager.py`）组装启动命令时，
除了注册表里的 `-ngl` / `-c`，还固定带两个参数：

| 参数 | 作用 | 为什么要它 |
|---|---|---|
| **`-v`** | 打开详细日志 | 默认 verbosity=3 时启动日志只有 9 行，**完全没有 GPU 信息**。题目要求「提供模型计算层和 KV 在 GPU 的日志证据」，加 `-v` 后约 4300 行，才有 `offloaded 29/29 layers to GPU`、`llama_kv_cache: layer N: dev = CUDA0`、`CUDA0 KV buffer size = …` 这些关键行 |
| **`--cache-ram 0`** | 关闭主机 RAM prompt cache | 题目第 17 行要求「关闭主机 RAM prompt cache，使基础阶段的缓存工作集驻留显存」；第 18 行把「KV 卸载到主机内存」划归进阶方向。**只关主机搬运，不影响 KV 位置，也不影响计算层的前缀复用** |

**⚠️ 两条启动路径必须保持一致**（`serve.sh model` 与网页切换模型）。
若只改一处，同一个模型会因为"谁启动的"而跑出不同配置，测试数据就不可比了。
改动时两边一起改。

**⚠️ 口径提示**：这两个参数是在迁移之后才加的。
`results/` 里 2026-10-12 之前的运行是在 prompt cache 开启、无 `-v` 的情况下跑的，
**不能和之后重跑的数据放进同一张对比表**。判断方法见 [`../results/README.md`](../results/README.md)。

---

### ⚠️ KV 容量的口径：`-c` 是**共享池总量**，不是每槽值

这一条最容易写错，报告里报错会被扣分（题目第 84 行专门要求说明"共享池或分槽方式"）。

**事实**：我们不显式给 `-np`，此时 llama.cpp 把 slot 数取默认 **4**，
并且 **`kv_unified = true`** —— **4 个 slot 共享一个 KV 池**，
而 `-c` 就是这个**池的总容量**。

```
-c 32768  →  一个 32768 token 的池，4 个 slot 分着用
             每槽可用容量是【动态的】≈ 池总量 ÷ 同时在跑的请求数
             （4 个都在跑时约 8192；只有 1 个请求时可以用满 32768）
```

**实测依据**（`-v` 日志）：

```
llama_kv_cache:      CUDA0 KV buffer size =  3584.00 MiB
3584 MiB ÷ 32768 token = 112 KiB/token   →  只够【一个】32768 的池
若真是 4 槽各 32768，需要约 14336 MiB —— 8 GB 显存根本装不下
```

**官方 help 的印证**：

```
-kvu, --kv-unified   use single unified KV buffer shared across all sequences
                     (default: enabled if number of slots is auto)
--kv-unified-per-slot N   ... when set without -c/--ctx-size, the shared KV pool
                          is sized to n_parallel*N
```

**⚠️ 一个容易误读的地方**：启动日志会打

```
load_model: initializing, n_slots = 4, n_ctx_slot = 32768, kv_unified = 'true'
```

那个 `n_ctx_slot = 32768` **是池的标称值，不是"每槽能拿 32768"**。
统一池模式下每个 slot 拿不到一整份。所以：

| 口径 | 值 |
|---|---|
| **配置总容量** | `-c` = 32768 token |
| **各 slot 可用容量** | 动态，最少约 8192（4 槽全忙），最多 32768（单请求） |
| **分配方式** | 共享池（`kv_unified=true`） |
| **KV 精度** | `f16`（默认，未显式给 `-ctk` / `-ctv`） |

**想改成"每槽固定一份"**：需要显式 `-np N`（此时 `-c` 变成总预算，
每槽 = `-c ÷ N`，`kv_unified=false`），或用 `--kv-unified-per-slot N`
（此时不写 `-c` 的话，池会按 `n_parallel × N` 自动定容）。
**本阶段没用这两种方式** —— 共享池更省显存，对"10 人以内"的场景够用。

---

### 前端访问方式

浏览器打开 **http://localhost:8000** （或局域网内 `http://<WSL IP>:8000`）。

四个页面：

| 页面 | 功能 |
|---|---|
| **对话** | 流式聊天；可选资料文件（用 `===== 资料区/任务区 =====` 包装）；实时 token 预算 |
| **测试** | 测试配置预览与前提检查（**规划中**） |
| **结果** | 浏览测试结果：汇总 / 逐请求 / 错误 / 配置 / **两轮对比** |
| **服务** | 服务状态、日志、异常恢复（**规划中**） |

---

## 六、测试方法

### 性能测试程序

```bash
cd "Basic Task/development/src"
python3 tests/core/run_bench.py --config ../configs/baseline-full.json --out ../results
```

**它做什么**：

1. **前置检查**：模型是否匹配、`note` 是否合规、输入是否装得下
2. **输入组装**：为每个任务生成 messages 与精确 token 数，产出 `prompt-index.json`
3. **三段式运行**：
   - **冷启动**：第一次请求（前缀缓存为空）
   - **预热**：2 次 P01（让前缀缓存热起来）
   - **正式**：按并发档 × 轮次跑固定负载
4. **落盘**：结果写到 `results/<运行编号>/`，并更新 `results/runs.json` 登记表

**运行编号格式**：`<配置标签>-<时间戳>`，例如 `baseline-full-r1-20261010-215643`。
时间戳保证**绝不覆盖**已有结果；同一配置可以跑多次，各自留下独立目录。

**退出码**：`0` 成功；`2` 模型不匹配；`3` 备注不合规。

### 测试配置

`../configs/` 下每份 JSON 定义一个固定负载：

```jsonc
{
  "label": "baseline-full-r1",          // 决定运行编号前缀
  "note":  "0.6B Q8_0 全层上卡 · -c 16384 · …",   // 只写本轮自己的参数
  "task_ids": ["P01", ..., "C02"],      // 固定任务集（10 个）
  "rounds": 3,                          // 每个并发档的轮数
  "concurrency": [1, 4, 10],            // 并发档
  "seed": 20261009,                     // 固定种子 → 任务顺序可复现
  "order": "shuffled",
  "max_tokens": 2048,
  "temperature": 0.0,
  "timeout_s": 900
}
```

现有配置：

| 配置 | 任务 | 轮 | 并发 | 请求数 |
|---|---|---|---|---|
| `baseline-c1.json` | 5 | 1 | [1] | 5 |
| `baseline-full.json` | 10 | 3 | [1, 4, 10] | 90 |
| `opt-ctx32k-full.json` | 10 | 3 | [1, 4, 10] | 90 |

**⚠️ 备注（`note`）规范**：只描述**本轮自己**的参数，不得出现
「唯一改动」「其余同基线」「相比上一轮」这类**依赖别的运行才能理解**的说法。
程序开跑前会校验，不合规直接拒绝（退出码 3）。
理由：备注会被抄进结果、前端和报告；一旦写成跨轮次的说法，
换了基线或再跑别的轮次，它就会变成**错误信息**。

---

## 七、报告和数据位置

| | 位置 |
|---|---|
| **测试数据** | [`../results/`](../results/) —— 按运行编号分目录，见该目录 README |
| **书面报告** | [`../reports/`](../reports/) —— **规划中**，尚未撰写 |
| **测试说明** | [`../tests/`](../tests/) —— **规划中** |
| **开发记录** | [`../docs/`](../docs/) —— 工作区规则等 |

### 一次运行产生的文件

```
results/<运行编号>/
├─ run-config.json     配置快照（复现的关键）
├─ meta.json           硬件、模型、服务端参数、控制变量（实际生效值）
├─ prompt-index.json   任务编号 ↔ 文件 ↔ sha256 ↔ token 数
├─ requests.jsonl      逐请求原始记录（含模型输出原文）
├─ summary.json        汇总统计（中位数 / P95 / 吞吐 / 显存峰值）
├─ errors.json         非成功请求
├─ gpu-samples-c*.csv  各并发档的显存与利用率采样
└─ run.log             运行日志
```

---

## 八、已知限制

| # | 限制 | 说明 |
|---|---|---|
| 1 | **测试页与服务页未实现** | 前端目前只有对话页和结果页可用 |
| 2 | **报告未撰写** | `reports/` 为空 |
| 3 | **KV 容量实测表未做** | 已写好 `kv_capacity.py`，尚未系统跑 |
| 4 | **模型切换后需重启服务** | 改 `-c` / `-ctk` 等参数要重启 llama-server（约 5 秒） |
| 5 | **显存预估系数未跨模型验证** | 约 114 KiB/token 是在 0.6B 上测的 |

---

## 九、复现检查清单

按顺序执行，可复现整套流程：

- [ ] 确认 GPU 与驱动（`nvidia-smi`，应看到 ≥ 8 GB 显存）
- [ ] 确认 llama.cpp 构建（`llama-server --version`，应为 `9bf55f4`）
- [ ] 下载模型并核对 SHA256（见第四节）
- [ ] `bash serve.sh start`，`bash serve.sh status` 应显示 health=ok
- [ ] 浏览器打开 http://localhost:8000 ，对话页发一条消息应能流式回答
- [ ] `python3 tests/core/run_bench.py --config ../configs/baseline-c1.json --out ../results`
- [ ] 结果页刷新，应能看到新出现的运行
