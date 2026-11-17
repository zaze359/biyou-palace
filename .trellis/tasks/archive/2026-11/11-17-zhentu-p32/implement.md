# 实施计划（P3.2）

## 顺序

1. 散仙 `Resources` 增可选 `active`；健康采集改为 `queue_len=0`、`active=在途数`；同步注释与测试。
2. 阵图新增 `zhentu/metrics.py`（线程安全计数器 + 固定分桶直方图 + Prometheus 文本输出）。
3. 阵图 `/route` 记录指标；新增 `GET /zhentu/v1/metrics`（Bearer 鉴权）。
4. 新增 `zhentu/tests/test_metrics.py`。
5. 契约文档 v1.2、阵图/散仙 README 更新。
6. 验证：阵图与准圣 pytest；散仙 Kotlin 单测（如环境可用）。

## 指标口径

- `zhentu_route_total{strategy,result}`：result=`selected|no_route`。
- `zhentu_route_rejected_total{reason}`：按 rejected 条目累计。
- `zhentu_route_duration_seconds`：直方图，桶 1ms/2ms/5ms/10ms/25ms/50ms/100ms/250ms/500ms/1s/2.5s/+Inf。
- 不新增节点/模型标签，避免高基数与信息泄漏。

## 回滚

- 散仙新增字段为可选，回滚仅需还原字段与采集；阵图指标端点纯新增，可整体 revert。
