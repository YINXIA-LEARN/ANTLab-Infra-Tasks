# tools/analysis —— 从结果数据里取数、出证据的脚本

这些脚本**不参与系统运行**，是"事后分析工具"：把 `../../results/` 里的原始
记录变成报告能引用的表格与判定。放在这里而不是 `tests/`，是因为
**它们不判"系统对不对"，只产出证据**（`tests/` 里的测试才做判定）。

## 为什么单独放一层

一开始它们被临时放在 `development/` 根目录下，越堆越多（10 个），
和 `src/`、`tests/`、`configs/` 混在一起看不出层次。
统一收进 `tools/analysis/`，并且**一律用 `Path(__file__)` 定位**，
不再依赖"从哪个目录执行"—— 这样从仓库根、从本目录、
从任何地方调用结果都一样。

---

## 脚本清单

### 出报告数据

| 脚本 | 干什么 | 输出 |
|---|---|---|
| `report_data.py` | 汇总所有运行：① 运行清单与完整性 ② 逐并发档指标表 ③ **单变量对比**（自动算百分比变化） | `results/report-data.txt` |

**它做的两件关键判断**：
- **不信 label** —— 真实配置一律从 `run-config.json` / `meta.json` 读。
  这批运行的 label 曾与配置大面积错配（`kv_baseline_0.6` 实际是 q8_0、
  `kv_opt_1.7` 实际跑的是 0.6B），按 label 配对会把结论搞反。
- **不可对比的运行先剔除** —— 没跑完（`n != 90`）、或跨了服务进程重启
  （目录里有 `service-starts.jsonl`、或 `requests.jsonl` 里出现超过 1 个
  `service_instance_pid`）的运行不参与对比，因为它们的数据是两段混的。

### 判定模型输出

| 脚本 | 干什么 | 输出 |
|---|---|---|
| `evaluate_code_tasks.py` | **编程任务**：提取 markdown 围栏里的代码 → 编译 → import → 真跑边界用例 | `results/code-eval.json` |
| `office_eval.py` | **办公任务**：核对可自动判的事实项（日期仲裁、关键人物、条款编号、格式要求） | `results/office-eval.json` |

**这两个脚本各修掉了一个"检测手段本身的错"，值得单独说**：

1. `evaluate_code_tasks.py` 的来历 —— `tests/core/test_08_output_code.py`
   最初把**整篇输出**当 Python 编译，于是 C01（题目要的是"代码审查报告"，
   输出本来就是说明文字）被报"语法失败"。9 次运行里 6 次是这种**假阳性**。
   正确做法是**只编译围栏里的代码**；没有围栏就如实说"没提取到代码"。
   `test_08` 已按此修正。

2. `office_eval.py` 里**删掉了一条判定** —— 原本想自动判"有没有把旧会议
   决定当成现行安排"。试了两版正则，**两次都误报**：
   ```
   第一版："10月16日…演示" 排除 lookbehind 的"调整为/改为"
           → 误报 "原定 10 月 16 日的客户演示改为 10 月 20 日"
   第二版：加正向条件，只在"通篇无变更措辞"时报
           → 误报 "由原定 10 月 16 日调整为 10 月 20 日"
   ```
   根因：**正确表述里必然要提到旧值**（"由 X 改为 Y"），所以任何
   "出现 X 就算错"的否定模式都会把正确输出判错。
   **结论：这类语义判断不做自动判定**，只保留正向核对
   （现行日期是不是 10/20、有没有交代变更），其余交给人。

### 一次性核查（留档用）

| 脚本 | 核查了什么 |
|---|---|
| `verify_d01_stale.py` | 那 3 条被判"用了旧日期"的 D01 输出，到底是真错还是我们误判 |
| `verify_p03_empty.py` | `ctx16k-q80-06b-r1` 的 P03 空输入返回什么（结论：它把 owner 丢了） |
| `show_d01_verdicts.py` | D01 逐运行的命中/漏项明细 |
| `check_task_kinds.py` | 从 `src/common/tasks.py` 读 `kind` 字段，确认哪些任务真属编程类 |
| `dump_office_outputs.py` | 导出办公任务的模型输出原文，供人工评判 |

这些留着是因为**报告里的每条结论都要能追溯到核查过程** ——
评委看到"某运行某任务有缺陷"时，应该能自己跑一遍复现。

### 维护用

| 脚本 | 干什么 |
|---|---|
| `fix_run_labels.py` | 按**真实配置**重命名运行目录，并同步 `run_id` / 登记表 / label |
| `summarize_new_runs.py` | 早期版本的汇总（已被 `report_data.py` 取代，留作对照） |

---

## 用法

```bash
cd <development>

# 出报告数据
python3 tools/analysis/report_data.py

# 判定模型输出（会写 results/*.json）
python3 tools/analysis/evaluate_code_tasks.py --json results/code-eval.json
python3 tools/analysis/office_eval.py       --json results/office-eval.json

# 重命名运行（默认预演，加 --apply 才真改）
python3 tools/analysis/fix_run_labels.py
python3 tools/analysis/fix_run_labels.py --apply
```

## ⚠️ 注意

- `fix_run_labels.py --apply` 会**移动目录**并改写里面的 `run_id`。
  动之前先 `git status` 确认干净，事后核对 `git diff --stat`。
- 这些脚本**不判定"系统对不对"**，只产出证据。系统正确性由 `tests/` 负责
  （`cd tests && bash run_all.sh`，9 个测试约 4000 项断言）。
