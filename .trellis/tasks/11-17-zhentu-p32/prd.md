# 阵图 P3.2：散仙 active、阵图指标与契约 v1.2

## 目标

1. 散仙 `/v1/health` 上报可选 `resources.active`（真实在途请求数），`queue_len` 回归契约语义（真实排队数，满载立即 429 的节点恒为 0）。
2. 阵图新增无依赖 Prometheus 文本指标端点，覆盖决策总数、拒绝原因与决策耗时。
3. 契约文档升级为 v1.2（`Contract-Version` 响应头仍为 `1.0`），README 同步。

## 约束

- 散仙仅做最小改动：字段模型、健康采集与对应测试，不改聊天/嵌入/降载逻辑。
- 新增字段可选，旧阵图忽略即可；契约响应头版本不升级。
- 指标不引入第三方依赖，不记录 node_id/model 等高基数标签，不泄漏 endpoint/token/prompt。
- 指标端点沿用 Bearer 鉴权，不为监控开鉴权例外。

## 验收

1. 散仙健康响应含 `active`，`queue_len` 为 0；并发满载时 `active=1`。
2. `GET /zhentu/v1/metrics` 在鉴权后返回 `zhentu_route_total`、`zhentu_route_rejected_total`、`zhentu_route_duration_seconds`，无候选决策计入 `no_route`。
3. 指标文本不含节点、模型、endpoint、token。
4. 契约文档标注 v1.2 且明确 `Contract-Version` 仍为 `1.0`。
5. 阵图与准圣现有测试全绿；散仙相关 Kotlin 测试通过（可运行范围内）。
