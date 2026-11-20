# PRD · 任务观测与并行调度（P5）

## 目标

补齐阵图编排的可观测性与并行能力，让灵脉能直接观察任务存量，让 `parallel_group` 真正并发执行：

- `/zhentu/v1/metrics` 增加任务指标：创建总数、按终态分类的完成数、活跃任务 gauge
- 同一 `parallel_group` 且依赖已满足的步骤按 `plan.max_parallel` 并发执行
- 全局并发受统一上限约束，避免多任务同时展开时压垮 NewAPI / 节点
- 事件仍按 `seq` 单调追加，聚合输入仍按 `aggregate.input_step_ids` 顺序
- 取消、deadline、`required` / `partial` 语义与 P2 完全一致

## 范围

### In

- `zhentu/metrics.py`：任务指标渲染函数（无第三方依赖）
- `zhentu/ledger.py`：按状态计数查询；`TERMINAL_STATUSES` 补 `partial`
- `zhentu/runner.py`：`parallel_group` 批次调度、全局信号量、单步执行抽取
- `/zhentu/v1/metrics` 挂载任务指标，不改变既有指标名与既有端点
- 测试：并发同组执行、`max_parallel` 限流、聚合顺序、指标计数、既有 required/partial 回归

### Out

- 多 uvicorn worker / DB lease（维持单进程约束）
- 步骤级重试、优先级、公平调度
- 按节点容量做细粒度准入（仍由路由层决定候选）
- 前端展示（P4）

## 验收

- 同组 3 步（无依赖）并发执行，观测到的最大并发 > 1
- `max_parallel=2` 时同组 3 步的最大并发不超过 2
- 快慢步骤并发时，`answer` 仍按 `aggregate.input_step_ids` 顺序拼接
- `/metrics` 暴露 `zhentu_tasks_created_total`、`zhentu_tasks_terminal_total{status=...}`、`zhentu_tasks_active`，且不泄露 node/model/prompt
- 既有 54 个测试保持通过，新增测试覆盖上述行为
