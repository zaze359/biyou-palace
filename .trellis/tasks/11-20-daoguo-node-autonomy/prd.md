# 阵图/道果编排边界与节点自治架构

## Goal

确定阵图与道果的职责边界、节点本地闭环与集群编排的分层、请求/事件/结果契约，为后续任务编排实现提供设计基线。

本任务解决的核心问题是：**用户始终在节点上发起请求并等待一个结果；节点脱离集群必须能独立完成请求；接入集群时，需求理解、拆分、分发、执行、结果聚合由谁负责。**

## 背景

当前项目已经具备：

- 准圣、散仙两类节点，统一 `/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` 契约；
- 阵图节点注册表、心跳租约、候选查询与单跳 `/route` 决策；
- 灵脉 Prometheus/Grafana 监控；
- 节点本地聊天 UI/CLI 与本地推理闭环。

尚未具备：

- 节点的本地任务编排器（理解 → 计划 → 本地执行 → 聚合）；
- 阵图的集群任务编排（任务账本、DAG、调度、重试、取消）；
- 道果聚合（结果合并、去重、裁决、来源标记）；
- 节点与阵图之间的请求/事件/结果协议。

若直接把编排逻辑放进节点，会导致 Python/Kotlin 两套实现、移动端任务随 App 生命周期中断、节点推理容量被编排占用；若把编排完全放在中心，节点脱离集群后又无法完成用户请求。因此必须先明确边界。

## 已确认的设计决策

1. **节点在本地是完整闭环**：用户输入 → 本地理解/计划（可简化）→ 本地能力/模型选择 → 本地执行 → 本地聚合 → 回复。脱离集群可用。
2. **节点在集群中是执行单元 + 可选集群客户端**：不承担跨节点 DAG、选路、容量准入、集群级聚合。
3. **阵图与道果是一个中心编排服务的两个阶段**，不是两个独立网络服务：阵图负责集群控制面，道果负责集群结果聚合。
4. **本地实现是集群实现的退化版**：同一个 `Orchestrator`/`Aggregator` 契约，两个执行后端（Local / Cluster）。
5. **节点 `/v1/*` 契约不变**：编排是节点客户端/UI 的行为，不改变推理端点的语义；不能让 `/v1/chat/completions` 递归转发到中心。
6. **集群不可达时节点回退本地模式**；`local_only` 请求永不离开节点。
7. **用户只看到一个请求和一个结果**；集群模式的过程事件可以流式展示，但最终结果形状与本地模式一致。
8. **`auto` 默认本地优先**：默认走本地闭环；只有用户显式切到集群、请求超出本地能力（多模态/上下文/能力），或本地不可用时才提交集群。
9. **节点本地 v1 只做单步 + 本地工具循环**：先保证离线可用；本地多步计划/复杂聚合列为后续增强。
10. **集群任务与事件流仅发起节点可读**：其他节点即使持有集群 token 也不能读；管理 token 可读全部的能力列为后续可选。

## Requirements

### R1 节点本地闭环

- 节点必须能在无阵图、无其他节点、无外网的情况下完成一次用户请求。
- 本地闭环至少包含：输入接收、会话管理、模型/能力选择、推理执行、流式输出、取消、超时、错误呈现、结果持久化。
- 本地可以跳过复杂规划：单步直答、少量工具调用、简单结果合并都算合法实现。
- 本地模式不得把“集群编排”作为前置条件。

### R2 集群模式

- 节点前端可以显式选择 `cluster`；`auto` 模式默认本地，只有用户显式切换、请求超出本地能力或本地不可用时才提交集群。
- 集群模式由中心编排服务负责：需求理解、计划、拆分、选路、容量准入、执行、重试、取消、结果聚合。
- 节点只负责提交请求、展示过程事件、取消、断线重连、取回最终结果。
- 集群任务生命周期不依赖发起节点的进程存活；节点退出后任务可继续。

### R3 阵图/道果边界

- 阵图（控制面）负责：任务状态机、DAG/依赖、并行/独占调度、容量准入、`/route` 选路、重试、取消、超时、任务账本。
- 道果（聚合面）负责：聚合策略、聚合 prompt 构造、结果合并/去重/裁决、来源与 partial 标记、最终 `ResultEnvelope`。
- 道果不选节点、不调度子任务、不直连节点、不持有节点凭据。
- 阵图通过 `PlannerClient` 生成计划、`ExecutionClient` 经 NewAPI 执行；阵图只持有 NewAPI 调用凭据，不持有节点 `shared_token`。
- 两者共享同一 SQLite 账本、同一事件 schema、同一进程/部署单元；代码模块分开。

### R4 共享契约

必须定义并评审以下契约：

- `RequestEnvelope`：节点提交给编排器的请求（request_id、session_id、origin_node_id、mode、privacy、deadline、input、requirements）。
- `Plan`：编排器产出的计划（intent、steps、`input`、`depends_on`、`parallel_group`、`required`、`failure_policy`、`exclusive`、aggregate 策略）。
- `TaskEvent`：任务生命周期事件（created/plan/step/aggregate/completed/failed/canceled/interrupted/resumed），带 `seq`/`event_id`/`task_id`。
- `ResultEnvelope`：最终结果（status、answer、sources、partials、usage、mode、error）。

本地与集群必须返回同形状的 `ResultEnvelope`，UI 不按后端分叉。

### R5 回退与失败语义

- 阵图不可达：`auto` 模式回退本地；`cluster` 模式明确报错，不静默降级。
- 本地模型无法完成：明确提示能力不足，或建议切换集群模式；不得伪造结果。
- 计划失败：退回单步直答或最小计划。
- 子任务失败：允许 partial 结果，标记缺失来源；`required=true` 的步骤失败才整体失败。
- 聚合失败：保留子任务结果，单独重试聚合；不重跑推理。
- 用户取消：停止调度新子任务，保留已完成结果。
- 超时：区分单步超时与整体 deadline；返回 partial 或明确错误。
- 幂等：`request_id` + `task_id` 防止重复创建/重复聚合。

### R6 可观测性

- 中心任务账本必须 append-only，可回放；至少记录任务、计划（`plan_json`）、每次尝试、每步输入（`input_json`）、路由决策、聚合结果。
- 节点本地模式至少记录本地会话与任务结果；集群模式记录 `task_id` 与最终结果。
- 指标至少覆盖：任务数、成功/partial/失败、计划步数、子任务成功/失败、聚合耗时、回退次数、本地/集群模式分布。
- 日志与指标不得记录 token、密钥、完整 prompt 原文（按现有脱敏规则）。

### R7 安全与隐私

- 节点到阵图优先使用每节点 token 映射；`X-Node-Id` 与 token 匹配，`origin_node_id` 必须等于鉴权身份。只配置集群级 token 时进入共享信任模式，origin-only 可见性不可强制执行。
- 不得把用户凭据转发给节点或上游。
- `local_only` 在节点侧强制拦截，请求体不发送到中心。
- 阵图不代理推理、不持有节点 `shared_token`；执行仍经碧游宫/NewAPI。
- 未来多用户节点需要用户级身份；本任务先明确为开放问题，不默认实现。
- 集群任务与事件流仅发起节点可读；其他节点默认拒绝，管理 token 读全部列为后续可选。

### R8 部署约束

- 当前规模为单中枢 + 十几台节点；不引入 Redis/Celery/Kafka。
- 中心编排与阵图同进程、同 SQLite；单进程运行。
- 若未来多进程/多 worker，必须引入 DB lease 或独立 worker；本任务只记录约束，不实现。
- 不新增独立道果服务；未来拆分需先定义 `Aggregator` 接口。

## Non-goals

- 不在本任务实现 LocalOrchestrator / ClusterOrchestrator / DaoGuo 的完整运行时代码。
- 不决定是否采用 LangGraph；先把任务账本、契约和调度语义定清楚。
- 不重做节点 UI；只定义前端需要消费的事件与结果。
- 不实现多租户、用户级鉴权、P2P 编排、跨中枢高可用。
- 不修改现有 `/route` 的纯决策语义，不改变节点 `/v1/*` 契约。
- 不把 Trellis 开发工作流与产品运行时编排混在一起。

## Acceptance Criteria

- [ ] `design.md` 明确本地/集群两种模式、组件职责、数据流、状态机、失败矩阵、部署约束。
- [ ] `docs/orchestration-contract.md`（或等价文档）给出四份契约的字段、示例、版本策略。
- [ ] 明确节点 `/v1/*` 不变，编排属于客户端/中心服务，不在推理端点内递归。
- [ ] 明确阵图/道果同服务、分模块的代码落点与禁止事项。
- [ ] `auto` 默认本地优先；集群仅在显式选择、超出本地能力或本地不可用时启用。
- [ ] 明确 PlannerClient / ExecutionClient 的配置、输入输出、失败降级与凭据边界。
- [ ] `Plan` 支持 `required` / `failure_policy`，硬/软依赖可判定。
- [ ] `tasks.plan_json` 与 `task_steps.input_json` 落盘，任务可重放。
- [ ] v1 多轮历史由节点发送有界完整历史，`session_id` 仅审计。
- [ ] 任务 API 支持创建、事件流恢复、结果查询、取消四个动作。
- [ ] `/candidates` 暴露 `effective_max_concurrency`，编排器按热降载后的有效并发准入。
- [ ] 任务/事件流仅发起节点可读。
- [ ] 给出后续实现任务的拆分、依赖顺序与每项的验收标准。
- [ ] 设计结论与 `.trellis/tasks/10-13-biyou-v1-blueprint/prd.md`、`docs/node-agent-contract.md` 不冲突。
- [ ] 所有开放问题要么关闭，要么标记为 deferred 并说明影响。

## Open Questions

- 节点到中心的流式传输用 SSE 还是 WebSocket；SSE 断线重连是否足够覆盖安卓后台场景？
- 节点本地任务账本用 SQLite 还是 JSONL；是否复用现有日志缓冲？
- 多用户节点是否需要用户级 token；本任务先按设备级信任设计是否可接受？
- 道果聚合的默认策略：直接拼接、LLM 润色、还是 judge + 合成？
- `ResultEnvelope.partials` 的结构与 UI 呈现方式。

## References

- `.trellis/tasks/10-13-biyou-v1-blueprint/prd.md`
- `docs/node-agent-contract.md`
- `zhentu/app.py`
- `zhentu/route.py`
- `zhentu/store.py`
- `nodes/quasheng/README.md`
- `nodes/sanxian/README.md`
- `monitoring/README.md`