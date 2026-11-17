# 阵图编排（P3）：单跳选路与准圣消费

## 背景

- P1 已落地阵图注册表（`zhentu/`，端口 4020，SQLite 租约/能力视图）。
- P2 已落地准圣 `fallback_source=registry`：查询 `GET /zhentu/v1/candidates` 得到候选模型，执行仍走 NewAPI。
- 现有候选查询只做「状态 + 队列长度」粗排，缺少负载、新鲜度、已加载、热状态、角色偏好的统一评分，也没有可审计的决策标识。

## 目标

1. 阵图新增只读决策接口 `POST /zhentu/v1/route`，返回排序后的 `selected` + `alternates` + `rejected`，并带 `decision_id` 与 `ttl_ms`。
2. 准圣在 `fallback_source=registry` 下消费 `/route` 的 `selected` + `alternates`，映射回 `newapi/<model>` 后仍经 NewAPI 执行。
3. 默认行为完全不变：`fallback_source=static` 不调用 `/route`；阵图不可用时准圣回退本地静态链条。

## 范围

- 包含：P3.0（阵图 `/route`、评分、过滤、单测）与 P3.1（准圣消费 `/route`、回退成功标记 `decision_id`、单测/真实验证）。
- 不包含：多步编排（P4 `/plan`）、阵图代数据面转发、反馈/熔断闭环、散仙补 `active` 字段（P3.2）、NewAPI 改造。

## 约束

- 阵图仍是控制面：不持有节点/上游凭据，不代理推理请求，不感知 NewAPI 渠道表。
- `GET /zhentu/v1/candidates` 及注册/心跳/注销协议保持兼容，不改既有响应形状。
- 准圣不新增入站端点，不改变契约错误形状；`/route` 失败只影响本次候选来源，不影响本地推理/health。
- 节点侧不向阵图提交 prompt、响应、API key 或节点凭据。
- `/route` 只做单次请求的单跳决策，决策不落库，仅计算与日志审计。
- 权重按策略预设固定在阵图侧；逐请求只允许选择 `strategy`，不开放自定义权重。

## 验收标准

1. `/route` 对相同输入多次调用返回完全一致的排序（确定性）。
2. 缺 `active`、缺 `max_concurrency`、缺温度/电量时仍返回 200 且结果合理，不 500。
3. 无候选时返回 200 + `selected=null` + `reason=no_route` + `rejected`，不返回 5xx。
4. `/route` 参数错误返回既有 OpenAI 错误形状：`invalid_capability` / `invalid_strategy` / `too_many_models`（400），鉴权失败 401。
5. 准圣 `fallback_source=registry` 时消费 `/route`，回退成功响应带 `X-Node-Fallback-Source`、`X-Node-Fallback-Origin: registry`、`X-Node-Route-Decision`；`/v1/logs` 的 fallback 事件含 `decision_id` 与 `node_id`。
6. 停掉阵图或 `/route` 超时/报错时，准圣在该请求上回退本地静态链条，本地 chat/embeddings/health 不受影响。
7. `fallback_source=static` 时准圣不产生任何阵图请求。
8. 阵图日志与 `/route` 响应中不出现任何上游 key、节点 token 或 prompt。
9. `zhentu` 与 `nodes/quasheng` 现有测试全部通过，并新增 `/route` 评分/过滤/边界与准圣消费测试。
