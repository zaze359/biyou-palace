# PRD · 道果聚合（P3）

## 目标

把阵图任务结果聚合从 runner 内联拼接抽成可注入的 `Aggregator`：

- 默认 `concat`：按 `aggregate.input_step_ids` 顺序拼接，行为与 P2 一致
- `llm`：配置后经 NewAPI 对子结果做润色/合并，保留来源
- 聚合失败、超时或无配置时回退 `concat`，任务不因聚合失败整体失败
- `sources` / `partials` 语义不变；partial 不得被包装成 completed

## 范围

### In

- `zhentu/aggregator.py`：`Aggregator` 协议、`ConcatAggregator`、`LlmAggregator`
- runner 注入聚合器，替换内联拼接
- 配置：`ZHENTU_AGGREGATOR_MODE=concat|llm`、`ZHENTU_AGGREGATOR_MODEL`、`ZHENTU_AGGREGATOR_TIMEOUT_S`
- 测试：默认拼接、注入聚合器、LLM 失败回退

### Out

- 多轮裁决、去重、事实校验
- 单独聚合服务/进程
- 前端展示（P4）

## 验收

- 默认为 `concat`，P2 全部测试保持通过
- `llm` 模式下聚合器被调用，输出写入 `answer`
- 聚合器抛错/超时/newapi 未配置时回退拼接，任务终态不变
- 新增测试通过，`zhentu/tests` 全绿
