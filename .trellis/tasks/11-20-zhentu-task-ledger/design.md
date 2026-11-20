# Design · 阵图任务账本与集群编排（P2）

契约以 `docs/orchestration-contract.md` 为准，本文件只定实现落点与取舍。

## 1. 模块划分

| 模块 | 职责 |
|---|---|
| `zhentu/ledger.py` | `TaskStore`：四张表、事件 append、任务查询/更新、容量计数 |
| `zhentu/orchestration.py` | `Planner` / `Executor` 协议、`SingleStepPlanner`、`PlannerClient`、`NewApiExecutor`、`validate_plan` |
| `zhentu/runner.py` | `TaskRunner`：状态机、DAG 顺序执行、事件、取消、deadline、聚合 |
| `zhentu/task_models.py` | 创建请求 Pydantic 模型、枚举校验 |
| `zhentu/app.py` | 四个端点、身份解析、注入与 lifespan |

`TaskStore` 与 `RegistryStore` 共用同一 SQLite 文件，各自持有连接；复用 WAL 与 `busy_timeout`。

## 2. 数据模型

### tasks

`task_id` PK；`UNIQUE(origin_node_id, request_id)`；`status` ∈ `queued|running|aggregating|completed|failed|canceled`；保存 `request_json`、`plan_json`、`result_json`、`error_json`、`deadline_at_ms`、`lease_*`、时间戳。

### task_events

`event_id` PK；`UNIQUE(task_id, seq)`；`seq` 在事务内 `MAX(seq)+1`。事件 append-only。

### task_steps

`UNIQUE(task_id, step_id)`；保存计划字段、`input_json`、`status`、`attempt`、`output_ref`、`error_code`。

### route_decisions

每次选路写一行，保存请求、selected、rejected，供审计；不存凭据。

## 3. 身份与鉴权

- `ZHENTU_NODE_TOKENS`（JSON 对象）：每节点 token 映射。
- 任务端点使用 `require_node_identity`：
  - 映射非空：Bearer 必须匹配某个节点 token；`X-Node-Id` 必须一致；`body.origin_node_id` 必须一致。
  - 映射为空：沿用集群级 `ZHENTU_REGISTRY_TOKEN`，身份取 `X-Node-Id`；这是降级模式，不作为安全边界。
- 缺失/不匹配：`401 unauthorized` 或 `403 origin_mismatch`。
- 既有注册端点继续使用 `require_auth`，行为不变。

## 4. 端点

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/zhentu/v1/requests` | 校验 → 幂等查询 → 容量 → 建任务 → `asyncio.create_task` → `202` |
| GET | `/zhentu/v1/requests/{id}/events` | SSE；校验归属；`Last-Event-ID` 之后回放并轮询至终态 |
| GET | `/zhentu/v1/requests/{id}` | 终态返回 `result_json`；否则返回状态摘要 |
| POST | `/zhentu/v1/requests/{id}/cancel` | 终态 `200` 幂等；运行中置取消位；未启动置 `canceled` |

- `local_only` 在入口直接 `403 local_only_rejected`。
- 容量：非终态任务数 ≥ `max_active_tasks` → `429` + `Retry-After: 2`。
- 事件流超出保留窗口 → `410 replay_unavailable`（P2 仅校验未知 `Last-Event-ID`）。
- SSE 帧：`id: <seq>\nevent: <type>\ndata: <json>\n\n`。

## 5. 执行

```text
queued → running → aggregating → completed|failed|canceled
```

- 计划由 `Planner.plan(envelope)` 生成；默认 `SingleStepPlanner`，`PlannerClient` 在配置了 planner 模型时调用 NewAPI，非法 JSON 回退单步。
- `validate_plan`：id 唯一、依赖前序、无环、能力合法、`max_steps`/`max_parallel` 上限。
- P2 按拓扑顺序串行执行；`parallel_group` 记录但不并行（P5）。
- 每步：`/candidates` 风格的 `decide()` 进程内选路 → `route_decisions` → `ExecutionClient.execute()`。
- `required=true` 失败且无替代 → `failed`；`required=false` + `failure_policy=partial` → 记 `partials`，继续。
- `deadline_s` 到期：取消未启动步骤，已完成结果进入聚合，`terminal_reason=deadline`。
- 聚合：按 `aggregate.input_step_ids` 顺序拼接成功内容；无 LLM 润色。
- 任何终态都写 `task/completed|failed|canceled`，并持久化 `result_json`。

## 6. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ZHENTU_NODE_TOKENS` | 空 | `{"sanxian-01":"..."}` |
| `ZHENTU_MAX_ACTIVE_TASKS` | 4 | 同时运行任务上限 |
| `ZHENTU_TASK_EVENT_RETENTION_DAYS` | 30 | 事件保留天数 |
| `ZHENTU_TASK_EVENT_MAX_PER_TASK` | 10000 | 单任务事件上限 |
| `ZHENTU_NEWAPI_BASE_URL` | 空 | 执行 base URL |
| `ZHENTU_NEWAPI_API_KEY` | 空 | 执行凭据，仅环境变量 |
| `ZHENTU_NEWAPI_TIMEOUT_S` | 120 | 单次执行超时 |
| `ZHENTU_PLANNER_MODEL` | 空 | 为空时单步计划 |

配置不自洽（并发 <1、超时 ≤0、node token 为空串）时拒绝启动。

## 7. 测试

- `create_app(..., planner=..., executor=...)` 注入假计划与假执行器。
- 覆盖：创建与结果、幂等、403、取消、429、local_only、必需失败、非必需 partial、事件回放。
- 真实 SSE 用 TestClient 读取完整流；不依赖 uvicorn。

## 8. 回滚

新增模块与表；不改既有端点响应。回滚删除新文件并移除 app.py 中的路由注册即可，DB 新表可保留。
