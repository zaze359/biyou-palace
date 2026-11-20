# 三轮评审：`docs/orchestration-contract.md`

## 结论

契约可评审通过。与 `design.md` 第 4、6、7、9–11 节逐项对照后，未发现与设计冲突的协议级 blocker；评审发现 2 处表述缺口，已在契约内修正。

共享 fixture 已同时接入 Python 与 Kotlin 解析测试，两端对四类契约的理解一致。

## 评审依据

- `design.md`：组件职责、Plan/RequestEnvelope/TaskEvent/ResultEnvelope、数据流、调度容量、持久化回放、失败矩阵。
- 现有实现：`zhentu/app.py` 的 `/candidates`、`/route` 响应形状；`zhentu/store.py` 的 SQLite/WAL 约束。
- `docs/node-agent-contract.md`：节点 `/v1/*` 契约边界。
- 共享 fixture：`docs/fixtures/orchestration/envelopes.v1.json`。

## 检查项

| 检查项 | 结论 |
|---|---|
| 文档定位为编排协议，不替代节点 `/v1/*` | 通过 |
| `RequestEnvelope` 字段、`mode`/`requested_mode`/`privacy` 语义 | 通过 |
| `Plan` 含 `input`、`required`、`failure_policy`、`depends_on` 注入规则 | 通过 |
| `TaskEvent` 类型表、`seq`、`event_id`、保留与重放 | 通过 |
| `ResultEnvelope` 四种 `status` 与 `partials`/`sources` | 修正后通过 |
| 本地/集群结果同形状 | 通过 |
| 四端点与 HTTP 状态码 | 通过 |
| `local_only`、可见性 403、阵图不持节点凭据 | 通过 |
| `effective_max_concurrency` 与热降载准入 | 通过（已实现并测试） |
| `plan_json` / `input_json` 持久化与恢复 | 通过 |
| 失败矩阵覆盖九类场景 | 通过 |
| 单进程/多 worker 约束 | 通过 |
| Python/Kotlin fixture 解析一致 | 通过 |

## 评审发现与修正

### F1 `partial` 未覆盖 deadline（已修正）

原表述只覆盖“非必需分支失败”，但设计第 8 节明确 `deadline_s` 到期也可进入 `partial`。已把 `partial` 定义改为：

> 至少一个非必需分支失败/超时/取消，或 `deadline_s` 到期但仍有可用结果。

### F2 `tasks` 表缺 `updated_at`（已修正）

设计第 10 节的 `tasks` 字段含 `updated_at`，契约表漏列。已补齐，避免实现时对账本字段产生分歧。

## 未阻塞的开放项

| 项 | 处理 |
|---|---|
| `answer_ref` 与内联 `answer` 的阈值 | 保持开放，实现时按结果大小定；契约已声明“二选一（待定）” |
| fixture 只覆盖 `step/result` 一种事件 | v1 可接受；其余事件类型由类型表约束，后续可扩充 fixture |
| Kotlin 侧目前只有协议模型，未被运行时引用 | 属 P1/P2 实现范围，不影响本契约评审 |

## 验证

- `zhentu/tests`：**42 passed**（含 fixture Python 侧 4 项校验）
- Kotlin `OrchestrationFixtureTest`：**4 passed**
- `task.py validate 11-20-daoguo-node-autonomy`：通过
- `git diff --check`：通过
