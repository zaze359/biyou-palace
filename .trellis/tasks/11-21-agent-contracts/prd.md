# 碧游宫 V2 · Agent 契约（仙箓/师门任务/师门令/回禀/巡阵）

## Goal

定义 Super-agent 架构下仙徒、任务、授权、回禀与巡阵的协议和状态机，作为桌面端、移动端、任务板与联阵的共同契约。

## Scope

- 仙箓（Agent Card）：身份、根骨、道行、神通、法宝、模型、限额、状态。
- 师门任务：目标、输入、产物、品阶、风险、依赖、预算、截止时间。
- 师门令：范围、允许法宝、数据边界、最大香火/令牌/时长、签发者、有效期。
- 回禀：accepted / progress / waiting / blocked / done / failed 及产物与用量。
- 巡阵：命灯、租约、离线、重派、取消。
- 兼容：既有 `/v1/*` 不变；新能力走独立 `/agent/v1/*` 或扩展编排契约。

## Requirements

1. 仙箓字段至少包含：`agent_id`、`root`（inner/outer）、`cultivation`（0–8）、`skills`、`tools`、`tool_permissions`、`models`、`limits`、`status`、`contract_version`。
2. 师门任务品阶固定八阶：黄、玄、地、天、宇、宙、洪、荒；协议使用 `tier: 1..8`。
3. 风险固定四档：清修、入世、破阵、传道；授权与法宝权限必须匹配。
4. 派差规则：低阶不可越阶；高阶可低接；高阶任务优先，候差不抢占。
5. 回禀必须带 `ticket_id`、`assignment_id`、`status`、`artifacts`、`usage`、`trace_id`。
6. 巡阵以命灯/租约判定离线；离线任务可回收重派；取消为批次边界语义。
7. 所有新增字段必须可选、向后兼容；契约版本独立递增。

## Acceptance Criteria

- [ ] 仙箓、师门任务、师门令、回禀、巡阵均有 JSON Schema 与示例。
- [ ] 状态机覆盖：待派、候差、悬赏、执行中、回禀、验收、失败、取消。
- [ ] 不越阶、可低接、风险与法宝权限匹配均有明确错误码。
- [ ] 与现有 `node-agent-contract.md`、`orchestration-contract.md` 的边界清晰，无重复定义。
- [ ] 文档评审通过，桌面端与移动端可据此实现。

## Dependencies

- `docs/naming.md`
- `docs/orchestration-contract.md`
- `docs/lan-formation-contract.md`

## Out of Scope

- 任务板存储与香火账本实现（见 `11-21-taskboard-cost`）。
- 桌面端、移动端 UI 与运行时实现。
