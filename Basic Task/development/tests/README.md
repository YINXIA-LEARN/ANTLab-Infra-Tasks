# 测试

在本目录组织项目测试，注明依赖、输入、运行方式和判定依据。

---

## 〇、目录怎么分的

```
tests/
├─ _testlib.py                  共用骨架（★ 故意放在根，别挪）
├─ run_all.sh                   跑全部
├─ README.md                    本文件
├─ test_11_service_watch.py     服务监测与「测试期间自动重启」
└─ core/                        功能与数据检查
   ├─ README.md
   ├─ test_00_paths.py
   ├─ test_01_input.py
   ├─ test_02_token_counting.py
   ├─ test_03_metrics.py
   ├─ test_04_concurrency.py
   ├─ test_08_output_code.py
   ├─ test_09_web.py
   └─ test_10_midstop.py
```

**`test_11` 单独留在根**：它测的是**服务监测与自动重启**这套功能，
与其他测试不是一类 —— 它一条运行数据都不读，全部靠调 `/api/service/*`
看真实行为；而 `core/` 里的都是**读 `results/` 下的原始记录**做检查。

**⚠️ `_testlib.py` 为什么不能放进 `core/`**：

它的路径锚点是按**自己的位置**向上推算的：

```python
TESTS_DIR   = Path(__file__).resolve().parent     # tests/
DEV_DIR     = TESTS_DIR.parent                    # development/
RESULTS_DIR = DEV_DIR / "results"
```

放在 `tests/` 时 `DEV_DIR` 正好是 `development/`；一旦挪进 `tests/core/`，
`DEV_DIR` 会变成 `tests/`，`RESULTS_DIR` 就指向
`development/tests/results` —— **不存在**，所有读 `results/` 的测试一起失灵。

所以 `core/` 里的测试用**两行** `sys.path.insert` 把上两层都加进来：

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))          # core/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/ ← _testlib 在这
```

---

## 一、怎么跑

```bash
cd development/tests
bash run_all.sh            # 跑全部（递归找两处），退出码 = 失败个数
```

单个测试（**可以在任意目录跑**，路径是按 `__file__` 算的）：

```bash
python3 core/test_03_metrics.py    # 退出码 0 通过 / 1 有失败 / 2 脚本自身出错
python3 test_11_service_watch.py
```

**依赖**：Python 3.12，**零第三方包**（全部标准库）。

**前置**：`results/` 下至少要有一次带 `requests.jsonl` 的运行。

**⚠️ 但 `test_11` 例外**：它需要控制层在跑（要调 `/api/service/*`），
`core/` 里那 8 个不需要 —— 它们是读文件推断，不连端口。

**若 `results/` 是空的**（刚 clone 下来），先跑一次：

```bash
cd ../src
python3 core/run_bench.py --config ../../configs/baseline-full.json
```

---

## 二、测验清单

| 文件 | 测什么 | 独立来源 |
| --- | --- | --- |
| `core/test_00_paths.py` | 路径解析 + 写入是否落在 `.gitignore` 内 + **配置落位** | 文件系统 + `git check-ignore` |
| `core/test_01_input.py` | 输入组织、50,000 token 上限、模板开销、任务构成 | **题目规定的 50,000 上限** |
| `core/test_02_token_counting.py` | **流式块 vs token** | 两个独立来源：客户端数块 / 服务端报 token |
| `core/test_03_metrics.py` | TTFT / TPOT / 吞吐 / P95 的计算 | **另写一套算法重算**（见 `_testlib.py`） |
| `core/test_04_concurrency.py` | 并发档、请求数、排队现象 | 配置声明 vs 实际记录 |
| `core/test_08_output_code.py` | **编程输出运行验证** | **真编译、真执行** |
| `core/test_09_web.py` | 前端 6 项展示能力是否齐备 | **题目原文列的 6 项** |
| `core/test_10_midstop.py` | 中途被停止的测试，数据还在不在 | `requests.jsonl` 行数 vs `run-state.json` 的 state |
| `test_11_service_watch.py` | 自动重启的**规则**（只在测试运行中动作） | 调 `/api/service/*` 看真实行为 |

**合计约 2100 项断言**（随 `results/` 里的运行数量增长 ——
很多检查是逐运行、逐请求展开的）。


### 原计划里被移除的部分

| 文件 | 原计划测什么 | 现状 |
| --- | --- | --- |
| `test_05_errors.py` | 请求失败后的计数、有上限的重试 | **移除**（见下） |
| `test_06_recovery.sh` | 服务退出 → 检测 → 重启 → 就绪 → 恢复后验证 | **移除**（见下） |
| `test_07_output_office.py` | 办公输出检查（事实/条款/格式） | 未做，需人工判定为主 |

#### ⚠️ 为什么移除 test_05 / test_06（后来又部分做了回来）

这两个测试属于**「主动制造异常」**一类：杀掉服务、注入超时、伪造失败请求。
它们依赖一套配套功能（后台监测 + 自动重启 + 测试钩子），而**当时那套功能有害**：

```
它分不清两种情况：
    ① 服务真的崩了
    ② 有人正在有意重启服务
        （`serve.sh model` 的语义就是"先停后起"，中间约 3 秒没有监听）

第 ② 种在监测器眼里和崩溃【一模一样】，于是它每次判定掉线都要去"救"，
而"救"的过程（停进程 + 启动 + 等就绪）自己又制造一次空窗 ——
服务被反复停起。

实测表现：手动测试时服务很稳（静置 60 秒、107 秒 health 都是 200），
          一跑测试脚本就频繁掉线，恢复日志里每 2~3 分钟一条。
```

**根因**：没有可靠信号区分"有意重启"与"真崩"。

##### 后来的解决办法：加一道"总闸"

不再试图去【分辨】那次掉线是有意还是意外 —— 换个思路，**只在明确的时间窗内动作**：

```
测试运行中   → 服务掉了是意外，救它
其余时间     → 只提示，绝不动手
             （那多半是有人故意停的 / 换模型）
```

这条规则把"分辨"这件事从监测器转移到了时间窗上，**不需要任何额外信号**。
另外还有一道**宽限期**（默认 90 秒）兜住时序问题：服务在测试中途挂掉时，
剩下的请求会很快集体失败、测试提前退出，等监测器探活时已经"没有测试在跑"了 ——
没有宽限期就会眼睁睁看着不去救（这是实测踩出来的）。

**现在这套功能回来了**，对应 `src/control/service_watch.py` +
服务页的开关 + `test_11_service_watch.py`。开关**默认关**。

**仍然移除的**：`run_bench.py` 里的 `--cancel-after` / `--timeout-override`
两个测试钩子（往正式测试程序里塞"主动注入失败"的开关，弊大于利）。

**保留的**：`client.py` 里的 `cancel_event` 取消能力（纯粹的客户端能力，
不碰服务进程，可用代码直接调用）。

---

## 三、判定依据（★ 每个测试的独立来源）

**测试的价值全在"独立来源"这一列。** 拿被测代码验证它自己叫循环论证，永远通过。

| 断言 | 独立来源 | 强度 |
| --- | --- | --- |
| `ttft_ms == (t_first - t_submit) × 1000` | 记录里的**原始时间戳** + 手算 | 强 |
| `summary` 的中位数 == 逐请求重算 | `_testlib.py` 里**另写的中位数** | 强 |
| `summary` 的 P95 == 逐请求重算 | `_testlib.py` 里**另写的线性插值** | 强 |
| 吞吐 == Σ(output_tokens) / 墙钟 | 逐请求记录 + 墙钟字段 | 强 |
| `output_tokens` 来源不是"数块数" | **服务端 `timings.predicted_n`** | 强 |
| `prompt_n_computed + cache_hit == prompt_tokens` | 服务端**恒等式** | 强 |
| 每条输入 ≤ 50,000 tokens | **题目规定的上限** | 强 |
| 编程输出可编译可执行 | **真跑一遍** | 最强 |
| 前端有 6 项展示能力 | **题目原文列的 6 项** | 强 |
| 写入路径被 `.gitignore` 覆盖 | `git check-ignore`（外部工具） | 强 |

---

## 四、关键边界样例（题目点名要的 4 个）

`tests/README.md` 原文：

> 围绕关键边界设计样例，如**流式块与 token 的区别、请求失败后的计数、
> 服务退出和有上限的重试**。

| 边界 | 样例怎么造 | 在哪个测试 | 覆盖状态 |
| --- | --- | --- | --- |
| **流式块 vs token** | 用真实记录（块数与 token 数天然不等） | `core/test_02` | ✅ **已覆盖**（90 条里 0 条相等） |
| **请求失败后的计数** | 造失败/超时/取消/截断记录 | `core/test_03` | 🟡 **部分覆盖** —— 计数与分类有断言，但"失败后是否该重试"没有 |
| **服务退出** | `kill` 服务进程 / 服务页点停止 | `test_11` + 实测 | ✅ **已覆盖**（检测 → 自动重启 → 就绪确认 → 恢复后验证，见服务事件时间线） |
| **有上限的重试** | 功能未实现 | — | ❌ **未覆盖** |

**⚠️ 4 条边界里覆盖了 2 条、部分覆盖 1 条、未覆盖 1 条。** 报告里应如实写明：

> 题目要求围绕 4 类关键边界设计样例。
> · **流式块与 token 的区别** —— 已覆盖（`core/test_02`，实测块数与 token 数从无相等）
> · **服务退出** —— 已覆盖。服务页停掉模型服务后，系统在 2~4 秒内检测到、
>   自动拉起（约 2 秒）、确认就绪（核对模型与上下文与注册表一致）、
>   并真发一个请求验证恢复；全过程记进服务事件时间线与运行目录的
>   `service-starts.jsonl`
> · **请求失败后的计数** —— 部分覆盖。`core/test_03` 断言了各类状态的计数
>   与分类完备性（不重不漏），但"失败后重试几次"这件事没有
> · **有上限的重试** —— **未覆盖**，该功能尚未实现

### `test_02` 抓到的实测数字

```
块数 == token 数 :   0 条      ← 从不相等的边界，证明这个边界真实存在
块数 <  token 数 :  81 条      （一个数据块含多个 token）
块数 >  token 数 :   9 条      （token 跨多块 / 含空块）

按块统计会偏 +2.2%
```

**这就是「必须按 token 数统计」的量化依据**（题目原文要求）。

### 其他边界

| 边界 | 说明 | 在哪 |
| --- | --- | --- |
| 单位边界（秒/毫秒） | 差 1000 倍，算错所有指标全偏 | `core/test_03` |
| 除零边界（`output_tokens==1`） | TPOT 分母 `(n-1)` 为 0 | `core/test_03` / `core/test_02` |
| 两种 TPOT 口径 | 排除首 token 与包含首 token 必须不同 | `core/test_03` |
| 上下文档位边界 | 并发 > slot 数（4）时 TTFT 抬高 | `core/test_04` |
| **输出截断** ⭐ | `output_tokens == max_tokens` → 是截断不是能力差 | `core/test_08` |
| 页签角标 | 已实现的页签不该挂"未实现" | `core/test_09` |
| 静态资源路径 | 必须 `/static/` 开头（后端只挂这个前缀） | `core/test_09` |

---

## 五、`test_08` 的实测发现（★ 报告 §8 的关键材料）

题目原文：

> 对**失败输出给出具体样例**，说明其对应用景的影响。

本测试在真实数据上抓到：

```
P03（要求实现 aggregate_by_owner + 至少 5 个测试示例）
    output_tokens = 2048   ← ★ 恰好等于 max_tokens 上限
    输出 7121 字符，代码在 line 173 断在字符串中间
    → py_compile: SyntaxError: unterminated string literal
```

**⚠️ 归因很重要**：

```
❌ 错误归因：「模型写得差」
✅ 正确归因：「输出被 max_tokens=2048 截断」
```

**对应用场景的影响**：编程任务拿到的代码**不能直接运行**；
把 `max_tokens` 提到 4096 或让模型只写函数不写测试即可解决 ——
这是**配置问题**，不是模型能力问题。

---

## 六、所检查的模型输出与提示词编号（可追溯）

题目原文：

> 记录所检查的**模型输出文件及提示词编号**，使结果与**正式测试记录**对应。

| 测试 | 检查的运行 | 检查的提示词编号 | 输出在 |
| --- | --- | --- | --- |
| `core/test_01` | 全部可用运行 | **P01–C02**（10 个） | `results/<run>/prompt-index.json` |
| `core/test_02` | 全部可用运行 | **P01–C02**（10 个） | `results/<run>/requests.jsonl` |
| `core/test_03` | 全部可用运行 | **P01–C02**（10 个） | `results/<run>/requests.jsonl` + `summary.json` |
| `core/test_04` | 全部可用运行 | **P01–C02**（10 个） | `results/<run>/requests.jsonl` |
| `core/test_08` | 全部可用运行 | **P03 / P04 / C01 / C02** | `results/<run>/requests.jsonl` 的 `output_text` |
| `core/test_10` | 被中途停止的运行 | 各自已完成的那些 | `results/<run>/requests.jsonl` + `run-state.json` |
| `test_11` | 不依赖运行数据（连 `/api/service/*`） | — | `results/<run>/service-starts.jsonl` |
| `core/test_00` / `core/test_09` | 不依赖运行数据 | — | 源码与前端文件 |

**运行编号与路径**：见 `results/README.md` 与各次运行目录下的 `meta.json`。
报告引用这些结论时，请标注运行编号，便于回溯原始记录。

---

## 七、测试辅助文件说明

| 文件 | 作用 |
| --- | --- |
| `_testlib.py` | 共用骨架：报告器、读结果、**独立实现的指标算法**（★ 留在 `tests/` 根，别挪进 `core/`） |
| `run_all.sh` | 批量跑，`find` 递归发现两处的测试，汇总退出码 |
| `core/README.md` | 子目录说明（为什么不放 `_testlib`） |

`_testlib.py` 以 `_` 开头，是为了让"按 `test_*.py` 收集"不会把它当测试。

**⚠️ `_testlib.py` 里的 `median_of` / `percentile_linear` 是故意重写的** ——
如果直接调 `src/bench/` 里的函数来验证它自己，测试就失去意义了。

---

## 八、已知限制

| 限制 | 说明 |
| --- | --- |
| 依赖 `results/` 有数据 | 空的 `results/` 只能跑 `core/test_00` / `core/test_09` |
| **`test_11` 需要控制层在跑** | 它调 `/api/service/*`；控制层不在时会跳过并说明，不判失败 |
| 并发覆盖靠跨运行汇总 | 单次小型运行（如 `baseline-c1`）本来就不含多并发，不按它判 |
| 办公输出未自动检测 | 事实准确性与条款引用需人工对照原文（见"原计划里被移除的部分"） |
| `core/test_08` 不做质量判定 | 小模型答不好是常态；它判的是**检查流程本身**能不能跑通 |
