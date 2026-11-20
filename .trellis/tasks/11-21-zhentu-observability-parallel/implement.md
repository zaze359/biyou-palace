# Implement · 任务观测与并行调度（P5）

- [x] `zhentu/ledger.py`：`TERMINAL_STATUSES` 补 `partial`；新增 `counts_by_status()`
- [x] `zhentu/metrics.py`：新增任务指标渲染（created / terminal by status / active）
- [x] `zhentu/app.py`：`/zhentu/v1/metrics` 追加任务指标
- [x] `zhentu/runner.py`：抽取 `_execute_step`；同组批次并发；全局 `Semaphore`
- [x] 测试：并发同组、`max_parallel` 限流、聚合顺序、任务指标、既有回归
- [x] `pytest zhentu/tests` 全绿（60 passed）
- [x] `git diff --check` 通过
