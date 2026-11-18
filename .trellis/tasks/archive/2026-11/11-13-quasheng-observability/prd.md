# 准圣可观测性收口

## Goal

在不改变现有端点语义、鉴权与脱敏规则的前提下，提供 Prometheus 指标与结构化日志落盘，并保证两者都不会成为新的故障源。

## Requirements

- `GET /metrics`：请求量（端点/方法/状态码）、模型请求量（模型/上游/状态码）、首字节与整体耗时直方图、在途请求与闸门排队、发现成功/失败、回退次数与来源、上游可用状态。
- `/metrics` 与业务共用 bearer 鉴权；只读缓存状态，不主动触发上游探活；采集/渲染异常不得影响业务。
- 日志：可选 `log_file` 落 JSONL，按 `log_max_bytes` 轮转、`log_backup_count` 保留；沿用 token/绝对路径脱敏；写盘失败降级为内存并在环里记 WARNING。
- `GET /v1/logs` 语义不变，仍只读内存环形缓冲。

## Acceptance

- [x] `/metrics` 返回上述指标，真实进程验证 200。
- [x] 本地 chat / 流式 / embeddings 均产生模型、耗时、首字节指标。
- [x] 日志文件写入、轮转与保留份数符合配置；失败自动降级。
- [x] 指标与日志中无 token、上游地址。
- [x] pytest 180 passed，smoke 18/18。
