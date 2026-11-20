# 设计：阵图/道果编排边界与节点自治架构

## 1. 结论

采用**双层编排**：

- **节点本地编排（LocalOrchestrator）**：单节点闭环，脱离集群可用。
- **中心集群编排（ClusterOrchestrator）**：阵图内运行，负责跨节点理解、拆分、分发、执行、聚合。
- **道果（Aggregator）**：中心编排的一个阶段，不独立成服务；节点侧存在同一契约的本地退化实现。
- **共享契约**：`RequestEnvelope` / `Plan` / `TaskEvent` / `ResultEnvelope`；协议共享，代码不共享。

用户始终在节点输入、等待一个结果；节点前端选择本地或集群后端，两种后端返回同形状结果。

## 2. 约束

- 节点必须独立可用，不能依赖阵图。
- 节点 `/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` 契约不变。
- 阵图不代理推理、不持有节点 `shared_token`；执行仍经碧游宫/NewAPI。
- 当前部署为单中枢 + 十几台节点；不引入 Redis/Celery/Kafka。
- 中心编排与阵图同进程、同 SQLite；单进程运行。
- 散仙是安卓 App，任务不能随 App 进程被杀而丢失（集群模式）。

## 3. 架构总览

```text
用户在节点输入
   │
   ▼
┌──────────────────────────────┐
│ 节点前端（UI / CLI）          │
│ - 输入、会话、模式选择         │
│ - 事件渲染、取消、重连         │
│ - 本地结果持久化               │
└──────────┬───────────────────┘
           │
     ┌─────┴─────────────────────────┐
     │                               │
     ▼                               ▼
┌──────────────────┐        ┌──────────────────────────┐
│ LocalOrchestrator│        │ POST /zhentu/v1/requests  │
│ （节点内）        │        │ （集群模式，SSE/事件流）    │
│ - 本地理解/计划   │        └──────────┬───────────────┘
│ - 本地能力选择    │                   │
│ - 本地执行        │                   ▼
│ - 本地聚合        │        ┌──────────────────────────┐
└──────────┬───────┘        │ ClusterOrchestrator       │
           │                │ （阵图内）                 │
           ▼                │ - 理解/计划/DAG            │
┌──────────────────┐        │ - 选路/容量/调度           │
│ 节点推理端点      │        │ - 重试/取消/超时           │
│ /v1/chat 等       │        │ - 任务账本                 │
└──────────────────┘        │ - 调用 DaoGuo Aggregator   │
                            └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │ 节点执行（经 NewAPI）      │
                            │ quasheng / sanxian        │
                            └──────────────────────────┘
```

## 4. 组件职责

| 组件 | 职责 | 禁止 |
|---|---|---|
| 节点前端 | 输入、会话、模式选择、事件渲染、取消、重连、本地结果保存 | 不做跨节点规划、不选节点、不聚合集群结果 |
| LocalOrchestrator | 单节点理解/计划、能力选择、执行、局部聚合、本地失败处理 | 不访问其他节点、不依赖阵图 |
| ClusterOrchestrator | 集群任务状态机、DAG、调度、容量、选路、重试、取消、账本 | 不直接拼接最终答案、不持有节点 `shared_token` |
| PlannerClient | 把 RequestEnvelope 变成合法 Plan；只读能力/模型快照 | 不执行计划、不直接访问节点 |
| ExecutionClient | 经 NewAPI 调用 `/v1/chat/completions` / `/v1/embeddings` | 不直连节点、不转发节点 token |
| DaoGuo Aggregator | 聚合策略、prompt、合并/去重/裁决、来源标记、partial | 不选节点、不调度、不直连节点 |
| 节点推理端点 | 原 `/v1/*` 契约 | 不递归调用阵图、不改变语义 |

### 4.1 PlannerClient

- 配置：`planner.base_url`、`planner.model`、`planner.api_key_env`、`planner.timeout_s`、`planner.max_steps`、`planner.max_parallel`。
- 输入：`RequestEnvelope` + 当前可用能力/模型/limits 快照 + 预算；快照来自注册表/候选查询，不触发节点实时探活；不包含节点 endpoint 或凭据。
- 输出：`Plan` JSON；必须过 `Plan` schema，非法/超时/无响应时退回单步计划。
- 规划阶段有独立超时，计入整体 `deadline_s`；规划失败不重试模型多次，直接降级。

### 4.2 ExecutionClient

- 配置：`execution.newapi_base_url`、`execution.api_key_env`、`execution.timeout_s`、`execution.connect_timeout_s`、`execution.max_concurrency`。
- 只调用 NewAPI 的 OpenAI 兼容端点；模型 id 由 ClusterOrchestrator 根据 `/route` 决策 + `model_hint` 传入，ExecutionClient 只负责发送。
- 不直连节点、不读取节点 `shared_token`；阵图持有的是 NewAPI 调用凭据。
- 返回标准化 `StepResult`：`status`、`content`/`output_ref`、`usage`、`latency_ms`、`error_code`。

## 5. 模式选择

| 模式 | 行为 |
|---|---|
| `local` | 节点本地闭环；不访问阵图；`local_only` 默认走这里 |
| `cluster` | 提交阵图；阵图不可达时明确报错，不静默降级 |
| `auto` | 默认本地；仅当用户显式切到集群、请求超出本地能力（多能力/上下文/附件），或本地不可用时提交集群 |

`auto` 初期只用配置和规则判断（如请求需要多能力、上下文超过本地模型、用户显式选择集群），**默认本地优先**，不要用 LLM 做入口分类，避免增加一次模型调用和不确定性。`auto` 只在节点前端内部存在；发给阵图的 `RequestEnvelope.mode` 只能是 `local|cluster`。

## 6. 共享契约

### 6.1 RequestEnvelope

```json
{
  "schema_version": "1.0",
  "request_id": "req-...",
  "session_id": "sess-...",
  "origin_node_id": "sanxian-01",
  "mode": "cluster",
  "requested_mode": "auto",
  "privacy": "normal",
  "deadline_s": 120,
  "input": {
    "messages": [{"role": "user", "content": "..."}],
    "attachments": []
  },
  "requirements": {
    "capabilities": ["chat"],
    "context_len": 8192
  }
}
```

约束：

- `request_id` 由节点生成，作为幂等键；重试必须复用。
- `mode` 只能是 `local|cluster`，是节点前端解析后的结果；`auto` 只在节点内部使用，`requested_mode` 仅用于审计。
- `origin_node_id` 必须与鉴权身份一致；阵图不能信任 body 里的自述。
- `privacy` 是唯一权威字段：`local_only` 时节点不得发送；`mode=cluster` + `local_only` 在节点侧直接拒绝；阵图收到应返回 400/403。
- `deadline_s` 是整体预算，不是单步超时；需有上下限（如 5–600 秒）。
- `requirements.capabilities` 只能使用 canonical 能力；`context_len` 为估算值，不是硬保证。
- v1 多轮历史由节点前端放入 `input.messages`（有界、已裁剪/摘要）；`session_id` 只做关联/审计，阵图不持久化会话正文，也不跨任务取历史。
- `input.messages`、`attachments` 必须有大小/数量上限；超限在节点侧拒绝，不要发到中心再报错。

### 6.2 Plan

```json
{
  "plan_id": "plan-...",
  "intent": "...",
  "max_steps": 8,
  "max_parallel": 4,
  "steps": [
    {
      "id": "s1",
      "goal": "...",
      "capability": "chat",
      "input": {
        "messages": [{"role": "user", "content": "..."}]
      },
      "depends_on": [],
      "parallel_group": "g1",
      "exclusive": false,
      "required": true,
      "failure_policy": "fail_task",
      "model_hint": null,
      "input_ref": null,
      "output_schema": null
    }
  ],
  "aggregate": {
    "mode": "llm",
    "input_step_ids": ["s1"],
    "include_partial": true,
    "instructions": "..."
  }
}
```

约束：

- `steps` 必须有唯一 `id`；`depends_on` 只能引用前序步骤，且必须无环。
- `input` 是执行器实际发送给节点的内容；`input_ref` 只用于大对象/附件，不替代 `input`。
- `required` 默认 `true`；`failure_policy` 取值 `fail_task|partial`。`required=true` 的 step 失败会阻断依赖它的下游；`required=false` + `failure_policy=partial` 允许下游继续，最终结果标记 partial。
- `depends_on` 只表达顺序/数据依赖，不自动等于必需依赖；必需/可选由 `required` 决定。
- `depends_on` 的前序输出按声明顺序注入，每条包成固定结构：`{step_id, status, content|output_ref}`；注入位置固定在当前 step 的 user/tool 消息之后，模板 v1 不允许 planner 自定义。
- `parallel_group` 表示同组且依赖满足的步骤**可以**并行；`exclusive=true` 的步骤必须独占（模型加载、配置变更、重启等），形成 barrier；`exclusive` 优先于 `parallel_group`。
- `max_steps` / `max_parallel` 必须有上限，防止计划膨胀；planner 输出必须过 JSON Schema，不合法时退回单步，不执行原始模型文本。
- `aggregate.input_step_ids` 决定聚合器读哪些步骤结果，顺序按列表；默认只含成功步骤，`include_partial=true` 时也包含 partial/failed 的结果引用。聚合器不直接访问节点。
- 本地模式允许退化计划：单步 `s1` 或少量本地工具步骤；v1 不做本地多步计划。

### 6.3 TaskEvent

```json
{
  "event_id": "evt-...",
  "task_id": "task-...",
  "seq": 3,
  "type": "step/result",
  "at": "2026-11-20T02:30:00+08:00",
  "data": {
    "step_id": "s1",
    "node_id": "quasheng-01",
    "model": "...",
    "route_decision_id": "dec-...",
    "status": "ok",
    "output_ref": "artifact://..."
  }
}
```

事件类型：

- `task/created`
- `task/planned`
- `step/started`
- `step/result`
- `step/failed`
- `step/canceled`
- `step/retrying`
- `aggregate/started`
- `aggregate/result`
- `task/completed`
- `task/failed`
- `task/canceled`
- `task/interrupted`
- `task/resumed`

约束：

- `seq` 单调递增；事件 append-only。
- `event_id` 用于断线重连/去重；客户端可用 `Last-Event-ID` 恢复。
- 事件不得包含密钥、完整 prompt 原文；必要时用 `output_ref` 引用结果。
- `task/completed` / `task/failed` / `task/canceled` 只由 Orchestrator 写入；Aggregator 只返回 `ResultEnvelope`，不碰任务状态。
- `step/started` 记录 `input_hash`、`model`、`node_id`。
- `task/interrupted` 只在恢复扫描发现过期 lease 时写入；恢复执行时写 `task/resumed`，随后必须写 `task/completed`/`task/failed`/恢复事件，不能停在 interrupted。

### 6.4 ResultEnvelope

```json
{
  "schema_version": "1.0",
  "task_id": "task-...",
  "status": "completed",
  "terminal_reason": null,
  "mode": "cluster",
  "answer": "...",
  "sources": [
    {
      "step_id": "s1",
      "node_id": "quasheng-01",
      "model": "...",
      "route_decision_id": "dec-...",
      "output_ref": "artifact://..."
    }
  ],
  "partials": [
    {
      "step_id": "s2",
      "reason": "timeout",
      "output_ref": null
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "steps": 1
  },
  "error": null
}
```

`status` 取值：`completed` | `partial` | `failed` | `canceled`。

约束：

- `terminal_reason` 说明终态原因：`deadline`、`required_step_failed`、`aggregate_failed`、`user_canceled` 等。
- `sources` 只列成功产出的步骤；本地模式 `route_decision_id` 可为空。
- `partials` 记录失败/取消/超时的步骤及原因；不能把 partial 包装成 completed。
- `error` 为 `null` 或 `{code, message, type, param}`；`type` 使用 `orchestration_error`，`code` 稳定可枚举（如 `planner_failed`、`aggregate_failed`、`task_capacity_exceeded`）。
- 本地与集群必须返回同形状；大结果可用 `answer_ref` 替代内联 `answer`（待定）。

## 7. 数据流

### 7.1 本地模式

```text
节点前端
  → LocalOrchestrator.submit(RequestEnvelope)
  → 本地模型/能力选择
  → 节点 /v1/chat/completions（或 vision/embedding）
  → LocalAggregator
  → ResultEnvelope
  → 节点前端渲染
```

本地模式不访问阵图，不要求其他节点。任务账本可以简化，但事件仍按 `TaskEvent` 形状输出，保证 UI 一致。

- LocalOrchestrator 只通过 `http://127.0.0.1:<port>/v1/*` 调本机节点，不直接调用 llama.cpp/Ollama 引擎；本地与集群走同一套推理端点。
- v1 工具集只包含 `health`、`models`、`chat`、`vision`（有视觉能力时）、`embeddings`（有 embedding 能力时）；不暴露模型删除、节点重启、配置修改等破坏性工具。
- 工具循环受并发闸门、单步超时、整体 deadline 和取消约束；只读工具不需要审批。
- 本地任务结果落本地账本；`ResultEnvelope.mode=local`，`sources[].route_decision_id` 为空。

### 7.2 集群模式

```text
节点前端
  → POST /zhentu/v1/requests（RequestEnvelope）
  → 202 { task_id, events_url, result_url }
  → GET /zhentu/v1/requests/{task_id}/events（SSE，支持 Last-Event-ID）
  → ClusterOrchestrator:
       1. 创建任务，写 task/created
       2. PlannerClient 生成 Plan；校验 schema；失败退回单步
       3. 持久化 plan_json，写 task/planned
       4. 拓扑排序，按 parallel_group 调度
       5. 每步调用 /zhentu/v1/route 选路
       6. ExecutionClient 经 NewAPI 调用节点
       7. 写 step/result 或 step/failed
       8. 全部完成/达到 partial 条件 → aggregate/started
       9. DaoGuo Aggregator 生成 ResultEnvelope
      10. Orchestrator 写 task/completed，推送 final
  → GET /zhentu/v1/requests/{task_id} 取最终 ResultEnvelope
  → 节点前端渲染，用户看到最终结果
```

节点断线不影响任务；节点重连后用 `task_id` 或 `Last-Event-ID` 恢复。`POST` 不直接承载长连接，避免断线重连语义绑在一次请求上。

### 7.3 回退

```text
提交前失败（连接失败/鉴权失败/未创建 task）
  → requested_mode=auto：回退 LocalOrchestrator，结果标记 mode=local + fallback_reason
  → requested_mode=cluster：返回明确错误，不静默降级

提交后失败（task_id 已存在、流中断、中枢重启）
  → 不回退本地，避免重复推理
  → 返回 task_id + 恢复入口；任务恢复后继续，或按策略返回 partial
```

### 7.4 HTTP 状态与错误

| 场景 | 状态码 | 说明 |
|---|---|---|
| 创建任务 | `202` | 返回 `task_id`、`events_url`、`result_url` |
| 重复 `request_id` | `200` | 返回已有 `task_id`，不重复创建 |
| 未认证 | `401` | Bearer 缺失/无效 |
| `local_only` 发到阵图 | `403` | 节点侧应已拦截；阵图兜底拒绝 |
| 非发起节点读任务/事件 | `403` | 按鉴权身份校验 |
| 任务不存在 | `404` | `task_unknown` |
| 取消已终态任务 | `200` | 幂等，返回当前状态 |
| 任务容量满 | `429` | `task_capacity_exceeded` + `Retry-After` |
| 规划/聚合失败 | `200` | 在 `ResultEnvelope.error` 中返回，任务已落账本 |
| 事件重放超出保留窗口 | `410` | `replay_unavailable`，让节点取最终结果 |

错误体统一为：

```json
{"error": {"code": "...", "message": "...", "type": "orchestration_error", "param": null}}
```

## 8. 任务状态机

```text
created
  → planning
  → planned
  → running
  → aggregating
  → completed | failed | canceled

running 中任一步失败：
  - 可重试 → 重试（step/retrying）
  - required=true 失败 → 阻断依赖它的下游；无可行分支则 failed
  - required=false + failure_policy=partial → 下游继续，最终 partial
  - 副作用未知 → 不自动重试，标记 unknown/partial
```

`partial` 的终态条件：所有可运行分支都 settle（成功/失败/取消），或整体 `deadline_s` 到期，才进入 `aggregating`；不能在仍有可运行分支时提前宣布 partial。

`completed` 可以携带 `status=partial` 的 `ResultEnvelope`。进入 `aggregating` 后，聚合失败不回退到 running，而是标记 `aggregate_failed` 并返回已有子结果的 partial/失败。

## 9. 调度与容量

- **并行**：`parallel_group` 相同、依赖满足的步骤可并行；使用有界并发池。
- **独占**：模型加载/卸载、配置修改、节点重启等步骤 `exclusive=true`，形成 barrier。
- **容量**：每一步通过 `/zhentu/v1/candidates` 获取 `active` / `effective_max_concurrency`（缺失时回退 `max_concurrency`）；`thermal_shedding=true` 的节点按有效并发 1 处理。满载不排队，缩小 fan-out 或返回可重试错误。
- **任务级并发**：`max_active_tasks`（默认 4）限制同时运行的集群任务；超限返回 `429 task_capacity_exceeded` + `Retry-After`，不排队。`max_parallel` 取 `min(plan.max_parallel, 全局 step 上限, 候选容量)`。
- **超时**：单步 `step_timeout_s` + 整体 `deadline_s`；规划/聚合各有独立超时并从整体预算扣除（建议规划 ≤15%，聚合预留 ≥20%）；超时后取消下游，保留已完成结果。
- **取消**：用户取消 → 停止调度新步骤；已启动步骤尝试协作取消；未启动步骤写 `step/canceled`。
- **重试**：只重试可重试错误（连接失败、5xx、超时）；4xx 不重试；副作用未知的操作不自动重试。

## 10. 持久化与回放

中心 SQLite（复用 `zhentu/store.py` 的连接与 WAL 配置）：

```text
tasks(task_id PK, request_id, request_json, plan_json,
      planner_model, mode, status, origin_node_id, session_id,
      created_at, started_at, updated_at, finished_at, deadline_at,
      lease_owner, lease_expires_at, result_json, error_code)
  UNIQUE(origin_node_id, request_id)

task_events(event_id PK, task_id, seq, type, at, data_json)
  UNIQUE(task_id, seq)
  INDEX(task_id, seq)

task_steps(task_id, step_id, goal, capability, input_json,
           depends_on_json, parallel_group, exclusive, required,
           failure_policy, status, attempt, started_at, finished_at,
           error_code, output_ref)
  UNIQUE(task_id, step_id)

route_decisions(decision_id PK, task_id, step_id, request_json,
                selected_json, rejected_json, created_at)
  INDEX(task_id, step_id)
```

原则：

- `task_events` append-only；`tasks`/`task_steps` 是可重建的投影。
- `(origin_node_id, request_id)` 唯一索引保证幂等；重复提交返回已有 `task_id`。
- `plan_json` 保存 planner 校验后的完整计划；`task_steps.input_json` 保存每步实际输入，支持失败重放与道鉴复盘。
- `lease_owner` / `lease_expires_at` 用于单进程和多进程下的任务所有权；启动扫描非终态任务：过期 lease 写 `task/interrupted`，有 `plan_json` 且已完成步骤可重放时从最后一个 completed step 恢复；否则返回 partial/失败，不能静默丢失。
- `task_events` 默认保留 30 天或每任务 10,000 条（以先到者为准）；终态任务保留 `plan_json` + `result_json` 快照；超出保留窗口的 SSE 重放返回 `410 replay_unavailable`。
- 任务恢复从事件重放；不要依赖内存状态。
- 结果大文本放 artifact/文件，事件只存引用。
- 版本字段 `schema_version` 固定在 envelope；事件 schema 变更需兼容读。
- 任务账本包含 prompt/结果，按敏感数据处理：文件权限 0600、保留期可配置、不默认外传。

节点本地：

- 本地模式：会话与结果落本地（SQLite/JSONL 均可），不要求中心可达。
- 集群模式：本地只存 `task_id`、最终结果、必要事件摘要；完整事件在中心。

## 11. 失败矩阵

| 失败 | 本地模式 | 集群模式 |
|---|---|---|
| 本地模型加载失败 | 明确错误；可切换模型 | 节点侧错误作为 step/failed；可换候选 |
| 本地能力不足 | 提示能力不足/建议集群 | 阵图选路时过滤；无候选返回 no_route |
| 计划失败 | 退回单步直答；单步也失败则明确错误 | planner 失败/非法 → 单步计划；单步也失败则 failed |
| 单步失败 | 重试；required 失败则失败，否则 partial | 可重试则重试；required=true 失败阻断下游，required=false + partial 继续 |
| 聚合失败 | 直接拼接/返回原始结果 | 保留子结果，单独重试聚合 |
| 节点断线 | 本地不适用 | 任务继续；节点重连后取结果 |
| 中枢重启（running/aggregating） | 不适用 | 过期 lease 写 task/interrupted；有 plan_json 则从最后 completed step 恢复，否则 partial/失败 |
| 集群任务中途中枢失败 | 不适用 | 不回退本地；返回 task_id/unknown，恢复后继续或 partial |
| 阵图不可达 | 本地正常 | `auto` 回退本地；`cluster` 报错 |
| 用户取消 | 停止本地生成 | 停止调度，保留已完成结果 |
| 重复 request_id | 本地幂等 | 返回已有 task_id，不重复创建 |
| 多 worker 重复调度 | 本地单进程 | 需要 lease；当前单进程约束 |

## 12. 安全与隐私

- 节点 → 阵图：优先使用每节点 token 映射（如 `ZHENTU_NODE_TOKENS={"sanxian-01":"..."}`）；`X-Node-Id` 与 token 必须匹配，`origin_node_id` 必须等于鉴权后的节点 id。
- 若只配置旧的集群级 token，则进入“共享信任模式”：origin-only 可见性无法强制执行，必须明确记录该限制，不能声称是安全边界。
- token 只在内存/环境变量，不进日志。
- `local_only` 在节点侧强制拦截；阵图收到应返回 400/403。
- 阵图不持有节点 `shared_token`；执行经 NewAPI，阵图只持有 NewAPI 调用凭据。
- 事件与结果脱敏；不记录完整 prompt、token、密钥。
- 多用户节点暂按设备级信任；用户级鉴权列为 deferred。
- 任务查询/事件流按 `origin_node_id` 授权：仅发起节点可读；其他节点默认 403；管理 token 读全部列为后续可选。

## 13. 可观测性

新增指标（Prometheus 命名可后续定）：

- `zhentu_tasks_total{status,mode}`
- `zhentu_task_duration_seconds{mode}`
- `zhentu_task_steps_total{status,capability}`
- `zhentu_task_step_duration_seconds`
- `zhentu_aggregate_duration_seconds`
- `zhentu_fallback_total{from,to,reason}`

日志字段：`task_id`、`step_id`、`decision_id`、`node_id`、`model`、`status`、`reason`。不记录 prompt 原文。

## 14. 部署与进程模型

- 当前：阵图 FastAPI 单进程 + 后台 asyncio worker；SQLite WAL。
- 任务执行不要阻塞 HTTP handler；提交后由 worker 推进。
- 启动：扫描 `tasks` 非终态记录，过期 lease 写 `task/interrupted`，有 `plan_json` 则从最后 completed step 恢复，否则 partial/失败；不能静默丢失。
- 停机：停止接收新任务，等待 worker drain（默认 30s），超时后写 `task/interrupted` 并关闭 store；避免 worker 在 store 关闭后写入。
- 任务级并发：`max_active_tasks` 默认 4；超限 429，不排队；worker 只处理已准入任务。
- 单进程约束：如果以后多 uvicorn worker，必须加 DB lease 或独立 worker 进程；否则重复调度。
- 不引入 Redis/Celery；规模不足。
- 道果不独立部署；未来拆分时定义 `Aggregator` 接口。

## 15. 备选方案

| 方案 | 结论 | 原因 |
|---|---|---|
| 节点承担全部编排 | 拒绝 | Python/Kotlin 重复实现；App 被杀任务丢；节点容量被占 |
| 道果独立服务 | 当前拒绝 | 单请求单结果事务被切两段；多网络跳/多故障域；规模不划算 |
| P2P 编排 | 拒绝（V1） | 复杂度过高；与单中枢蓝图冲突 |
| 中心编排 + 本地退化 | 采用 | 节点自治；中心统一策略；契约一致 |

## 16. 实施阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 本设计 + 契约文档 | `design.md`、`docs/orchestration-contract.md` |
| P1 | 节点本地最小闭环 | LocalOrchestrator 单步/工具循环、本地事件、结果 |
| P2 | 中心任务账本 + 集群编排 | `tasks/task_events/task_steps`、PlannerClient/ExecutionClient、调度器、`/requests` |
| P3 | 道果聚合 | Aggregator、来源/partial、结果 schema |
| P4 | 节点前端集群模式 | 提交、SSE、取消、断线重连、回退 |
| P5 | 观测与硬化 | 指标、超时、容量、故障注入 |

P1 与 P2 可以并行设计，但 P2 的任务账本契约必须先定；P4 依赖 P2/P3 的接口。

P1 的 v1 范围固定为**单步 + 本地工具循环**；本地多步计划、复杂聚合列为后续增强，不阻塞节点离线可用。

## 17. 开放问题

已关闭（2026-11-20）：

- `auto` 默认本地优先；集群是显式增强或本地能力不足时的路径。
- 节点本地 v1 固定为单步 + 本地工具循环；多步计划后续再做。
- 集群任务/事件流仅发起节点可读；用每节点 token 映射绑定身份。
- PlannerClient / ExecutionClient 的配置与职责已定义。
- `required` / `failure_policy` 已定义；硬/软依赖不再靠口头约定。
- `plan_json` / `input_json` 已纳入持久化；任务可重放。
- v1 多轮历史由节点发送有界完整历史，`session_id` 仅审计。

仍开放：

- 节点到中心的流式传输用 SSE 还是 WebSocket；SSE 断线重连是否足够覆盖安卓后台场景？
- 节点本地任务账本用 SQLite 还是 JSONL；是否复用现有日志缓冲？
- 多用户节点是否需要用户级 token；当前按设备级信任是否可接受？
- 道果聚合的默认策略：直接拼接、LLM 润色、还是 judge + 合成？
- `ResultEnvelope.partials` 的 UI 呈现方式与可接受范围。
- `answer_ref` 是否替代内联 `answer`。

## 18. 参考

- `.trellis/tasks/10-13-biyou-v1-blueprint/prd.md`
- `docs/node-agent-contract.md`
- `zhentu/app.py`、`zhentu/route.py`、`zhentu/store.py`
- `nodes/quasheng/README.md`、`nodes/sanxian/README.md`
- `monitoring/README.md`