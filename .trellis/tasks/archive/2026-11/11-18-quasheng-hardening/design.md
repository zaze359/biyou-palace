# 设计：准圣健壮性修复

## 现状与问题定位

| 编号 | 位置 | 现象 | 根因 |
|---|---|---|---|
| R1 | `app.py` `_stream_body` / `_stream_upstream_body` / `_handle_local_chat` / `_handle_upstream_chat` | 清理顺序为 `await stack.aclose()` → `gate.release()`；若关闭抛错，释放被跳过，名额永久泄漏 | 顺序执行，未用嵌套 `finally` |
| R2 | `app.py` `_run_auto_fallback` | 候选返回 `_capacity_error()`（`JSONResponse 429`）被当作成功结果，打回退头/日志/指标 | 仅用 `isinstance(result, _ChatFailure)` 判断成败 |
| R3 | `app.py` `health()` / `models()` | `for runtime: await runtime.refresh_status()` 串行，单上游探测超时 2s，N 个上游最坏 N×2s | 无并发编排 |
| R4 | `app.py` `handle_unexpected` | 500 由 `ServerErrorMiddleware` 生成，绕过用户中间件，`metrics.observe_http` 不执行 | 指标只在中间件记录 |
| R5 | `config.py` `load_settings` | `cast(val)` 直接抛 `ValueError`，调用方只捕获 `ConfigError` | 无转换包装 |
| R6 | `cli.py` `_stream_chat` / `_list_model_ids` | `httpx.HTTPError`、`ValueError` 直接冒泡，REPL 崩溃 | 未在边界捕获 |

## 方案

### R1 流式清理

新增模块内私有协程：

```python
async def _close_stream(stack, gate, runtime=None):
    try:
        await stack.aclose()
    except Exception:
        logger.warning("关闭上游流失败", exc_info=True)
    finally:
        if runtime is not None:
            runtime.release()
        await gate.release()
```

- 所有流式失败分支与流生成器 `finally` 统一调用它，保证“关闭失败不掩盖原始结果、名额必归还”。
- 保持响应语义不变：失败分支仍返回原有 `_ChatFailure` / 抛出原异常。

### R2 回退成功判定

在 `_run_auto_fallback` 中：

```python
if not isinstance(result, _ChatFailure):
    succeeded = index > 0 and getattr(result, "status_code", 200) < 400
    if succeeded:
        ...原有打标/日志/指标...
    return result
```

- 不改候选顺序、不改 4xx/429 不兜策略；仅修正观测与响应头。

### R3 并发探活

```python
async def _refresh_upstreams(runtimes):
    if not runtimes:
        return
    await asyncio.gather(*(r.refresh_status() for r in runtimes), return_exceptions=True)
```

- `refresh_status` 内部已把 httpx 异常收敛为状态，`return_exceptions=True` 仅防御性兜底。
- `health` 与 `models` 改为先并发刷新，再遍历读取状态；输出字段顺序与内容不变。

### R4 500 指标

在 `handle_unexpected` 返回前调用
`metrics.observe_http(request.url.path, request.method, 500)`。
该 handler 位于 `ServerErrorMiddleware`，不会经过用户中间件，因此不存在二次计数。

### R5 环境变量解析

```python
for env_key, (attr, cast) in _ENV_OVERRIDES.items():
    val = os.environ.get(env_key)
    if not val:
        continue
    try:
        setattr(settings, attr, cast(val))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"环境变量 {env_key}={val!r} 无法解析为 {cast.__name__}") from exc
```

### R6 CLI 边界

- `_stream_chat`：把 `httpx.HTTPError` 与 `ValueError`（非 JSON 响应）收敛为打印一行错误并返回 `(False, "")`。
- `_list_model_ids`：非 JSON 响应抛 `CliHTTPError`（消息说明响应不是 JSON），由调用方统一处理。
- `run_repl` 的 `/model` 分支额外捕获 `httpx.HTTPError`，与 `CliHTTPError` 同样只打印。

## 兼容性与回滚

- 全部为内部行为修复，无配置项、无端点、无响应字段变更；契约头与错误形状保持不变。
- 回滚以单文件为单位：`app.py` / `config.py` / `cli.py` 各自独立，可单独 revert。
- R3 只改变耗时，不改变字段顺序与内容。

## 测试策略

- R1：直接调用 `_stream_body` / `_stream_upstream_body`，注入 `aclose()` 抛错的假 stack，断言 `gate.active == 0`、`runtime.active == 0` 且可再次 `acquire`。
- R2：复用 `test_fallback.py` 的 mock app，构造“本地 500 + 候选上游满载”，断言状态码、头、日志与指标。
- R3：直接调用 `_refresh_upstreams`，用记录峰值并发的假 runtime。
- R4：`_build_client` 让上游 handler 抛 `RuntimeError`，请求 500 后检查 `/metrics`。
- R5：`monkeypatch.setenv("QUASHENG_PORT", "abc")`，断言 `ConfigError` 含变量名。
- R6：`httpx.MockTransport` 抛 `ConnectError` / 返回非 JSON 文本，断言返回值与捕获输出。
