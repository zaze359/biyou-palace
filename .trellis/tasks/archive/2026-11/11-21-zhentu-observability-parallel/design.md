# Design · 任务观测与并行调度（P5）

## 1. 并行调度

### 批次规则

在 `TaskRunner.run` 的主循环中，按计划顺序取当前步骤：

- `parallel_group` 为空或 `exclusive=true`：单步 barrier，行为与 P2 一致。
- `parallel_group=G`：从当前步骤向后扫描，收集**连续**且满足以下条件的同组步骤：
  - `parallel_group == G`
  - `exclusive` 为假
  - `depends_on` 全部已在 `results` 中且状态为 `ok`
  - 数量不超过 `min(plan.max_parallel, MAX_PARALLEL)`

扫描遇到第一个不满足条件的步骤即停止，保持计划顺序的可预测性，避免后序步骤跨越未完成的前序步骤。

### 执行

- 批次内步骤用 `asyncio.gather` 并发执行；批次外仍串行。
- 全局并发用 `asyncio.Semaphore(MAX_PARALLEL)` 约束，跨任务共享；单任务批次大小再受 `plan.max_parallel` 约束。
- 批次完成后统一检查 `cancel_requested`；若取消，未启动步骤写 `step/canceled`。
- deadline 在派发批次前检查；已派发的在途步骤等待其自然结束或单步超时。
- 失败语义不变：`required=true` 失败 → 任务 `failed`；`required=false` → `partial`。依赖失败的下游步骤仍走 `dependency_failed` 分支，不进入批次。

### 顺序保证

- 事件：仍由 `TaskLedger.append_event` 在事务内分配 `seq`，并发完成的事件按实际写入顺序追加，`seq` 单调。
- 结果：`contents` 可能按完成顺序收集，但聚合前按 `aggregate.input_step_ids` 重排，`answer` 顺序不变。
- `sources` 允许按完成顺序排列，契约未要求与计划顺序一致。

### 代码结构

把单步的选路、账本写入、执行、事件抽取为 `TaskRunner._execute_step`，返回：

```python
{"status": "ok"|"failed", "error_code": str|None,
 "source": dict|None, "content": str|None,
 "usage": {"prompt_tokens": int, "completion_tokens": int}}
```

主循环只负责批次划分、依赖失败处理与结果归并。

## 2. 任务指标

指标在 scrape 时由 SQLite 现状直接计算，不做进程内累加，避免重启归零与漂移：

- `TaskStore.counts_by_status()` → `{status: count}`（全表分组）
- 活跃数复用 `count_active_tasks()`

输出：

```text
# TYPE zhentu_tasks_created_total counter
zhentu_tasks_created_total <所有状态之和>
# TYPE zhentu_tasks_terminal_total counter
zhentu_tasks_terminal_total{status="completed"} n
zhentu_tasks_terminal_total{status="partial"} n
zhentu_tasks_terminal_total{status="failed"} n
zhentu_tasks_terminal_total{status="canceled"} n
# TYPE zhentu_tasks_active gauge
zhentu_tasks_active n
```

- 四个终态始终输出（含 0），方便看板稳定查询。
- 只有 `status` 一个标签，不含 task_id / node_id / model / prompt。
- 追加到现有 `/metrics` 文本末尾，不改既有 `zhentu_route_*` 与 `zhentu_nodes_*`。

## 3. 配置

不新增环境变量：

- 全局 step 并发取 `orchestration.MAX_PARALLEL`（8）
- 单任务并发取计划里的 `max_parallel`，校验上限同为 8

## 4. 测试

- 同组 3 步无依赖：执行器记录最大并发 ≥ 2
- 同组 3 步 `max_parallel=2`：最大并发 ≤ 2
- 同组快慢步骤：`answer` 按 `input_step_ids` 顺序
- 指标：创建后 `created_total` 增长，完成后 `terminal_total{status="completed"}` 增长、`active` 归零；运行中 `active=1`
- 回归：required 失败、optional partial、取消、capacity 429 等既有用例全绿

## 5. 回滚

删除批次扫描逻辑即回到 P2 串行执行；任务指标为只读渲染，删除后不影响任务状态。
