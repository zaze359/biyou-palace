# Design · 节点前端集群模式（P4）

## 1. 配置

扩展 `NodeSettingsValues`：

| 字段 | 含义 | 默认 |
|---|---|---|
| `zhentuBaseUrl` | 阵图根地址，如 `http://192.168.1.10:8100`；空=未配置 | 空 |
| `zhentuToken` | 阵图 Bearer token | 空 |
| `originNodeId` | 本节点身份，需与阵图 `node_tokens` 匹配 | 取自 `NodeConfig.nodeId` |
| `defaultMode` | `local` / `auto` / `cluster` | `local` |

持久化在 `NodeConfigStore` 新增四个 DataStore 键；读取时 `originNodeId` 为空回退 `NodeConfig.nodeId`。
集群配置只影响前端，不参与节点服务启动；设置页保存时若只有集群字段变化，不重启前台服务。

## 2. 客户端

新增 `commonMain/ui/ClusterOrchestrationClient.kt`：

```kotlin
data class ZhentuEndpoint(val baseUrl: String, val token: String?)

data class ClusterSubmit(taskId: String, eventsUrl: String, resultUrl: String, duplicate: Boolean)

interface ClusterOrchestrationClient {
    suspend fun submit(endpoint: ZhentuEndpoint, envelope: RequestEnvelope): ClusterSubmit
    suspend fun streamEvents(
        endpoint: ZhentuEndpoint,
        eventsUrl: String,
        lastEventId: String?,
        onEvent: suspend (TaskEvent) -> Unit,
    )
    suspend fun getResult(endpoint: ZhentuEndpoint, resultUrl: String): ResultEnvelope
    suspend fun cancel(endpoint: ZhentuEndpoint, taskId: String)
    fun close()
}
```

- 路径按 `zhentu/v1` 拼接；`events_url` / `result_url` 是相对路径时与 `baseUrl` 拼接。
- `submit` 接受 `202` 与幂等 `200`（`duplicate=true`）；其余 4xx 抛 `ClusterException(status, code, message)`。
- SSE 解析 `id:`、`event:`、`data:`；事件体用 `TaskEvent` 反序列化，回调真实事件。
- `Last-Event-ID` 使用**事件体里的 `event_id`**（契约 4.3），而不是 SSE 的 `id:` 数字 seq。
- `410 replay_unavailable` 抛 `ClusterException(410, ...)`，由上层改为直接取最终结果。
- `cancel` 使用 `POST /requests/{task_id}/cancel`；已终态返回 200，按幂等处理。

## 3. 请求构造

`RequestEnvelopeBuilder`（纯函数，可单测）：

- `schema_version="1.0"`、`request_id="req-"+随机`、`origin_node_id=配置`、`mode="cluster"`、
  `requested_mode=用户模式`、`privacy="normal"`、`deadline_s` 夹在 5..600。
- `input.messages`：系统提示 + 最近 `MAX_HISTORY=20` 条非空消息，单条内容截断到 8000 字符。
- `requirements.capabilities`：默认 `["chat"]`；后续有附件时再扩展，v1 不发送附件。
- `privacy=local_only` 时构建函数直接拒绝（抛 `IllegalArgumentException`）。

## 4. ViewModel 流程

`ChatUiState` 增加：

- `mode: ChatMode`
- `clusterTask: ClusterTaskUiState?`（taskId、status、doneSteps、totalHint、lastEventType、error）
- `clusterConfigured: Boolean`

`onSend()` 分支：

1. `local`：现有路径。
2. `cluster`：必须已配置阵图，否则报错；提交失败 → 明确错误，不回退。
3. `auto`：
   - 未配置阵图 → 本地。
   - 本机有模型 → 本地。
   - 本机无模型 → 提交集群；**提交前失败**（连接/401/429/其他未创建 task）→ 回退本地，
     助手消息标注 `[回退本地] 原因：...`；本地也无模型则同时报出两条原因。

集群流程：

```text
submit → 持久化 task_id/status
streamEvents（携带 lastEventId，逐事件持久化）
  step/started → 记录模型/节点
  step/result / step/failed → 计数
  aggregate/started → 聚合中
  task/completed|failed|canceled → 终止
getResult → 渲染 answer / sources / partials
持久化 terminal 状态与最终答案摘要
```

- 提交后失败（已拿到 `task_id`）**不回退本地**；界面保留 `task_id` 与「恢复」入口。
- `onStop()`：本地取消 job；集群调用 `cancel`，等待 `task/canceled` 或直接终止。
- 初始化时若持久化任务未终态且已配置阵图，自动恢复订阅。

## 5. 渲染

- 集群任务使用一条 assistant 占位消息：进行中显示阶段文本，终态替换为答案。
- 答案后追加来源摘要：`来源：s1 @ node/model`；partial 追加 `未完成：s2（reason）`。
- `task/started` 等事件只更新状态条，不写入消息正文，避免刷屏。

## 6. UI

`ChatScreen` 顶部新增模式行（`FilterChip`：本地 / 自动 / 集群）；未配置阵图时集群 chip 可点但发送时报错提示。
集群任务状态条展示：`task_id` 短码、状态、已完成步数、最近事件、取消按钮。

## 7. 测试

- `ClusterOrchestrationClientTest`（MockEngine）：提交路径/鉴权/body、202 与 200 幂等、SSE 多事件、410、取消、取结果。
- `RequestEnvelopeBuilderTest`：字段、capabilities、deadline 夹取、历史截断、local_only 拒绝。
- `ChatViewModelTest` 扩展：集群成功渲染答案与来源、cluster 提交失败不回退、auto 无模型提交集群、
  auto 提交前失败回退本地、取消调用 cancel。
- 运行 `./gradlew :composeApp:testDebugUnitTest`。

## 8. 回滚

模式选择与集群客户端为增量路径；默认 `local` 时行为与 P3 完全一致，删除集群分支即可回退。
