# Implement · 节点前端集群模式（P4）

- [x] 配置：`NodeSettingsValues` 增加 `zhentuBaseUrl` / `zhentuToken` / `originNodeId` / `defaultMode`
- [x] `NodeConfigStore` 新增四个键与读写；`NodeSettings.android.kt` 映射
- [x] 设置页新增「集群」字段与校验；仅集群字段变化时不重启服务
- [x] 新增 `RequestEnvelopeBuilder`（commonMain，纯函数）
- [x] 新增 `ClusterOrchestrationClient` 接口 + Ktor 实现（submit/SSE/result/cancel）
- [x] `ChatViewModel`：模式状态、集群提交流程、事件渲染、取消、恢复、auto 回退
- [x] 集群任务持久化：`task_id`、`lastEventId`、status、最终答案摘要
- [x] `ChatScreen`：模式选择行、集群任务状态条、来源/partial 摘要
- [x] 单测：客户端、Envelope、ViewModel（成功/失败/回退/取消）
- [x] `./gradlew :composeApp:testDebugUnitTest` 通过（23 passed）
- [x] `./gradlew :composeApp:assembleDebug` 通过
- [x] `git diff --check` 通过
