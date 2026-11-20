# Implement · 阵图任务账本与集群编排（P2）

## 1. 账本

- [x] `zhentu/ledger.py`：`TaskStore`，建表 `tasks` / `task_events` / `task_steps` / `route_decisions`
- [x] `create_task`（幂等唯一约束）、`get_task`、`get_task_by_request`
- [x] `append_event`（事务内分配 `seq`）、`list_events(after_seq)`、`get_event`
- [x] `update_task`、`upsert_step`、`count_active_tasks`
- [x] 单元测试：幂等、事件序号、容量计数

## 2. 契约模型与编排

- [x] `zhentu/task_models.py`：`TaskCreateRequest` 与枚举/长度校验
- [x] `zhentu/orchestration.py`：`Planner` / `Executor` 协议、`SingleStepPlanner`、`PlannerClient`、`NewApiExecutor`、`validate_plan`
- [x] `validate_plan` 覆盖：id 唯一、依赖前序、无环、能力合法、上限
- [x] `NewApiExecutor` 通过 `/v1/chat/completions` 调用，携带 Bearer，不记录明文密钥

## 3. 执行器

- [x] `zhentu/runner.py`：`TaskRunner.run(task_id)`
- [x] 状态机 `queued → running → aggregating → completed|failed|canceled`
- [x] 选路 + `route_decisions` 落库
- [x] `required` / `failure_policy` / `partials`
- [x] 取消位、`step/canceled`、deadline
- [x] 聚合与 `ResultEnvelope` 落盘

## 4. 端点

- [x] `require_node_identity`：每节点 token 映射与 `origin_node_id` 绑定
- [x] `POST /zhentu/v1/requests`：幂等、容量、`202`、`asyncio.create_task`
- [x] `GET /zhentu/v1/requests/{id}/events`：SSE、`Last-Event-ID`、终态关闭
- [x] `GET /zhentu/v1/requests/{id}`：状态 / `ResultEnvelope`
- [x] `POST /zhentu/v1/requests/{id}/cancel`
- [x] lifespan 关闭 `TaskStore` 并取消后台任务

## 5. 配置

- [x] `RegistrySettings` 新增 node tokens、任务并发、事件保留、NewAPI 配置
- [x] `load_settings` 解析 `ZHENTU_NODE_TOKENS`，非法 JSON 拒绝启动
- [x] `validate` 拒绝不自洽配置

## 6. 测试

- [x] `zhentu/tests/test_tasks.py`：创建 → 完成 → 取结果
- [x] 幂等重复提交、非发起节点 403、`local_only` 403
- [x] 取消、容量 429
- [x] 必需失败 `failed`、非必需失败 `partial`
- [x] 事件流回放与 `Last-Event-ID`
- [x] `pytest zhentu/tests` 全绿

## 7. 验证

```bash
./.venv/bin/python -m pytest zhentu/tests -q
git diff --check
python3 ./.trellis/scripts/task.py validate 11-20-zhentu-task-ledger
```

## 回滚

删除新增模块与路由注册；不动既有端点。数据库新表可保留，不影响旧逻辑。
