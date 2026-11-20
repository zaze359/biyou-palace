# 设计评审：阵图/道果编排边界与节点自治架构

## 结论

**方向正确，但不建议现在 `task.py start`。** 先修 4 个 blocker、补 1 个跨层缺口，并拍板 3 个决策，再进入契约文档实现。

评审对象：`prd.md`、`design.md`、`implement.md`，以及当前 `zhentu/`、节点 health/候选实现。

## 决议（2026-11-20）

- `auto` 默认**本地优先**；集群是显式增强或本地能力不足时的路径。
- 节点本地 v1 固定为**单步 + 本地工具循环**；多步计划后续再做。
- 集群任务/事件流**仅发起节点可读**；管理 token 读全部列为后续可选。

B3、S1（可见性部分）、S7 已关闭；B1、B2、B4、B5 仍需在契约文档阶段落实。

## Blockers

### B1 `Plan` 不可执行：缺少每步的输入与依赖注入规则

`design.md:113-142` 的 `Plan.steps[]` 只有 `goal`、`capability`、`depends_on`、`input_ref`，但执行器无法从中知道：

- 这一步实际发给节点的 `messages` 是什么；
- `depends_on` 的前序结果如何注入当前步骤；
- `goal` 是给模型看的提示词，还是仅给人看的描述；
- `input_ref` 指向什么存储、谁写入。

按当前契约，`ClusterOrchestrator` 无法把 `Plan` 直接交给执行器。建议补：

```json
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
  "output_schema": null
}
```

并明确：

- `depends_on` 的前序输出默认以 tool/user 消息注入，注入模板由 `step.input` 或编排器统一约定；
- `input_ref` 只用于大对象/附件，不替代 `input`；
- `max_steps` / `max_parallel` 有上限，防止计划膨胀。

### B2 任务 API 与事件流不可恢复

`design.md:229-248` 写的是 `POST /zhentu/v1/requests` 返回 `task_id + 事件流`，但没有定义：

- 事件流是同一个 POST 的响应，还是单独的 GET；
- 节点断线后如何从 `Last-Event-ID` 恢复；
- 事件存在哪里、保留多久；
- 任务完成后如何取最终 `ResultEnvelope`。

建议拆成四个明确端点：

```text
POST /zhentu/v1/requests                  -> 202 {task_id, events_url}
GET  /zhentu/v1/requests/{task_id}        -> ResultEnvelope / 当前状态
GET  /zhentu/v1/requests/{task_id}/events -> SSE，支持 Last-Event-ID
POST /zhentu/v1/requests/{task_id}/cancel -> 202
```

事件从 `task_events` 表按 `seq` 读取；SSE 断开不影响 worker。这样节点被杀、重开、换网络都能恢复。

### B3 `auto` 默认行为自相矛盾（已关闭：本地优先）

- `design.md:72-80`：`auto` 按复杂度/能力规则判断，可能走本地或集群。
- 评审时 `implement.md` 风险表曾写“先固定为有阵图且非 local_only 时集群优先”（现已改为本地优先）。

这两条不一致。前者是“按需上云”，后者是“有中枢就全走集群”，会直接改变用户在主入口的延迟、隐私和节点负载。

需要拍板：

- **本地优先**：默认本地，用户显式切换/请求超出本地能力才上集群；
- **集群优先**：只要阵图可用就走集群，本地只做回退。

我倾向本地优先：用户就在节点上，短请求没必要多一跳；集群作为“更强能力”而不是默认路径。

### B4 中枢重启/任务恢复没有定义

设计声称“集群任务不依赖节点进程存活”，但没有定义中枢自身重启后的恢复：

- `running` / `aggregating` 的任务在阵图重启后怎么办？
- 谁持有任务执行权？是否需要 lease/owner？
- 已启动的子任务结果是否已落盘？
- 重启后是继续、标记 interrupted，还是返回 partial？

当前 `zhentu/app.py:165-171` 的 lifespan 只关闭 store；`zhentu/store.py:42-58` 是单连接 + RLock。单进程下可以先做简单策略，但必须在设计里写清楚：

- `tasks` 增加 `lease_owner` / `lease_expires_at` / `started_at` / `finished_at`；
- 启动时扫描非终态任务：有 lease 且未过期 → 等待/拒绝重复执行；过期 → 标记 `interrupted`，按策略恢复或返回 partial；
- 子任务结果必须先落 `task_events`，再做下一步。

否则“任务可恢复”只是口号。

### B5 容量计算漏掉热降载后的有效并发

`zhentu/app.py:464-487` 的 `/candidates` 只返回 `max_concurrency`，没有 `effective_max_concurrency`；而散仙在热降载时会把 `limits.effective_max_concurrency` 压到 1（`nodes/sanxian/shared/src/commonMain/kotlin/com/biyou/sanxian/contract/Models.kt:84`，`NodeApi.kt:449` 也有对应指标）。

`zhentu/route.py:231-236` 的评分逻辑已经优先读 `effective_max_concurrency`，但候选 API 没有把它暴露给编排器。结果是：编排器按 `max_concurrency` 准入，可能把 2 个请求压给一个已经降载到 1 的手机节点。

建议：

- `/candidates` 增加 `effective_max_concurrency`（缺失时回退 `max_concurrency`）；
- 同时暴露 `thermal_shedding` 或 `shed_reason`（可选），供编排器显示/拒绝；
- 编排器容量公式用 `effective_max_concurrency`，不要用 `max_concurrency`。

## Should-fix

### S1 `local_only` 字段重复且权限边界缺失（可见性部分已关闭）

评审时 `RequestEnvelope` 同时有 `privacy` 和 `requirements.local_only`（`design.md:86-104`），没有定义优先级。现已只保留 `privacy` 为权威字段。建议：

- 任一为 `local_only` 即强制本地；
- `mode=cluster` + `local_only` 在节点侧直接拒绝，不发送；
- 阵图收到 `local_only` 返回 400/403。

另外，任务查询/事件流没有定义“谁能看”。至少应限制：

- 发起节点可读自己的任务；
- 其他节点默认不可读；
- 管理 token 可读全部（可选）。

### S2 契约校验与边界值缺失

需要明确：

- `max_steps`（建议 8–12）、`max_parallel`、`deadline_s` 上下限；
- `prompt`/`attachments` 大小上限；
- `capability` 必须是 canonical 能力；
- planner 输出必须过 JSON Schema；不合法时退回单步，不执行原始模型文本；
- `Plan.steps[].id` 唯一、`depends_on` 无环、只能引用已存在步骤。

这属于跨层边界校验，应该在入口/计划生成处做一次，不要在执行器里散落判断。

### S3 `partial` 的终态条件不明确

`design.md:258-274` 说 `completed` 可以携带 `status=partial`，但没有定义：

- 什么条件下可以提前结束并返回 partial；
- 非硬依赖失败后，是否继续等其他分支；
- deadline 到时，已完成/未完成步骤如何标记；
- `partials[]` 里放失败步骤、取消步骤，还是放不完整的结果。

建议明确：所有可运行分支都 settle，或整体 deadline 到期，才进入 `aggregating`；`partials` 记录 `step_id`、`reason`、`output_ref`（如有）。

### S4 超时预算没有分配

`deadline_s` 是整体预算，单步还有 `step_timeout_s`，但没有说：

- 规划阶段预留多少；
- 聚合阶段预留多少；
- 剩余预算如何分给子任务；
- 规划超时后是否直接单步。

建议固定一个简单预算模型，例如：规划 ≤ 15%，聚合预留 ≥ 20%，剩余给执行；任何阶段超时都返回明确的 `timeout_stage`。

### S5 持久化 schema 需要索引、时间和保留策略

当前 `design.md:289-301` 的表缺少：

- `tasks(request_id)` 幂等唯一索引；
- `task_steps` 的 `(task_id, step_id)` 唯一约束；
- `started_at` / `finished_at` / `error_code`；
- `task_events` 的 `(task_id, seq)` 已有，但需要按 `seq` 分页查询；
- prompt/结果属于敏感数据，需要文件权限、保留期、备份策略。

### S6 事件归属要明确

`design.md:184-211` 的 `ResultEnvelope` 由 Aggregator 返回，但 `task/completed` 应该由 `ClusterOrchestrator` 写，不是 Aggregator 写。建议明确：

- Aggregator 只返回 `ResultEnvelope`，不碰任务状态；
- Orchestrator 负责 `aggregate/started`、`aggregate/result`、`task/completed`；
- `task/failed` / `task/canceled` 也只由 Orchestrator 写。

### S7 本地 v1 范围要收窄（已关闭：单步 + 工具循环）

PRD 说本地要“理解/计划/执行/聚合”，但 `implement.md:51-54` 的最小闭环是“单步/工具循环”。这两个范围不完全一致。建议明确 v1：

- 本地 v1 = 单步 + 必要工具调用 + 简单结果整理；
- 本地多步计划列为后续增强；
- UI 明确显示“本地回答”或“集群回答”。

### S8 版本与跨语言契约测试

`schema_version` 只在 body 里，建议同时定义 HTTP 头 `X-Orchestration-Version`；Python/Kotlin 两端至少共享一套 contract fixture/golden JSON，防止 `Plan`/`TaskEvent` 漂移。

## Nits

- `Plan` 示例没有 `exclusive` 字段，但正文提到了。
- `parallel_group` 是“可并行”还是“同组 barrier”需要一句话定义。
- `ResultEnvelope.sources` 在本地模式没有 `route_decision_id`，应允许为空。
- `zhentu_tasks_total{mode}` 建议用“实际执行模式”，不是请求里的 `auto`。
- `attachments` 的结构、大小限制、是否允许 base64 需要明确。
- `deadline_s` 建议有最小/最大值，避免 0 或超大值。

## 需要拍板的决策

1. `auto` 默认：本地优先，还是集群优先？
2. 本地 v1：单步 + 工具循环，还是一次性做多步计划？
3. 任务可见性：仅发起节点可读，还是所有持集群 token 的节点可读？

## 建议

- 先修 B1–B5，再补 S1–S4。
- 修完后更新 `design.md` 和 `implement.md`，再 `task.py start`。
- 本评审不改运行时代码；只改设计/契约文档。