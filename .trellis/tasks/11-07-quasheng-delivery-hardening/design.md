# 技术设计

## 1. 配置与默认值

- `Settings` 新增 `host: str = "127.0.0.1"`、`shutdown_timeout: float = 10.0`，`max_concurrency` 默认改为 4。
- `_ENV_OVERRIDES` 新增 `QUASHENG_HOST`、`QUASHENG_SHUTDOWN_TIMEOUT`。
- `load_settings()` 末尾调用 `validate_settings()`：`host` 非回环且 `shared_token` 为空 → 抛 `ConfigError`，由 lifespan 记录明确日志后终止启动。
- `__main__` 使用 `settings.host`，不再硬编码 `0.0.0.0`。

## 2. 能力校验

- 抽出 `_require_capability(settings, model, capability)`，chat 与 embeddings 共用，错误形状完全一致（501 / `capability_not_supported` / `invalid_request_error`）。
- 未登记模型仍走 `capability_for` 的默认 `["chat"]`，不改变既有行为。

## 3. queue_len 与 active

- `ConcurrencyGate` 增加 `waiting` 计数。当前策略是"满载立即 429、不做本地排队"，因此 `waiting` 结构上恒为 0——这正是契约 3.1"当前排队请求数"的真实值。
- `resources.queue_len = gate.waiting`；新增 `resources.active = gate.active` 作为面板/排障用的在途并发。
- 面板资源卡改为两行：在途并发（进度条 active/max）+ 排队数；压测采样改采 `active`，标签改为"在途并发"。

## 4. loading 判定

- 新模块 `loading.py`：`LoadingTracker`
  - `set_loaded(names)` 启动时用 `/api/ps` 初始化已加载集合。
  - `note_request(model)`：模型不在已加载集合时登记 pending（仅在尚未登记时）。
  - `mark_loaded(model)` / `mark_failed(model)`：首个 SSE chunk 或成功响应 / 上游错误时清理。
  - `sync(loaded)`：health 每次拿到 `/api/ps` 后同步已加载集合，清除已加载的 pending。
  - `is_loading()`：仅当存在 pending 且未超过 TTL（取 `request_timeout`）。
- `build_health(..., loading=None, warmup_failed=None)`：
  - 预热判定只统计存在于 `/api/tags` 的模型，并排除 `warmup_failed`；不存在的模型不再拖住 `ready`。
  - `determine_status(..., on_demand_loading=bool)`：`down > loading > degraded > ready`。

## 5. 启动重试与自恢复

- `_probe_with_retry(client, attempts=5, base_delay=1.0)`：指数退避 1/2/4/8 秒，逐次记录 WARNING；耗尽后抛最后一次 `UpstreamUnavailable`。
- lifespan 用该函数替代单次 `probe()`。
- 运行期恢复：health 每次实时请求 `/api/tags` + `/api/ps`，恢复后自动 `ready`；Ollama 不可达时 `down`，进程不崩。

## 6. 优雅停机

- `ConcurrencyGate.wait_idle(timeout)`：轮询 `active`，返回是否在超时内清空。
- lifespan shutdown：置 `app.state.shutting_down=True` → `wait_idle(shutdown_timeout)` → `client.aclose()`。
- HTTP 中间件在 `shutting_down` 时对 `/v1/*` 返回 503 `node_shutting_down`（OpenAI 错误形状）。
- `__main__` 向 uvicorn 传 `timeout_graceful_shutdown=settings.shutdown_timeout`；SIGTERM/SIGINT 由 uvicorn 捕获，先停 accept，再按超时取消残余任务。

## 7. CLI

- `_stream_chat(base, token, model, messages, *, transport=None) -> (ok, content)`：发送完整 messages 历史；非 2xx 读 body 并格式化打印 `HTTP <status> · <code> · <message>`；流式 chunk 原样拼接 assistant 内容。
- `run_repl`：维护 `messages`；`/reset` 清空；`/model <name>` 切换，`/model` 无参数列出模型；失败轮次回滚 user 消息，避免污染上下文。
- `--once` 失败返回非零退出码。

## 8. 部署模板

- `deploy/launchd/com.biyou.quasheng.plist`：`WorkingDirectory`、`EnvironmentVariables`、`KeepAlive`、`StandardOutPath`/`StandardErrorPath`。
- `deploy/systemd/quasheng.service`：`WorkingDirectory`、`EnvironmentFile`、`Restart=always`、`RestartSec`、`journal` 日志、`KillSignal=SIGTERM`、`TimeoutStopSec`。
- 模板用 `<REPO_ROOT>` / `<LOG_DIR>` 占位符，README 说明替换与安装命令。

## 兼容性

- 端点路由、鉴权、错误形状、SSE 透传不变；health 仅新增 `resources.active` 字段（附加字段，不破坏既有消费方）。
- 唯一语义变化是 `queue_len`（按要求修正）与 `loading`（按要求扩展）。
