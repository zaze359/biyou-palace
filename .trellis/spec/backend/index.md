# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | ORM patterns, queries, migrations | To fill |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |

---

## Project-Specific Conventions

### 阵图（ZhenTu）是控制面，不是代理

- `POST /zhentu/v1/route` 只返回 `selected` / `alternates` / `rejected` 与 `decision_id`；
  绝不代理推理请求、不返回 `endpoint`、不持有节点或上游凭据。执行仍由调用方经 NewAPI 完成。
- 选路评分缺失字段必须退化（中性值或保守估算 + `reasons` 标注），不得因字段不全返回 5xx；
  无候选是 `200 + selected=null + reason=no_route`，不是 5xx。
- 新只读端点必须保持既有行为不变：`fallback_source=static` 的节点不得产生任何阵图请求，
  `GET /zhentu/v1/candidates` 等既有端点响应形状不得改动。
- 准圣 registry 模式的审计头：`X-Node-Fallback-Origin: registry|static`，
  阵图决策额外带 `X-Node-Route-Decision: dec-...`；`fallback` 日志同时记录决策与节点。
  阵图查询失败只让本次退回静态链条，不影响本地 chat/embeddings/health。
- 阵图 `/metrics` 与其它 `/v1` 端点一样要求 Bearer 鉴权，不新增鉴权例外；
  指标不得携带 `node_id` / `model` / endpoint 等高基数或敏感标签。
- 契约新增字段必须是可选的向后兼容字段；`Contract-Version` 响应头保持 `1.0`，
  文档版本单独递增（v1.2），旧节点无需改动。

### 准圣节点（nodes/quasheng）健壮性约定

- **流式清理与名额归还分离**：关闭上游流（`AsyncExitStack.aclose()`）可能抛异常，
  必须先释放并发名额再处理关闭结果；所有流式失败分支统一走 `_close_stream()`，
  禁止先 `aclose()` 后 `release()` 的裸顺序（否则关闭失败会永久泄漏名额，最终全站 429）。
- **回退成功标记只对 2xx 生效**：候选返回 `429 upstream_capacity` 等非 2xx 时，
  不得附加 `X-Node-Fallback-*`、写“已回退到”日志或累加 `quasheng_fallback_total`。
- **多上游探测必须并发**：`/v1/health`、`/v1/models` 使用 `_refresh_upstreams()` 并发探活，
  串行探测会让耗时随上游数量线性增长（单个超时上限 2s）。
- **5xx 必须进入指标**：`ServerErrorMiddleware` 生成的 500 不经过用户中间件，
  需在 `handle_unexpected` 内补记 `metrics.observe_http(..., 500)`。
- **CLI 异常边界**：REPL 与 `--once` 遇到节点不可达/非 JSON 响应只打印可读错误并返回失败，
  不得把 `httpx.HTTPError` 抛给用户。

### 观测数据的一致性

- 日志事件（`/v1/logs`）、Prometheus 指标与响应头描述同一事实时必须一致：
  成功态标记不能出现在失败响应上，失败也不应被记成成功。


---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
