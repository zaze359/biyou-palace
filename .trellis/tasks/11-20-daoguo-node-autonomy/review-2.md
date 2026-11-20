# 二轮评审：`design.md` 修订版

## 结论

修订版把上一轮的 5 个 blocker 在**设计层面**都覆盖了：Plan 有 input、任务 API 拆开、`auto` 本地优先、lease/恢复有位置、容量改用 `effective_max_concurrency`。

评审时按“能否直接照着写实现”的标准，仍有 **5 个实现级 blocker**；这些已在修订版中处理，见下节。建议继续修设计，不要先写契约文档，否则契约会把未定义的语义固化。

## 修订（2026-11-20）

B6–B10 已在 `design.md` 修订：

- 增加 PlannerClient / ExecutionClient 配置与职责，明确阵图只持 NewAPI 凭据。
- `Plan.steps[]` 增加 `required` / `failure_policy`，硬/软依赖可判定。
- 增加每节点 token 映射与 `origin_node_id` 绑定；共享 token 模式明确降级。
- `tasks.plan_json` / `task_steps.input_json` 纳入持久化。
- v1 多轮历史由节点发送有界完整历史，`session_id` 仅审计。
- 另补：HTTP 状态码表、任务级并发、事件保留、聚合输入、依赖注入结构。

以下 B6–B10 文字保留为评审记录；实现前以修订后的 `design.md` 为准。

## 已确认改善

- `Plan` 增加 `input.messages`、`exclusive`、`max_steps`、`max_parallel`，并写明 planner 输出必须过 JSON Schema。
- 任务 API 改为 `POST` 202 + `GET events` + `GET result` + `POST cancel`，支持 `Last-Event-ID`。
- `auto` 明确本地优先；本地 v1 明确单步 + 工具循环。
- `tasks` 增加 `request_id UNIQUE`、`lease_owner`/`lease_expires_at`、`started_at`/`finished_at`。
- 容量改用 `effective_max_concurrency`，热降载按 1 处理。
- 任务可见性明确“仅发起节点可读”。

## 仍然阻塞实现的 5 个问题

### B6 没有定义“谁来做计划、谁去调 NewAPI”

`design.md:62-70` 把“理解/计划”放在 ClusterOrchestrator，`design.md:254-276` 写“经 NewAPI 调用节点”，但没有定义：

- planner 用哪个模型、哪个 provider、哪个 API；
- planner 的输入是什么（只有 RequestEnvelope，还是还要带可用节点/模型/能力/上下文预算）；
- planner 的 JSON Schema 在哪里维护、失败如何降级；
- 阵图调用 NewAPI 的 base URL、API key、超时、重试、并发限制从哪里来；
- 阵图是否因此持有 NewAPI 凭据，和“阵图不持有节点凭据”的边界怎么区分。

这不是实现细节，是 ClusterOrchestrator 能不能启动的前提。至少要在设计里增加：

```text
PlannerClient:
  provider/model/base_url/api_key_env
  timeout_s
  max_steps / max_parallel
  response_schema
  fallback: single_step

ExecutionClient:
  newapi_base_url
  api_key_env
  connect_timeout_s / timeout_s
  max_concurrency
  model_id mapping
```

并明确：阵图持有的是 **NewAPI 调用凭据**，不是节点 `shared_token`。

### B7 “硬依赖”没有字段，失败矩阵无法执行

`design.md:353-368` 的失败矩阵反复说“硬依赖失败 → failed”“非硬依赖 → partial”，但 `Plan.steps[]`（`design.md:113-151`）只有 `depends_on`，没有说明某条依赖是必需还是可选。

执行器无法判断一个 step 失败后是继续还是终止。建议给每个 step 增加：

```json
{
  "required": true,
  "failure_policy": "fail_task | partial | retry"
}
```

并明确：

- `required=true` 的 step 失败 → 依赖它的下游不可运行；
- `required=false` 的 step 失败 → 下游仍可运行，最终 `partial`；
- `depends_on` 只表达顺序/数据依赖，不自动等于硬依赖；
- `task_steps` 表增加 `required` / `failure_policy` 字段。

### B8 “仅发起节点可读”在当前鉴权模型下不可强制执行

`design.md:370-377` 说任务/事件流按 `origin_node_id` 授权，但当前阵图只有一个集群级 token：`zhentu/config.py:9-10` 的 `ZHENTU_REGISTRY_TOKEN`，`zhentu/app.py:183-192` 的 `require_auth` 只校验这一个 token。

这意味着任何持有集群 token 的节点都可以：

- 伪造 `origin_node_id` 提交任务；
- 用别人的 `task_id` 读事件流/结果。

“仅发起节点可读”在没有节点身份绑定的情况下只是 UI 约定，不是安全边界。需要至少选一个：

1. 每节点独立 token（注册时绑定 node_id）；
2. 节点请求签名（node_id + timestamp + HMAC）；
3. 阵图侧维护 `node_id -> token` 映射，拒绝 body/header 不一致。

否则应把这条降级为“默认 UI 隔离，不是安全保证”，并明确写入设计。

### B9 `Plan` 没有落盘，任务无法真正回放

`design.md:314-345` 的 `tasks` 表只有 `request_json`，`task_steps` 只有 `goal/capability/depends_on/...`，没有保存：

- 完整的 `Plan`（尤其是每步 `input.messages`）；
- planner 的原始输出/校验结果；
- 每步实际发送给节点的 payload 摘要；
- 聚合器的输入集合。

这样“从事件重放任务”只能恢复状态，不能恢复**当时到底发了什么**。建议：

- `tasks` 增加 `plan_json`；
- `task_steps` 增加 `input_json`（或 `input_ref`）；
- `task_events` 的 `step/started` 记录 `input_hash` 与 `model`；
- 聚合事件记录 `input_step_ids`。

否则道鉴复盘、失败重放、契约测试都没有可靠数据源。

### B10 多轮会话/历史语义没有定义

`RequestEnvelope` 有 `session_id`，`input.messages` 只给当前一轮（`design.md:84-111`）；但设计没有说明：

- 节点是否把完整多轮历史放进 `input.messages`；
- 阵图是否保存/读取会话历史；
- `session_id` 是仅用于审计，还是会话主键；
- 本地模式和集群模式对同一 `session_id` 的历史是否一致；
- 上下文超限时由谁裁剪/摘要。

当前写法会导致同一个用户第二轮请求丢失上下文，或者节点/中心各存一份历史、行为不一致。建议明确：

- v1 由节点前端发送**有界完整历史**，阵图不持久化会话正文；
- `session_id` 只用于关联/审计，不作为中心会话存储；
- 超限由节点前端或 planner 统一裁剪，规则写进契约。

## Should-fix

### S9 `RequestEnvelope.mode` 仍可能是 `auto`

`design.md:92` 的示例仍是 `"mode": "auto"`，但 `design.md:72-80` 说 `auto` 是节点前端的选择。建议明确：

- `auto` 只在节点前端内部使用；
- 发给阵图的 `RequestEnvelope.mode` 只能是 `local|cluster`；
- 若保留 `auto`，阵图必须定义“收到 auto 一律按 cluster”的规则，不能两边都解释。

### S10 回退语义在“提交前”和“提交后”混在一起

`design.md:277-283` 说“cluster 请求失败 → auto 回退本地”；`design.md:363-364` 又说“中途中枢失败不回退本地”。需要拆开：

- **提交前失败**（连接失败、鉴权失败、未创建 task）：`auto` 可以回退本地；
- **提交后失败**（task 已创建、流中断、hub 重启）：不回退本地，返回 `task_id` 并允许恢复/partial。

建议在 API 层用 `task_id` 是否存在来区分，并写进失败矩阵。

### S11 本地编排器走 HTTP 还是直连引擎，没有说清

`design.md:240-252` 画的是 LocalOrchestrator → 节点 `/v1/chat/completions`，但没写是 HTTP 调用 localhost，还是直接调 `LlmEngine`。项目原则是“本地/集群走同一套端点”，应明确：

- LocalOrchestrator 只通过 `http://127.0.0.1:<port>/v1/*` 调本机；
- 不直接调用 llama.cpp/Ollama 引擎；
- 工具循环的工具集、超时、取消、权限边界列出；
- 危险工具（模型删除/重启）默认不暴露给本地工具循环。

### S12 只有 step 级容量，没有 task 级并发/背压

`design.md:305-312` 定义了单步容量，`design.md:392-400` 没有定义同时跑多少任务。单进程下如果无限接任务，会打满 worker、SQLite 和模型并发。建议增加：

- `max_active_tasks`（例如 4）；
- 超限返回 `429 task_capacity_exceeded` + `Retry-After`；
- 或明确“任务排队”，但要给出队列上限和丢弃策略；
- 任务级并发与 step 级 `max_parallel` 的关系。

### S13 HTTP 状态码/错误语义缺失

设计只说 `POST` 202，没有定义：

- 重复 `request_id` → 200 + 已有 `task_id`，还是 409？
- `local_only` 发到阵图 → 400 还是 403？
- `task_id` 不存在 → 404；
- 非发起节点读任务 → 403；
- 取消已终态任务 → 200 幂等，还是 409；
- 任务容量满 → 429；
- planner 失败/聚合失败的错误体结构。

建议加一张状态码表和统一错误体（可沿用 OpenAI 风格或定义 orchestration error）。

### S14 事件保留/重放没有边界

`task_events` append-only 且用于 `Last-Event-ID` 重放，但没有说保留多久、是否压缩、事件被清理后如何恢复。建议：

- 明确保留期/最大事件数；
- 终态任务保留快照（`result_json` + `plan_json`），事件可归档；
- SSE 重放只保证保留窗口内；
- 超出窗口返回 410 或 `replay_unavailable`，让节点取最终结果。

### S15 聚合器输入没有契约

`Plan.aggregate` 只有 `mode` + `instructions`（`design.md:153-166`），没有定义聚合器拿到哪些 step 结果、顺序、是否包含失败/partial。建议：

```json
{
  "mode": "llm",
  "input_step_ids": ["s1", "s2"],
  "include_partial": true,
  "instructions": "..."
}
```

并明确聚合器只读 `ResultEnvelope`/step 结果，不直接访问节点。

### S16 `depends_on` 的输出注入仍是“约定”

`design.md:145-151` 说“默认以追加 user/tool 消息注入”，但没有模板/占位符/顺序。至少定义：

- 注入顺序按 `depends_on` 声明顺序；
- 每条前序结果包成固定结构（如 `{step_id, status, content}`）；
- 超长结果如何截断/引用 `output_ref`；
- 是否允许 planner 自定义注入模板。

## Nits

- `Plan.steps[]` 的 `depends_on` 拼写应统一；当前有一处 `depends_on`，正文也有 `depends_on`，检查是否一致。
- `task_steps` 表名与 `task_steps`/`task_steps` 统一。
- `TaskEvent` 类型没有 `task/interrupted`，但恢复策略用了 `interrupted`；要么加事件，要么明确只是内部状态。
- `ResultEnvelope.error` 没有 schema。
- `answer_ref` 仍是“待定”，契约文档阶段要定。
- `schema_version` 与 HTTP header 的关系未定。

## 建议

1. 先补 B6（Planner/ExecutionClient）、B7（required/failure_policy）、B8（节点身份）、B9（plan 落盘）、B10（多轮历史）。
2. 再补 S9–S16 中会影响契约字段的部分。
3. 更新 `design.md` 后，再写 `docs/orchestration-contract.md`；否则契约会固化未定义的语义。

本轮只评审，未改设计/契约。