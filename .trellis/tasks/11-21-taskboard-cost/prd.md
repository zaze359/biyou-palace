# 碧游宫 V2 · 任务板与香火账本（因果簿/藏经阁/缓存计量）

## Goal

实现师门任务板与成本账本：任务 DAG、CAS、owner、租约、紧凑视图，以及 token、缓存命中/未命中、香火预算与产物归档。

## Scope

- 本地任务板（SQLite 起步），后续可复制到控制面。
- 任务状态：pending / in_progress / completed / deleted；阻塞状态由 `blocked_by` 推导。
- 任务视图：列表/详情紧凑 JSON，不把整块任务板塞进模型上下文。
- 香火账本：按 `ticket_id`、`step_id`、`agent_id`、`model`、`provider` 归因。
- 藏经阁：产物落盘 + 索引，不把大产物塞进任务记录。

## Requirements

1. 任务字段：`ticket_id`、`title`、`goal`、`tier`、`risk`、`owner`、`blocked_by`、`write_scopes`、`revision`、`lease_expires_at`。
2. 认领与更新必须 CAS：`expected_revision` 不匹配则拒绝。
3. `blocked_by` 未完成时不可认领；`write_scopes` 只做冲突提示，不是锁。
4. 任务结果只追加，不改写历史；保护前缀缓存。
5. 成本字段：`input_tokens`、`output_tokens`、`cache_hit_tokens`、`cache_miss_tokens`、`cost`、`currency`、`price_version`。
6. 拿不到缓存字段时标记 `cache_unknown`，不得按命中价估算。
7. 师门令预算：`max_workers`、`max_input_tokens`、`max_output_tokens`、`max_cost`、`max_wall_time`；80% 预警、100% 暂停待定夺。
8. 任务板与账本 API 需幂等；重复回禀不得重复记账。

## Acceptance Criteria

- [x] 并发更新同一任务时 CAS 生效，旧 revision 被拒绝。
- [x] 阻塞任务不可认领；依赖完成后自动可认领。
- [x] 租约过期任务可被回收/重派。
- [x] 紧凑视图不包含完整历史与大产物。
- [x] 每次模型调用可归因到师门任务与步骤，缓存命中/未命中分开统计。
- [x] 超预算暂停并通知观星台，不静默降级。

## Dependencies

- `11-21-agent-contracts`
- `docs/orchestration-contract.md`

## Out of Scope

- 分布式任务板复制与 leader election（后续高可用任务）。
- 外部 provider 价格自动同步。
