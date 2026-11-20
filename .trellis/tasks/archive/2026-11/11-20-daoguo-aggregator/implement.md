# Implement · 道果聚合（P3）

- [x] `zhentu/aggregator.py`：`Aggregator` 协议、`ConcatAggregator`、`LlmAggregator`
- [x] `LlmAggregator`：NewAPI 调用、超时、空内容与异常回退 concat
- [x] `TaskRunner` 注入聚合器，替换内联拼接
- [x] 配置：`ZHENTU_AGGREGATOR_MODE` / `_MODEL` / `_TIMEOUT_S`，非法值拒绝启动
- [x] `create_app` 支持注入聚合器
- [x] 测试：默认 concat、注入聚合器、LLM 回退
- [x] `pytest zhentu/tests` 全绿
