# 准圣 P1 回退策略（manual | auto）

## Goal

在现有单节点上游桥接之上增加显式回退：默认 `manual` 只提示候选、不切换；`auto` 按 `fallback_chain` 顺序兜底「首字节前」的失败。不新增 HTTP 管理端点，不改动既有端点语义、鉴权、密钥处理、`visibility` 与 `queue_len` 策略。

## Requirements

- 全局配置：`fallback_mode: manual | auto`（默认 manual）、`fallback_chain: [<provider>/<model> | local/<model>]`。
- `local` 为保留前缀，上游 id 不得命名为 `local`。
- manual：上游错误原样返回；仅可回退类失败在 `error.fallback_candidates` 追加调用方可见候选，不含 base_url/密钥。
- auto：仅连接失败、超时、5xx 触发；4xx（含 401/403/429）一律不兜；每个候选最多一次，按链条顺序；跳过原模型、不可见候选与重复目标。
- 流式首字节后中断不回退，保持 SSE error 帧且不发 `[DONE]`。
- 成功回退：响应头 `X-Node-Fallback-Source: <实际条目>`，并在 `/v1/logs` 记录 `fallback` 事件。
- 耗尽：返回最后一次失败状态，`error.code=fallback_exhausted`，`error.fallback_attempts` 列出候选与脱敏原因。
- 启动校验：本地条目需在 `/api/tags` 且声明 chat；远程条目需在静态清单或发现快照且声明 chat，否则拒绝启动并指出条目。发现失败且无快照时记录 WARNING 后放行。
- 未配置链条时行为与现状完全一致。

## Acceptance Criteria

- [x] manual 5xx 不切换且带候选提示；无链条时错误体不变。
- [x] auto 5xx 兜底本地成功，响应头与 `/v1/logs` 均有标记。
- [x] 4xx / 401 不回退。
- [x] 首字节后中断只发 SSE error 帧，不发 `[DONE]`，不触发回退。
- [x] 链条耗尽错误包含候选顺序与各自原因。
- [x] 启动期引用缺失本地/远程模型被拒并点名条目。
- [x] mock 测试、全量 pytest、smoke 通过。

## Notes

- 回退仅作用于 `/v1/chat/completions`；embeddings 仍不桥接。
- 跨节点按能力查询候选留待阵图提供接口，本阶段不实现。
