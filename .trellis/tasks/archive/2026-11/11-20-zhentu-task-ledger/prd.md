# PRD · 阵图任务账本与集群编排（P2）

## 背景

`docs/orchestration-contract.md` 已定义请求、计划、事件、结果契约，但阵图目前只有节点注册、候选与单跳选路，没有任务账本与集群编排。节点前端无法提交任务、订阅事件或取回结果。

## 目标

在阵图内实现 P2 最小闭环：

- SQLite 账本：`tasks` / `task_events` / `task_steps` / `route_decisions`
- 四个任务端点：创建、事件流、结果、取消
- 单步与多步计划执行（计划可注入；默认单步降级）
- 任务幂等、发起节点可见性、取消、deadline、部分失败
- Planner / Executor 可注入，测试不依赖真实 NewAPI 与节点

## 范围

### In

- `POST /zhentu/v1/requests`：`202` 创建，重复 `request_id` 返回 `200`
- `GET /zhentu/v1/requests/{task_id}/events`：SSE，支持 `Last-Event-ID`
- `GET /zhentu/v1/requests/{task_id}`：运行中返回状态，终态返回 `ResultEnvelope`
- `POST /zhentu/v1/requests/{task_id}/cancel`：取消，幂等
- 账本持久化与恢复所需字段；事件 append-only
- 每节点 token 映射（`ZHENTU_NODE_TOKENS`）与 `origin_node_id` 绑定；共享 token 模式降级但记录
- 任务级并发上限与 `429 task_capacity_exceeded`
- 单步计划默认；多步计划按 `depends_on` 顺序执行，`required`/`failure_policy` 生效

### Out

- 道果 LLM 润色（P3）
- 节点前端 UI（P4）
- 并行组调度、观测指标、多 worker lease（P5）
- 模型加载/卸载、节点重启等工具调用
- `embedding` 执行（P2 仅 `chat`；遇到其他能力返回明确失败）

## 验收

- 一次 chat 请求可创建任务、落账本、跑完并取回 `ResultEnvelope`
- 相同 `request_id` 重复提交不重复执行
- 非发起节点读任务或事件返回 `403`
- 事件流可用 `Last-Event-ID` 续传；任务终态后流关闭
- 取消后任务终态为 `canceled`，未启动步骤写 `step/canceled`
- 必需步骤失败 → `failed`；非必需步骤失败 → `partial`
- 无 NewAPI 配置时任务失败为 `execution_unavailable`，不影响其他端点
- `pytest zhentu/tests` 全绿

## 风险

| 风险 | 处理 |
|---|---|
| 后台任务随进程退出丢失 | 账本持久化；P5 加 lease/恢复扫描 |
| 改写 response 形状影响现有端点 | 新端点独立实现，既有端点不动 |
| 真实 NewAPI 不可用 | Executor 可注入；配置缺失明确失败 |
| 任务长期占用 | `deadline_s` + `max_active_tasks` |
