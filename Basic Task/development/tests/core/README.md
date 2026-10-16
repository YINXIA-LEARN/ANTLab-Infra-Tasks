# tests/core —— 功能与数据检查

这里的测试读 `../../results/` 下的原始记录，验证功能与数据是否正确。

**运行方式**：在 `tests/` 目录下 `bash run_all.sh`（会连同上一层的
`test_11_service_watch.py` 一起跑）。单独跑一个：

```bash
cd development/tests
python3 core/test_03_metrics.py
```

⚠️ **`_testlib.py` 故意不放在本目录**，而在上一层的 `tests/`：
它的 `DEV_DIR` / `RESULTS_DIR` 是按自己所在位置向上推算的，
挪进 `core/` 会整体错一层，所有读 `results/` 的测试都会失灵。
本目录的测试用两行 `sys.path.insert` 把上一层也加进来。
