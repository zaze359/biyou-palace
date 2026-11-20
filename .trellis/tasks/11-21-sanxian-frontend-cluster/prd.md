# PRD · 节点前端集群模式（P4）

## 目标

让散仙节点前端具备「本地 / 自动 / 集群」三种模式，用户在同一条对话里既能用本机模型，也能把任务交给阵图：

- `local`：保持现有行为，走本机 `/v1/chat/completions` 流式输出
- `cluster`：显式提交 `POST /zhentu/v1/requests`，订阅 SSE 事件，取消与断线可恢复，失败明确报错
- `auto`：默认本地优先；本地能力不足或未加载模型时提交集群，提交前失败按规则回退本地并标注原因
- 集群任务只在本地保存 `task_id`、最终结果与必要事件摘要；完整事件留在中心
- 前端不规划、不选路、不聚合，不直连推理引擎，不改节点 `/v1/*` 契约

## 范围

### In

- 配置：阵图地址、阵图 token、本节点 id、默认模式（DataStore 持久化）
- `ClusterOrchestrationClient`：提交、SSE 事件流（`Last-Event-ID`）、取结果、取消
- `RequestEnvelope` 构造：有界历史、canonical capabilities、deadline 上下限、`local_only` 拦截
- `ChatViewModel` 集群流程：提交 → 事件驱动状态 → 终态取 `ResultEnvelope` → 渲染答案/来源/partial
- 取消：本地取消 HTTP 请求；集群调用 `/cancel`
- 重连：持久化 `task_id`、`lastEventId`、状态，重新进入应用时恢复
- 错误映射：401/403/404/410/429 与连接失败给出可读提示
- `ChatScreen`：模式选择、集群任务状态条、来源/partial 摘要
- 单测：客户端请求/SSE/410/取消、Envelope 构造、ViewModel 集群成功/失败回退/取消

### Out

- LocalOrchestrator 本地多步计划（P1）
- 节点本地任务账本、本地 `TaskEvent` 输出
- 阵图侧改动（P2/P3/P5 已完成）
- 集群结果的多步明细展开、附件上传
- 跨任务会话记忆；`session_id` 仅审计

## 验收

- 选择 `cluster` 且配置完整时，发送消息会提交阵图并渲染最终答案；事件进度可见
- 阵图不可达 / 401 / 429 / 404 / 410 时给出明确中文提示，不静默降级
- `cluster` 任务提交成功后流中断，可用 `task_id` 恢复并取得最终结果；不重复提交
- 取消进行中的集群任务会调用 `/cancel`，界面进入取消态并最终显示取消结果
- `auto` 在本机无模型时提交集群；提交前失败回退本地，答案标注 `fallback_reason`
- 发送 `local_only` 时不会提交阵图
- 既有本地聊天、模型管理、设置页行为不回归
- `./gradlew :composeApp:testDebugUnitTest` 通过（工具链可用时）
