# 执行计划

## 阶段一 · 正确性与默认安全（D1–D5）

1. D1：改 `config.example.yaml`（`nomic-embed-text:latest`、max_concurrency=4）、`Settings` 默认 4、`.env.example` 4；预热缺失模型不拖 loading。
2. D2：chat 端点加能力校验，抽 `_require_capability` 与 embeddings 共用。
3. D3：`ConcurrencyGate.waiting` + `resources.active`，panel 与压测采样同步。
4. D4：`loading.py` + health 判定扩展，预热失败/缺失模型不阻塞。
5. D5：`host` 配置与启动校验，`__main__` 改回环，README/.env 同步。

验证：`pytest nodes/quasheng -q`、`bash scripts/smoke_quasheng.sh`。
提交：`fix(quasheng): 修正能力/队列/加载语义并收敛默认监听安全`（按具体路径 add）。

## 阶段二 · 常驻与自恢复（D6–D8）

6. D6：新增 launchd/systemd 模板 + README 运维段。
7. D7：`_probe_with_retry` + 恢复后 ready 用例。
8. D8：`shutting_down` 中间件 + `wait_idle` + uvicorn graceful timeout + 用例。

验证同上。
提交：`feat(quasheng): 新增常驻模板、启动重试与优雅停机`。

## 阶段三 · CLI 收尾（D9–D10）

9. D9：REPL 多轮、`/reset`、`/model`。
10. D10：非 2xx 打印 OpenAI 错误体，失败非零退出。

验证：`pytest nodes/quasheng -q`、手工 `python -m nodes.quasheng.cli --once "hi"`（节点可用时）。
提交：`feat(quasheng): CLI 支持多轮上下文与错误体展示`。

## 回滚点

- 阶段一若 smoke 失败：先回滚 D3/D4 的 health 结构改动（`resources.active` 为附加字段，可单独撤）。
- 阶段二 graceful 若与 uvicorn 行为冲突：保留 `wait_idle`，撤中间件 503。
- 阶段三不影响服务端，可独立回滚。

## 验收命令

```bash
.venv/bin/python -m pytest nodes/quasheng -q
bash scripts/smoke_quasheng.sh
```
