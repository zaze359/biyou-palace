# 准圣 P1 上游模型动态发现

## Goal

在现有静态 `models[]` 上游桥接之上增加可选 `discover: true`：周期读取上游 `/v1/models`，与白名单取交集后对外暴露；发现失败不得阻塞启动或清空已有列表。不新增任何 HTTP 管理端点，不改变 `visibility`、`<provider>/<model>` 命名、鉴权与密钥规则。

## Requirements

- 配置新增 `discover`（默认 false）与 `discover_interval_s`（默认 300s，下限 60s，低于下限拒绝启动）。
- `discover: true` 时允许 `models[]` 为空：
  - 有白名单：只暴露「发现结果 ∩ 白名单」，能力/`context_len` 沿用白名单项；
  - 无白名单：暴露上游返回的全部模型，能力默认 `chat`、`context_len` 为 null，启动日志 WARNING 明示。
- 启动首轮发现失败只记录、不阻塞启动；后续失败沿用上一次成功快照，不清空列表。
- 首次成功前若配置了白名单则暂按白名单暴露；未配置白名单则暂为空。
- 上游 200 + `data: []` 视为合法快照（替换为空），并打 WARNING。
- 后台仅使用一个 `asyncio` 循环（每上游一个任务），不引入调度依赖。
- `/v1/health` 仅对 `discover: true` 的上游追加 `discover`、`last_discovery_at`、`discovery_error`；未开启时 health 逐字段不变。
- 发现失败日志不包含密钥与 `base_url`；不新增管理端点。

## Acceptance Criteria

- [x] 首轮成功：快照 = 交集，`last_discovery_at` 有值、`discovery_error` 为空。
- [x] 白名单交集：白名单外模型不暴露，白名单内但上游未返回的模型被移除。
- [x] 无白名单：全部发现模型暴露，能力默认 chat。
- [x] 上游 4xx/5xx、连接失败、响应格式异常：保留旧快照，记录错误码与快照时间。
- [x] 空列表：视为成功快照，快照清空且错误为空。
- [x] 失败后恢复：清空 `discovery_error` 并更新时间。
- [x] `discover: false`：模型列表、路由、health 字段与改动前一致。
- [x] 路由只认当前快照；白名单内但未发现的模型返回 404。
- [x] mock 测试及全量 pytest、smoke 通过。

## Notes

- health 的 `available` 仍由探活状态决定，发现失败不会改变节点整体状态机。
- 不持久化快照：进程重启后首次发现失败时按白名单兜底。
