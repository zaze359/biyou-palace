# 实施计划（P3.0 + P3.1）

## 顺序

1. 阵图决策引擎 `zhentu/route.py`：硬过滤、软评分、策略权重、确定性排序、rejected 原因码。
2. 阵图协议 `zhentu/models.py` 增 `RouteRequest`；`zhentu/config.py` 增 `ZHENTU_ROUTE_TTL_MS`（默认 5000）。
3. 阵图端点 `POST /zhentu/v1/route`：参数校验、鉴权复用、决策日志、错误语义。
4. 阵图单测 `zhentu/tests/test_route.py`：过滤/评分/边界/确定性/脱敏。
5. 准圣出站客户端 `nodes/quasheng/registry.py`：`/route` 调用（300ms 超时、5s 缓存、失败不抛）。
6. 准圣消费 `nodes/quasheng/app.py`：`selected + alternates` → `newapi/<model>`，`X-Node-Route-Decision`，日志含 `decision_id`/`node_id`。
7. 准圣测试：更新 `test_fallback.py` 的 registry 用例到 `/route`，补决策头/日志/无候选/失败回退用例。
8. 文档：`zhentu/README.md` 端点与 `/route` 说明；`nodes/quasheng/README.md` registry 模式说明。
9. 真实验证：阵图进程 + 两个模拟节点 → `/route` 稳定排序；准圣（registry 模式）+ mock Ollama + mock 网关 → 回退成功带头与日志；停阵图 → 静态链条可用。

## 关键实现约定

- 策略权重固定为阵图侧常量表（balanced / latency / local_first / power_saving），逐请求只选 `strategy`。
- `max_candidates` 夹到 1..10；`allowed_models` > 200 返回 400 `too_many_models`；非法能力/策略返回 400。
- 缺失字段取中性值或保守估算，并在 `reasons` 标注 `estimated_load` / `context_unknown`，绝不 500。
- `rejected` 只返回 `node_id` / `model` / `reason`，不含 endpoint、错误详情、凭据。
- 无候选返回 200 + `selected=null` + `reason=no_route`。
- 准圣路由候选仍做 NewAPI 快照交集，执行不走节点直连；阵图失败当次退回静态链条。
- `fallback_source=static` 时不得产生任何阵图请求。

## 校验命令

```bash
.venv/bin/python -m pytest zhentu/tests -q
.venv/bin/python -m pytest nodes/quasheng/tests -q
bash scripts/smoke_quasheng.sh
```

## 评审门

- 对照 `design.md` 第 3/4/6/7/8 节检查过滤顺序、权重、错误码、响应头与日志字段。
- 检查 `/route` 响应、日志、错误体中不出现 endpoint、上游 key、节点 token、prompt。
- 检查 `fallback_source=static` 与 `/candidates` 既有行为未受影响。

## 回滚

- `/route` 为纯新增接口；准圣回退到 `fallback_source=static` 即恢复旧行为。
- 单次提交可整体 revert，无数据库迁移、无协议破坏性变更。
