# 准圣节点健壮性巡检与修复

## Goal

对 `nodes/quasheng` 做一次只读巡检，定位并在**不改变现有契约**的前提下修复可复现的健壮性缺陷：流式资源释放、回退结果标记、上游探测阻塞、异常指标缺口、配置解析失败路径、CLI 网络异常路径。

## Background

准圣节点已具备并发闸门、流式透传、上游桥接、动态发现、回退与阵图注册。现有测试 202 项全部通过，但巡检发现若干异常路径未被覆盖，且部分失败路径会留下持久副作用或误导性观测数据。

## Requirements

### R1 流式资源释放必须无条件归还名额

- 无论关闭上游流（`AsyncExitStack.aclose()`）是否抛异常，全局并发闸门与上游并发名额都必须归还。
- 覆盖本地流式与远程流式两条路径，以及流建立失败、上游非 2xx、客户端断开、流中途异常等分支。

### R2 回退成功标记只对 2xx 生效

- `auto` 回退链路中，某候选若返回非 2xx（例如上游并发满载 `429 upstream_capacity`），不得被视为“回退成功”。
- 具体表现必须满足：不附加 `X-Node-Fallback-Source` / `X-Node-Fallback-Origin` / `X-Node-Route-Decision`，不写“已回退到”日志，不累加 `quasheng_fallback_total`。
- 非 2xx 响应体与状态码保持原样。

### R3 上游探测不能串行阻塞 health/models

- `/v1/health` 与 `/v1/models` 对多个上游的探活必须并发执行，总耗时不应随上游数量线性增长。
- 单个上游探测失败/超时只影响该上游的状态字段，不得让整个端点失败。

### R4 500 响应必须进入 HTTP 指标

- 未捕获异常返回的 500 必须计入 `quasheng_http_requests_total{...status="500"}`，且不得重复计数。

### R5 环境变量非法值必须给出可读的配置错误

- `QUASHENG_PORT` / `QUASHENG_MAX_CONCURRENCY` / `QUASHENG_SHUTDOWN_TIMEOUT` 等数值型覆盖项解析失败时，抛出 `ConfigError` 并在消息中点名环境变量，而不是裸 `ValueError`。

### R6 CLI 网络/解析异常不得崩溃

- 对话模式（REPL 与 `--once`）在节点不可达、响应非 JSON 时应打印可读错误并以失败返回，而不是抛出未捕获异常退出；REPL 失败轮次仍需回滚该轮 user 消息。

## Constraints

- 不修改现有端点契约：`X-Node-Id` / `X-Node-Role` / `Contract-Version` 保持，错误体保持 OpenAI 形状。
- 不调整 `queue_len` / `max_concurrency` 默认值与语义，不新增端点。
- 仅改动 `nodes/quasheng` 及其测试；不触碰 `nodes/sanxian`。
- 日志与错误体不得新增密钥、`base_url`、token 泄漏面。
- 不推送远端，不擅自提交。

## Acceptance Criteria

- [ ] R1：新增用例模拟 `aclose()` 抛异常，断言闸门 `active` 归零且后续 `acquire()` 成功；远程路径同样覆盖。
- [ ] R2：新增用例构造“首个目标 5xx + 候选上游满载”场景，断言响应为 429、无回退标记头、`/v1/logs` 无“已回退到”、`/metrics` 无 `quasheng_fallback_total`。
- [ ] R3：新增用例断言多上游探测并发（峰值并发数 > 1），并保持 health/models 既有字段不变。
- [ ] R4：新增用例触发 500 后断言 `/metrics` 出现 `status="500"`，且单次请求只计 1 次。
- [ ] R5：新增用例设置非法数值环境变量，断言抛 `ConfigError` 且消息包含变量名。
- [ ] R6：新增用例让 transport 抛 `ConnectError` / 返回非 JSON，断言打印错误且返回失败，不抛出异常。
- [ ] `pytest nodes/quasheng/tests -q` 全绿；`bash scripts/smoke_quasheng.sh` 通过。
