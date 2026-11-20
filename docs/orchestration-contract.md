# 阵图编排协议（Orchestration Contract）v1.0

> **定位**：这是**编排协议**，定义节点前端、节点本地编排器、阵图集群编排与道果聚合之间的请求、计划、事件、结果形状。它不是节点 `/v1/*` 契约的替代或扩展。
> **实现方**：阵图 `ClusterOrchestrator` / `PlannerClient` / `ExecutionClient` / `DaoGuo Aggregator`；节点 `LocalOrchestrator` 与节点前端。
> **权威设计**：`.trellis/tasks/11-20-daoguo-node-autonomy/design.md` 第 4、6、7 节，以及第 9–11 节的调度、持久化与失败矩阵。本文件只固化协议，不描述内部实现。

## 一、定位与边界

1. **节点 `/v1/*` 契约不变**：`/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` 的语义、响应头与错误形状仍以 [node-agent-contract.md](./node-agent-contract.md) 为准。
2. **阵图不代理推理**：编排器只做计划、选路、调度、聚合；实际推理经碧游宫/NewAPI，最终由节点 `/v1/*` 执行。
3. **阵图不持有节点凭据**：阵图持有的是 **NewAPI 调用凭据**，不持有节点 `shared_token`，也不直连节点。
4. **本地与集群同形状**：`LocalOrchestrator` 与 `ClusterOrchestrator` 返回同一个 `ResultEnvelope` 形状，节点前端不需要两套渲染逻辑。
5. **协议共享，代码不共享**：Python 与 Kotlin 各写各的实现，只共享本契约。
6. **部署约束**：阵图单进程 + SQLite；集群任务不随节点 App 进程存活而丢失。若改为多 uvicorn worker，必须先引入 DB lease 或独立 worker 进程，否则会重复调度同一任务。
7. **模式归属**：`auto` 只存在于节点前端内部；发给阵图的 `RequestEnvelope.mode` 只能是 `local|cluster`。

## 二、组件职责

| 组件 | 职责 | 禁止 |
|---|---|---|
| 节点前端 | 输入、会话、模式选择、事件渲染、取消、重连、本地结果保存 | 不做跨节点规划、不选节点、不聚合集群结果 |
| LocalOrchestrator | 单节点理解/计划、能力选择、执行、局部聚合、本地失败处理 | 不访问其他节点、不依赖阵图 |
| ClusterOrchestrator | 集群任务状态机、DAG、调度、容量、选路、重试、取消、账本 | 不直接拼接最终答案、不持有节点 `shared_token` |
| PlannerClient | 把 `RequestEnvelope` 变成合法 `Plan`；只读能力/模型快照 | 不执行计划、不直接访问节点 |
| ExecutionClient | 经 NewAPI 调用 `/v1/chat/completions` / `/v1/embeddings` | 不直连节点、不转发节点 token |
| DaoGuo Aggregator | 聚合策略、prompt、合并/去重/裁决、来源标记、partial | 不选节点、不调度、不直连节点 |
| 节点推理端点 | 原 `/v1/*` 契约 | 不递归调用阵图、不改变语义 |

### 2.1 PlannerClient

| 配置项 | 含义 |
|---|---|
| `planner.base_url` | 规划模型调用的 OpenAI 兼容 base URL |
| `planner.model` | 规划使用的模型 id |
| `planner.api_key_env` | 规划凭据所在环境变量名，不内联明文 |
| `planner.timeout_s` | 规划独立超时，计入整体 `deadline_s` |
| `planner.max_steps` | 计划步骤上限 |
| `planner.max_parallel` | 计划并行上限 |

- 输入：`RequestEnvelope` + 当前可用能力/模型/limits 快照 + 预算。
  v1 实现在 `RequestEnvelope` 之外由 Orchestrator 注入 `capability_snapshot.available`（能力 → 可用模型列表，
  只含在线且 `ready|degraded` 节点的声明，**不含 endpoint 与凭据**）。规划模型必须只使用快照中存在的能力；
  若仍返回不存在的能力，Orchestrator 会在计划阶段剔除该步骤并记录 `dropped_steps`。
- 快照来自注册表/候选查询，**不触发节点实时探活**，不包含节点 endpoint 或凭据。
- 输出必须是过 `Plan` JSON Schema 的 JSON；非法、超时、无响应时退回单步计划。
- 规划失败不对模型多次重试，直接降级。

### 2.2 ExecutionClient

| 配置项 | 含义 |
|---|---|
| `execution.newapi_base_url` | 碧游宫/NewAPI 的 OpenAI 兼容 base URL |
| `execution.api_key_env` | NewAPI 凭据所在环境变量名 |
| `execution.timeout_s` | 单次执行请求总超时 |
| `execution.connect_timeout_s` | 连接超时 |
| `execution.max_concurrency` | 执行侧全局并发上限 |

- 只调用 NewAPI 的 OpenAI 兼容端点；模型 id 由 `ClusterOrchestrator` 依据 `/zhentu/v1/route` 决策 + `model_hint` 传入，ExecutionClient 只负责发送。
- 不直连节点、不读取节点 `shared_token`。
- 返回标准化 `StepResult`：`status`、`content`/`output_ref`、`usage`、`latency_ms`、`error_code`。

## 三、通用约定

| 项 | 约定 |
|---|---|
| 内容类型 | `application/json`；事件流用 SSE（`text/event-stream`） |
| 版本字段 | envelope 内固定 `schema_version`；事件 schema 变更需兼容读 |
| 鉴权 | `Authorization: Bearer <token>`；目标形态为每节点 token 映射 `ZHENTU_NODE_TOKENS={"sanxian-01":"..."}` |
| 节点身份 | 请求头 `X-Node-Id` 必须与 token 映射一致；`origin_node_id` 必须等于鉴权后的节点 id，不得由 body 自述 |
| 共享信任模式 | 若只配置旧集群级 token，则 origin-only 可见性无法强制执行；必须记录该限制，不得声称是安全边界 |
| 错误格式 | 统一 `{"error": {"code": "...", "message": "...", "type": "orchestration_error", "param": null}}` |
| 幂等 | `request_id` 由节点生成并复用；`(origin_node_id, request_id)` 唯一 |
| 脱敏 | 事件、结果、日志不记录完整 prompt、token、密钥 |

## 四、共享契约

### 4.1 RequestEnvelope

节点前端提交给 `LocalOrchestrator` 或阵图 `POST /zhentu/v1/requests` 的请求体。

```json
{
  "schema_version": "1.0",
  "request_id": "req-01HQ...",
  "session_id": "sess-01HQ...",
  "origin_node_id": "sanxian-01",
  "mode": "cluster",
  "requested_mode": "auto",
  "privacy": "normal",
  "deadline_s": 120,
  "input": {
    "messages": [{"role": "user", "content": "帮我总结这份周报"}],
    "attachments": []
  },
  "requirements": {
    "capabilities": ["chat"],
    "context_len": 8192
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | 是 | 当前 `1.0` |
| `request_id` | string | 是 | 节点生成，幂等键；重试必须复用 |
| `session_id` | string | 否 | 仅关联/审计，不是中心会话主键 |
| `origin_node_id` | string | 是 | 必须与鉴权身份一致 |
| `mode` | `local` \| `cluster` | 是 | 节点前端解析后的实际执行模式 |
| `requested_mode` | `local` \| `cluster` \| `auto` | 否 | 仅审计，记录用户原始选择 |
| `privacy` | `normal` \| `local_only` | 是 | 唯一权威隐私字段 |
| `deadline_s` | integer | 是 | 整体预算，建议 5–600 |
| `input.messages` | array | 是 | OpenAI 风格消息；v1 由节点发送有界完整历史 |
| `input.attachments` | array | 否 | 有大小/数量上限；超限在节点侧拒绝 |
| `requirements.capabilities` | string[] | 是 | canonical 能力：`chat`/`embedding`/`vision`/`ocr`/`rerank` |
| `requirements.context_len` | integer | 否 | 估算值，不是硬保证 |

约束：

- `mode` 是解析后的结果；`auto` 只在节点内部使用。若仍收到 `auto`，阵图按 `cluster` 处理，不两边解释。
- `privacy=local_only`：节点不得发送；`mode=cluster` + `local_only` 在节点侧直接拒绝；阵图收到返回 `400` 或 `403`。
- v1 多轮历史由节点前端放入 `input.messages`（有界、已裁剪/摘要）；阵图不持久化会话正文，也不跨任务取历史。
- `input.messages`、`attachments` 的大小/数量上限在节点侧校验，不发到中心再报错。

### 4.2 Plan

PlannerClient 输出、ClusterOrchestrator 执行的计划。

```json
{
  "plan_id": "plan-01HQ...",
  "intent": "总结周报并给出风险提示",
  "max_steps": 8,
  "max_parallel": 4,
  "steps": [
    {
      "id": "s1",
      "goal": "总结周报要点",
      "capability": "chat",
      "input": {
        "messages": [{"role": "user", "content": "总结以下周报要点：..."}]
      },
      "depends_on": [],
      "parallel_group": "g1",
      "exclusive": false,
      "required": true,
      "failure_policy": "fail_task",
      "model_hint": null,
      "input_ref": null,
      "output_schema": null
    },
    {
      "id": "s2",
      "goal": "基于总结给出风险提示",
      "capability": "chat",
      "input": {
        "messages": [{"role": "user", "content": "基于前一步总结给出风险提示"}]
      },
      "depends_on": ["s1"],
      "parallel_group": null,
      "exclusive": false,
      "required": false,
      "failure_policy": "partial",
      "model_hint": null,
      "input_ref": null,
      "output_schema": null
    }
  ],
  "aggregate": {
    "mode": "llm",
    "input_step_ids": ["s1", "s2"],
    "include_partial": true,
    "instructions": "合并总结与风险提示，保留来源。"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `plan_id` | string | 是 | 计划唯一 id |
| `intent` | string | 否 | 给人看的整体意图描述 |
| `max_steps` | integer | 是 | 有上限，防止计划膨胀 |
| `max_parallel` | integer | 是 | 有上限；实际取 `min(plan.max_parallel, 全局 step 上限, 候选容量)` |
| `steps[].id` | string | 是 | 计划内唯一 |
| `steps[].goal` | string | 是 | 给模型/人看的步骤描述 |
| `steps[].capability` | string | 是 | canonical 能力 |
| `steps[].input.messages` | array | 是 | 执行器实际发送给节点的内容 |
| `steps[].input_ref` | string \| null | 否 | 仅用于大对象/附件，不替代 `input` |
| `steps[].depends_on` | string[] | 否 | 只引用前序步骤，必须无环 |
| `steps[].parallel_group` | string \| null | 否 | 同组且依赖满足的步骤**可以**并行 |
| `steps[].exclusive` | boolean | 否 | 独占 barrier；优先于 `parallel_group` |
| `steps[].required` | boolean | 否 | 默认 `true` |
| `steps[].failure_policy` | `fail_task` \| `partial` | 否 | 与 `required` 配合决定失败后是否继续 |
| `steps[].model_hint` | string \| null | 否 | 建议模型，不保证被选中 |
| `steps[].output_schema` | object \| null | 否 | 结构化输出约束，可为空 |
| `aggregate.mode` | string | 是 | 聚合策略，如 `concat` / `llm` |
| `aggregate.input_step_ids` | string[] | 是 | 聚合器读取哪些步骤结果，顺序按列表 |
| `aggregate.include_partial` | boolean | 否 | 是否包含 partial/failed 的结果引用 |
| `aggregate.instructions` | string | 否 | 聚合提示 |
| `planner_source` | string | 否 | 由 Orchestrator 附加，不来自规划模型；取值见 4.3 约束 |

约束：

- `steps` 的 `id` 唯一；`depends_on` 只能引用前序步骤且无环。
- `input` 是执行器实际发送的内容；`input_ref` 只补充大对象，不替代 `input`。
- `required=true` 的步骤失败会阻断依赖它的下游；`required=false` + `failure_policy=partial` 允许下游继续，最终结果标记 `partial`。
- `depends_on` 只表达顺序/数据依赖，不自动等于必需依赖；必需/可选由 `required` 决定。
- 前序输出按 `depends_on` 声明顺序注入，每条包成固定结构 `{step_id, status, content|output_ref}`；注入位置固定在当前 step 的 user/tool 消息之后，**v1 不允许 planner 自定义模板**。
- `exclusive=true` 的步骤必须独占（模型加载、配置变更、重启等），形成 barrier；`exclusive` 优先于 `parallel_group`。
- planner 输出必须过 JSON Schema；不合法时退回单步，**不执行原始模型文本**。
- `aggregate.input_step_ids` 决定聚合器读哪些结果；默认只含成功步骤，`include_partial=true` 时也包含 partial/failed 的结果引用。聚合器不直接访问节点。
- 本地模式允许退化计划：单步 `s1` 或少量本地工具步骤；**v1 不做本地多步计划**。

### 4.3 TaskEvent

任务事件流元素，用于 SSE 推送与断线重放。

```json
{
  "event_id": "evt-01HQ...",
  "task_id": "task-01HQ...",
  "seq": 3,
  "type": "step/result",
  "at": "2026-11-20T02:30:00+08:00",
  "data": {
    "step_id": "s1",
    "node_id": "quasheng-01",
    "model": "qwen2.5:7b-instruct-q4_K_M",
    "route_decision_id": "dec-01HQ...",
    "status": "ok",
    "output_ref": "artifact://task-01HQ/s1"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event_id` | string | 是 | 去重与 `Last-Event-ID` 恢复 |
| `task_id` | string | 是 | 所属任务 |
| `seq` | integer | 是 | 任务内单调递增 |
| `type` | string | 是 | 见下方事件类型表 |
| `at` | string | 是 | ISO 8601 时间 |
| `data` | object | 是 | 按事件类型定义 |

事件类型：

| 类型 | 写入方 | 说明 |
|---|---|---|
| `task/created` | Orchestrator | 任务创建 |
| `task/planned` | Orchestrator | 计划已校验并持久化；`data` 含 `planner_source` 与可选 `planner_note` / `dropped_steps` |
| `step/started` | Orchestrator | 记录 `input_hash`、`model`、`node_id` |
| `step/result` | Orchestrator | 单步成功 |
| `step/failed` | Orchestrator | 单步失败 |
| `step/canceled` | Orchestrator | 未启动步骤被取消 |
| `step/retrying` | Orchestrator | 单步重试 |
| `aggregate/started` | Orchestrator | 聚合开始 |
| `aggregate/result` | Orchestrator | 聚合完成 |
| `task/completed` | Orchestrator | 任务终态成功/partial |
| `task/failed` | Orchestrator | 任务终态失败 |
| `task/canceled` | Orchestrator | 任务终态取消 |
| `task/interrupted` | Orchestrator | 恢复扫描发现过期 lease |
| `task/resumed` | Orchestrator | 发生恢复执行 |

约束：

- `seq` 任务内单调递增；事件 append-only。
- `event_id` 用于断线重连与去重；客户端用 `Last-Event-ID` 恢复。
- 事件不得包含密钥、完整 prompt 原文；大结果用 `output_ref` 引用。
- `task/completed` / `task/failed` / `task/canceled` 只由 Orchestrator 写入；Aggregator 只返回 `ResultEnvelope`，不碰任务状态。
- `task/interrupted` 只在恢复扫描发现过期 lease 时写入；恢复执行写 `task/resumed`，随后必须写 `task/completed` / `task/failed` / 恢复事件，不能停在 `interrupted`。
- **`task/planned` 必须带 `planner_source`**，取值 `planner`（规划模型产出）、`fallback`（规划失败/未配置，退回单步）、`single_step`（无规划模型）、`invalid_plan`（计划器抛错）。`planner_note` 给出可读原因，`dropped_steps` 列出因能力无人可用被剔除的步骤 id。运维据此判断「为什么只有一步」，不需要翻代码。
- **取消是批次边界语义**：`POST /requests/{id}/cancel` 只置取消位，在途步骤**不会被打断**，在下一个批次边界生效。因此 `task/canceled` 的 `data` 固定带 `cancel_scope="batch_boundary"`，并用 `pending_steps`（未启动步骤数）与 `in_flight_completed`（是否在无在途步骤时取消）描述取消发生的位置；`step/canceled` 只对未启动步骤写入。前端应把取消按钮后的状态显示为「正在停止」，直到收到 `task/canceled`。
- 事件默认保留 30 天或每任务 10,000 条（先到者为准）；超出窗口的 SSE 重放返回 `410 replay_unavailable`。

### 4.4 ResultEnvelope

本地与集群统一的最终结果形状。

```json
{
  "schema_version": "1.0",
  "task_id": "task-01HQ...",
  "status": "completed",
  "terminal_reason": null,
  "mode": "cluster",
  "answer": "...",
  "sources": [
    {
      "step_id": "s1",
      "node_id": "quasheng-01",
      "model": "qwen2.5:7b-instruct-q4_K_M",
      "route_decision_id": "dec-01HQ...",
      "output_ref": "artifact://task-01HQ/s1"
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

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | 是 | 当前 `1.0` |
| `task_id` | string | 是 | 与 `TaskEvent.task_id` 一致 |
| `status` | `completed` \| `partial` \| `failed` \| `canceled` | 是 | 终态 |
| `terminal_reason` | string \| null | 否 | `deadline`、`required_step_failed`、`aggregate_failed`、`user_canceled` 等 |
| `mode` | `local` \| `cluster` | 是 | 实际执行模式 |
| `answer` | string | 否 | 内联结果 |
| `answer_ref` | string | 否 | 大结果引用；与 `answer` 二选一（待定） |
| `sources` | array | 是 | 只列成功产出的步骤 |
| `partials` | array | 是 | 失败/取消/超时步骤及原因，可为空数组 |
| `usage` | object | 是 | token 与步骤计数 |
| `error` | object \| null | 否 | `{code, message, type, param}` |

`status` 语义：

| 取值 | 含义 |
|---|---|
| `completed` | 所有必需步骤成功，聚合成功 |
| `partial` | 至少一个非必需分支失败/超时/取消，或 `deadline_s` 到期但仍有可用结果；**不得把 partial 包装成 completed** |
| `failed` | 必需步骤失败或聚合失败，无可用完整结果 |
| `canceled` | 用户取消 |

约束：

- `sources[].route_decision_id` 在本地模式可为空。
- `partials[]` 必须记录 `step_id`、`reason`、`output_ref`（如有）。
- `error.type` 使用 `orchestration_error`；`error.code` 稳定可枚举，如 `planner_failed`、`aggregate_failed`、`task_capacity_exceeded`、`task_unknown`、`replay_unavailable`。
- 本地与集群必须返回同形状；大结果可用 `answer_ref` 替代内联 `answer`（该项仍为开放问题）。

### 4.5 StepResult

ExecutionClient 返回给 ClusterOrchestrator 的标准化单步结果（不直接透传给节点前端）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `ok` \| `failed` \| `canceled` | 单步状态 |
| `content` | string \| null | 内联输出 |
| `output_ref` | string \| null | 大输出引用 |
| `usage` | object \| null | token 统计 |
| `latency_ms` | integer | 端到端耗时 |
| `error_code` | string \| null | 稳定错误码 |

### 4.6 版本策略

- `schema_version` 固定在 `RequestEnvelope` 与 `ResultEnvelope`；当前 `1.0`。
- 读取方必须兼容旧版本字段缺失：新增可选字段不升 major，删除/改义升 major。
- 破坏性变更流程：先在新版本中同时接受旧字段，观察一个版本后移除；不得在同一 major 内改变字段语义。
- `TaskEvent` 的 `type` 只增不改；新增类型时旧客户端应忽略未知类型。
- Python/Kotlin 两端至少共享一份 golden fixture（`RequestEnvelope`/`Plan`/`TaskEvent`/`ResultEnvelope`），CI 校验两端解析一致。

## 五、数据流

### 5.1 本地模式

```text
节点前端
  → LocalOrchestrator.submit(RequestEnvelope)
  → 本地模型/能力选择
  → 节点 /v1/chat/completions（或 vision/embedding）
  → LocalAggregator
  → ResultEnvelope
  → 节点前端渲染
```

- 本地模式不访问阵图，不要求其他节点。
- 任务账本可简化，但事件仍按 `TaskEvent` 形状输出，保证 UI 一致。
- LocalOrchestrator 只通过 `http://127.0.0.1:<port>/v1/*` 调本机节点，不直接调用 llama.cpp/Ollama 引擎；本地与集群走同一套推理端点。
- v1 工具集只包含 `health`、`models`、`chat`、`vision`（有视觉能力时）、`embeddings`（有 embedding 能力时）；不暴露模型删除、节点重启、配置修改等破坏性工具。
- 工具循环受并发闸门、单步超时、整体 deadline 和取消约束；只读工具不需要审批。
- 本地任务结果落本地账本；`ResultEnvelope.mode=local`，`sources[].route_decision_id` 为空。

### 5.2 集群模式

任务 API 四端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/zhentu/v1/requests` | 创建任务，返回 `202 {task_id, events_url, result_url}`；重复 `request_id` 返回 `200` + 已有 `task_id` |
| GET | `/zhentu/v1/requests/{task_id}/events` | SSE 事件流，支持 `Last-Event-ID` |
| GET | `/zhentu/v1/requests/{task_id}` | 当前状态或最终 `ResultEnvelope` |
| POST | `/zhentu/v1/requests/{task_id}/cancel` | 取消任务，返回 `202`；已终态任务返回 `200` 幂等 |

时序：

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

约束：

- `POST` 不承载长连接；断线重连语义绑在 `task_id` + 事件流上。
- 节点断线不影响任务；节点重连后用 `task_id` 或 `Last-Event-ID` 恢复。
- 任务/事件流仅**发起节点**可读；非发起节点返回 `403`。
- 每一步通过 `/zhentu/v1/candidates` 获取 `active` / `effective_max_concurrency`（缺失时回退 `max_concurrency`）；`thermal_shedding=true` 的节点按有效并发 1 处理。编排器容量准入必须用 `effective_max_concurrency`，不得用 `max_concurrency`。
- 任务级并发 `max_active_tasks` 默认 4；超限返回 `429 task_capacity_exceeded` + `Retry-After`，不排队。

### 5.3 回退

```text
提交前失败（连接失败/鉴权失败/未创建 task）
  → requested_mode=auto：回退 LocalOrchestrator，结果标记 mode=local + fallback_reason
  → requested_mode=cluster：返回明确错误，不静默降级

提交后失败（task_id 已存在、流中断、中枢重启）
  → 不回退本地，避免重复推理
  → 返回 task_id + 恢复入口；任务恢复后继续，或按策略返回 partial
```

判定依据：**`task_id` 是否已创建**。未创建才允许回退；已创建一律走恢复/partial。

### 5.4 HTTP 状态与错误

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

### 5.5 持久化与回放

集群任务落阵图 SQLite（复用 `zhentu/store.py` 的连接与 WAL 配置）。

| 表 | 关键字段 | 约束 |
|---|---|---|
| `tasks` | `task_id`、`request_id`、`request_json`、`plan_json`、`planner_model`、`mode`、`status`、`origin_node_id`、`session_id`、`lease_owner`、`lease_expires_at`、`created_at`、`started_at`、`updated_at`、`finished_at`、`deadline_at`、`result_json`、`error_code` | `UNIQUE(origin_node_id, request_id)` |
| `task_events` | `event_id`、`task_id`、`seq`、`type`、`at`、`data_json` | `UNIQUE(task_id, seq)`，append-only |
| `task_steps` | `task_id`、`step_id`、`goal`、`capability`、`input_json`、`depends_on_json`、`parallel_group`、`exclusive`、`required`、`failure_policy`、`status`、`attempt`、`started_at`、`finished_at`、`error_code`、`output_ref` | `UNIQUE(task_id, step_id)` |
| `route_decisions` | `decision_id`、`task_id`、`step_id`、`request_json`、`selected_json`、`rejected_json`、`created_at` | `INDEX(task_id, step_id)` |

写入规则：

- `plan_json` 保存 planner 校验后的完整计划；`task_steps.input_json` 保存每步实际输入。二者是失败重放与道鉴复盘的数据源。
- `task_events` 为事实来源，`tasks` / `task_steps` 是可重建投影；不要依赖内存状态推进任务。
- 大文本结果存 artifact，事件与结果只存 `output_ref`。
- 账本含 prompt/结果，按敏感数据处理：文件权限 0600，保留期可配置，不默认外传。

回放与恢复：

- `task_events` 默认保留 30 天或每任务 10,000 条（先到者为准）；终态任务保留 `plan_json` + `result_json` 快照。
- SSE 重放只保证保留窗口内；超出窗口返回 `410 replay_unavailable`，节点改用 `GET /requests/{task_id}` 取最终结果。
- 启动扫描非终态任务：lease 过期写 `task/interrupted`；有 `plan_json` 且存在已完成步骤时，从最后一个 completed step 之后恢复，并写 `task/resumed`；否则返回 partial/失败，不得静默丢失。
- 恢复后必须以 `task/completed` / `task/failed` 收口，不能停在 `interrupted`。

### 5.6 失败矩阵

| 失败 | 本地模式 | 集群模式 |
|---|---|---|
| 计划失败 | 退回单步直答；单步也失败则明确错误 | planner 失败/非法 → 单步计划；单步也失败则 `failed` |
| 单步失败 | 可重试则重试；`required` 失败则失败，否则 partial | 可重试错误重试；`required=true` 阻断下游，`required=false` + `partial` 继续 |
| 聚合失败 | 直接拼接/返回原始结果 | 保留子结果，标记 `aggregate_failed`，返回 partial/失败 |
| 节点断线 | 不适用 | 任务继续；节点重连后用 `task_id` / `Last-Event-ID` 取回 |
| 中枢重启（running/aggregating） | 不适用 | 过期 lease 写 `task/interrupted`；有 `plan_json` 则恢复，否则 partial/失败 |
| 阵图不可达 | 本地正常 | `auto` 回退本地；`cluster` 明确报错 |
| 用户取消 | 停止本地生成 | 停止调度新步骤；在途步骤尝试协作取消；未启动写 `step/canceled` |
| deadline 超时 | 停止本地生成并返回已有结果 | 取消下游，保留已完成结果，`terminal_reason=deadline` |
| 重复 `request_id` | 本地幂等 | 返回已有 `task_id`，不重复创建 |
| 多 worker 重复调度 | 单进程，不适用 | 需要 lease；当前单进程约束下不启用多 worker |

## 六、与节点契约的关系

- 本协议不新增、不修改节点入站端点；节点仍只实现 [node-agent-contract.md](./node-agent-contract.md) 的 `/v1/*`。
- 节点到阵图的注册/心跳仍是节点出站协议（`/zhentu/v1/nodes/*`），与本协议的请求提交是两条独立链路。
- 阵图编排器对节点的唯一影响面是经 NewAPI 发起的推理请求；阵图故障不得影响节点本地能力与启动。

## 七、参考

- `.trellis/tasks/11-20-daoguo-node-autonomy/design.md` 第 4、6、7 节
- `.trellis/tasks/11-20-daoguo-node-autonomy/prd.md`
- 共享 fixture：`docs/fixtures/orchestration/envelopes.v1.json`
- [node-agent-contract.md](./node-agent-contract.md)
- `zhentu/app.py`、`zhentu/route.py`、`zhentu/store.py`
