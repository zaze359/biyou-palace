# 实施计划：阵图/道果编排边界与节点自治架构

## 范围

本任务只产出**设计基线与契约文档**，不实现 LocalOrchestrator / ClusterOrchestrator / DaoGuo 的运行时。

B5 附带一处只读字段补充：`/zhentu/v1/candidates` 暴露 `effective_max_concurrency`（含回归测试），不涉及编排运行时。

实现阶段拆为后续子任务；本任务的完成标准是：设计可评审、契约可落地、后续任务边界清楚。

## 前置条件

- 设计已与用户对齐：节点本地完整闭环、集群编排中心化、阵图/道果同服务分阶段。
- 现有节点 `/v1/*` 契约保持不变。
- 现有阵图 `/route` 保持纯决策语义。

## 执行清单

### 1. 评审设计

- [x] 通读 `prd.md`、`design.md`。
- [x] 已确认 `auto` 默认本地优先；集群仅在显式选择、超出本地能力或本地不可用时启用。
- [x] 确认 `local_only`、`deadline_s`、`mode` 的语义。
- [x] 确认 `ResultEnvelope` 的 `status` 取值与 `partial` 表达。
- [x] 确认阵图/道果同进程、分模块的代码落点。

评审通过后再 `task.py start`。

### 1.1 先关闭评审 blocker

在写契约文档前，逐项确认 `review.md` 的 blocker：

- [x] B1：`Plan.steps[]` 增加 `input.messages`、`exclusive`、`max_steps`、`max_parallel`，并定义 `depends_on` 输出注入规则。
- [x] B2：任务 API 拆为 `POST /requests`（202）、`GET /requests/{id}/events`（SSE + Last-Event-ID）、`GET /requests/{id}`、`POST /requests/{id}/cancel`。
- [x] B3：`auto` 默认本地优先；显式集群或超出本地能力才上集群。
- [x] B4：定义中枢重启后的 lease/恢复策略，禁止非终态任务静默丢失。
- [x] B5：`/candidates` 增加 `effective_max_concurrency`；编排器按热降载后的有效并发准入。
- [x] B6：定义 PlannerClient / ExecutionClient 的配置、输入输出、失败降级与凭据边界。
- [x] B7：`Plan.steps[]` 增加 `required` / `failure_policy`，硬/软依赖可判定。
- [x] B8：节点身份绑定（每节点 token 映射或签名），`origin_node_id` 不由 body 自述。
- [x] B9：`tasks.plan_json` / `task_steps.input_json` 落盘，任务可重放。
- [x] B10：v1 多轮历史由节点发送有界完整历史，`session_id` 仅审计。

### 2. 编写持久契约文档

新增 `docs/orchestration-contract.md`（与 `docs/node-agent-contract.md` 平级），内容来自 `design.md` 第 4、6、7 节：

- [x] 文档定位：编排协议，不属于节点 `/v1/*` 契约。
- [x] `RequestEnvelope` 字段表 + 示例。
- [x] `Plan` 字段表 + 示例。
- [x] `TaskEvent` 类型表 + 示例 + `seq`/`event_id` 规则。
- [x] `ResultEnvelope` 字段表 + 示例 + `status` 语义。
- [x] 版本策略：`schema_version`、兼容读、破坏性变更流程。
- [x] 明确节点 `/v1/*` 不变，编排由节点前端/阵图服务承担。
- [x] 明确 `local_only` 不离开节点；`mode=cluster + local_only` 在节点侧拒绝。
- [x] 明确阵图不代理推理、不持有节点凭据。
- [x] 明确任务/事件流仅发起节点可读；非发起节点返回 403。
- [x] 明确 `/candidates` 暴露 `effective_max_concurrency`，编排器按有效并发准入。
- [x] 明确任务 API 四端点：POST 创建、GET 事件流、GET 结果、POST 取消。
- [x] 明确 PlannerClient / ExecutionClient 配置与凭据边界；阵图只持 NewAPI 凭据。
- [x] `Plan` 字段表包含 `required` / `failure_policy` / `input` / `aggregate.input_step_ids`。
- [x] 明确 `tasks.plan_json` / `task_steps.input_json` 的写入与重放规则。
- [x] 明确 HTTP 状态码：202/200/400/401/403/404/410/429。
- [x] 明确 v1 多轮历史由节点发送有界完整历史，`session_id` 仅审计。

### 3. 更新蓝图引用

- [x] 在 `.trellis/tasks/10-13-biyou-v1-blueprint/prd.md` 第三步附近记录本设计任务的分支/结论。
- [x] 在 `README.md` 的参考文档或架构段落加一行编排协议链接（如果该文档已存在）。
- [x] 不修改 `docs/node-agent-contract.md` 的现有端点与版本语义；如需引用，只加“编排协议另见”一句。

### 4. 拆分后续实现任务

在本任务下创建（或在本文件列出待创建）子任务，建议顺序：

1. **节点本地最小闭环**
   - LocalOrchestrator 单步/工具循环
   - v1 不做本地多步计划；多步计划列为后续增强
   - 本地事件流、取消、超时、结果落盘
   - 验收：断网、无阵图时能完成一次请求
2. **阵图任务账本 + 集群编排**
   - `tasks` / `task_events` / `task_steps` / `route_decisions`
   - `/zhentu/v1/requests`、事件流、取消、幂等
   - 验收：一次请求可拆分并落账本，可回放
3. **道果聚合**
   - Aggregator 接口、拼接/LLM 润色策略
   - 来源、partial、失败降级
   - 验收：子任务失败时仍可返回 partial
4. **节点前端集群模式**
   - 提交、SSE、取消、断线重连、回退本地
   - 验收：节点退出后任务继续，重开可取回结果
5. **观测与硬化**
   - 指标、超时、容量、故障注入
   - 验收：任务指标与告警可见

依赖顺序：1 与 2 可并行设计，2 的账本契约先定；3 依赖 2；4 依赖 2/3；5 最后。

### 5. 验证

文档任务：

```bash
git diff --check
python3 ./.trellis/scripts/task.py validate 11-20-daoguo-node-autonomy
test -f docs/orchestration-contract.md
```

契约评审：

- [x] 四份契约都有字段表、示例、版本策略。
- [x] 本地/集群结果形状一致。
- [x] 失败矩阵覆盖：计划失败、单步失败、聚合失败、断线、阵图不可达、取消、超时、重复请求。
- [x] 明确节点 `/v1/*` 不变。
- [x] 明确单进程/多 worker 约束。
- [x] Python/Kotlin 共享至少一份 `RequestEnvelope`/`Plan`/`TaskEvent`/`ResultEnvelope` fixture，验证两端解析一致。
  - fixture：`docs/fixtures/orchestration/envelopes.v1.json`；Python `test_orchestration_fixtures.py` 4 项、Kotlin `OrchestrationFixtureTest` 4 项均通过。
- [x] 任务可见性：非发起节点读任务/事件流返回 403。
- [x] 热降载节点：`effective_max_concurrency=1` 时编排器不会按 `max_concurrency=2` 准入（`/candidates` 字段与回归测试已补）。

### 6. 回滚

- 本任务以文档为主；B5 附带 `/zhentu/v1/candidates` 只读字段补充与回归测试，回滚 = revert 对应提交。
- 不涉及数据库迁移、不改节点代码、不实现编排运行时。

## 完成标准

- [x] `prd.md`、`design.md` 已评审。
- [x] `docs/orchestration-contract.md` 已创建并通过评审（见 `review-3.md`）。
- [x] 蓝图 PRD 已引用本设计。
- [x] 后续实现任务已列出，依赖顺序明确。
- [x] `task.py validate` 通过（上下文清单已填充）。
- [x] B5 附带代码变更已单独说明并测试（`zhentu/tests` 42 passed，Kotlin fixture 4 passed）。

## 风险与回退点

| 风险 | 处理 |
|---|---|
| `auto` 模式行为不明确 | 已定：默认本地优先；显式集群或超出本地能力才上集群 |
| 本地/集群契约漂移 | 同一份 `docs/orchestration-contract.md` + 契约测试 |
| 中心单进程成为瓶颈 | 先单进程；多 worker 前加 DB lease/独立 worker |
| 道果后续变重 | 预留 `Aggregator` 接口，不在本任务拆服务 |
| 节点 UI 改动范围膨胀 | 本任务只定义事件/结果，不改 UI |

## 参考

- `prd.md`
- `design.md`
- `docs/node-agent-contract.md`
- `.trellis/tasks/10-13-biyou-v1-blueprint/prd.md`