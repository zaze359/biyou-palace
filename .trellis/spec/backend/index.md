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
