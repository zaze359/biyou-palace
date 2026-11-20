# Design · 道果聚合（P3）

## 1. 接口

```python
class Aggregator(Protocol):
    async def aggregate(
        self,
        inputs: list[str],
        *,
        instructions: str | None,
        timeout_s: float,
    ) -> str | None: ...
```

- `inputs` 已按 `aggregate.input_step_ids` 顺序排列，只包含成功且有内容的步骤。
- 返回 `None` 表示无可用答案；runner 仍按 `sources`/`partials` 判定 status。

## 2. 实现

### ConcatAggregator

`"\n\n".join(inputs)`；无输入返回 `None`。默认实现，行为与 P2 一致。

### LlmAggregator

- 经 `ZHENTU_NEWAPI_BASE_URL` + `ZHENTU_NEWAPI_API_KEY` 调用 `/v1/chat/completions`。
- system prompt：合并子结果、保留关键事实、不编造、不提及内部步骤 id。
- user prompt：`instructions` + 编号子结果。
- 任何异常/超时/空内容 → 回退 `ConcatAggregator`，不向上抛错。
- 不记录 prompt 正文、密钥。

## 3. Runner 集成

- `TaskRunner` 新增 `aggregator` 依赖；聚合阶段替换内联 join。
- `status` 仍由 `partials` / `deadline_hit` / `required_failed` 决定，聚合器不改变终态。
- 聚合阶段写入 `aggregate/started` 与 `aggregate/result` 事件，data 增加 `mode`。

## 4. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZHENTU_AGGREGATOR_MODE` | `concat` | `concat` / `llm` |
| `ZHENTU_AGGREGATOR_MODEL` | 空 | 为空时即使 mode=llm 也回退 concat |
| `ZHENTU_AGGREGATOR_TIMEOUT_S` | 30 | 聚合超时 |

`mode=llm` 但缺 NewAPI 配置时启动不失败（编排仍可用），运行时回退 concat；配置值非法则拒绝启动。

## 5. 测试

- 默认 concat：沿用现有用例。
- 注入 `FakeAggregator` 验证 answer 来源。
- `LlmAggregator` 在无配置/异常时回退 concat（monkeypatch 或注入假传输层）。

## 6. 回滚

`ZHENTU_AGGREGATOR_MODE=concat` 即回到 P2 行为；代码回滚删除 aggregator.py 并恢复 runner 内联拼接。
