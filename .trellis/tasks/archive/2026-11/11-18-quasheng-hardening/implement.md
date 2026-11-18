# 执行计划：准圣健壮性修复

## 顺序

1. **R1 流式清理**（`nodes/quasheng/app.py`）
   - 新增 `_close_stream(stack, gate, runtime=None)`。
   - 替换 `_stream_body` / `_stream_upstream_body` 的 `finally`，以及 `_handle_local_chat` / `_handle_upstream_chat` 的 4 处失败分支清理。
   - 测试：`tests/test_app.py` 增加 `aclose()` 抛错用例（本地 + 远程）。
2. **R2 回退成功判定**（`nodes/quasheng/app.py`）
   - `_run_auto_fallback` 增加 2xx 判定，非 2xx 原样返回。
   - 测试：`tests/test_fallback.py` 增加候选满载用例（头/日志/指标三断言）。
3. **R3 并发探活**（`nodes/quasheng/app.py`）
   - 新增 `_refresh_upstreams`，`health()` 与 `models()` 改用。
   - 测试：`tests/test_upstream.py` 增加峰值并发断言。
4. **R4 500 指标**（`nodes/quasheng/app.py`）
   - `handle_unexpected` 内记录 500。
   - 测试：`tests/test_observability.py` 增加 500 指标用例。
5. **R5 环境变量解析**（`nodes/quasheng/config.py`）
   - 包装 `cast`，失败转 `ConfigError`。
   - 测试：`tests/test_config.py` 增加非法端口用例。
6. **R6 CLI 异常边界**（`nodes/quasheng/cli.py`）
   - `_stream_chat` / `_list_model_ids` / `run_repl` 捕获网络与解析异常。
   - 测试：`tests/test_cli.py` 增加连接失败与非 JSON 用例。

## 验证命令

```bash
.venv/bin/python -m pytest nodes/quasheng/tests -q
.venv/bin/python -m pytest nodes/zhentu/tests -q
bash scripts/smoke_quasheng.sh
```

## 评审门

- 每个修复点必须先有可复现的失败用例（或明确的最小复现脚本），再改实现。
- 不改端点字段、契约头、错误体形状；不改默认并发/排队语义。
- 全部完成后跑全量 pytest 与 smoke，记录命令与结果到最终报告。

## 回滚点

- 每个 R 项独立提交可 revert；若 R3 引入时序问题，单独回退 `health/models` 的并发改动即可恢复串行。
