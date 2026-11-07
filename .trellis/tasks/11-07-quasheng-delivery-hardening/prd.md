# 准圣节点对外交付加固

## Goal

把准圣节点从"可演示"推进到"可对外交付"。不改既有端点契约与错误形状，只修正契约已要求但实现有偏差的语义，并补齐常驻/自恢复/CLI 的交付必需品。

## Requirements

### 第一段 · 正确性与默认安全

1. `config.example.yaml` 的预置模型必须与真实可拉取的模型名一致（embedding 模型名补 `:latest`，避免 `capability_for` 精确匹配失败）；预热模型不存在不能导致 `/v1/health` 长期 `loading`；`max_concurrency` 与契约建议（≥4）对齐。
2. 声明为 embedding-only 的模型请求 `/v1/chat/completions` 必须返回 `501 capability_not_supported`（契约 3.3），与 embeddings 判定逻辑对齐。
3. `queue_len` 语义改为真实排队数（契约 3.1），不再是当前在途并发数；相应调整 health 结构与面板显示。
4. `loading` 状态覆盖按需加载期间，而不只看预热白名单。
5. 默认只监听 `127.0.0.1`；若配置成非回环地址且 `shared_token` 为空，启动直接失败并给出明确提示。同步 README 与 `.env.example`。

### 第二段 · 常驻与自恢复

6. 提供 launchd（macOS）与 systemd（Linux）服务模板，含工作目录、环境变量、重启策略、日志去处。
7. 进程守护与崩溃重启说明；节点对 Ollama 的自恢复：启动时 Ollama 未就绪要重试（有限次数、退避），而不是立即退出；运行期上游恢复后 health 能自动回到 `ready`。
8. 优雅停机：收到 SIGTERM/SIGINT 后立即停止接受新连接（真实信号路径下 uvicorn 先关 listener，表现为连接被拒绝；应用内 503 `node_shutting_down` 仅用于主动停机路径），在途流继续完成，超过 `shutdown_timeout` 后退出。

### 第三段 · CLI 收尾

9. REPL 支持多轮上下文与 `/reset`，支持运行中 `/model` 切换。
10. 非 2xx 响应时打印 OpenAI 错误体（code/message），不要静默输出空。

## Constraints

- 除第 2、3、4 条明确要求修正的语义外，不改既有端点契约与错误形状。
- 现有 47 passed 测试与冒烟脚本必须继续通过；新增语义要有用例覆盖。
- 面板 `/ui` 若受影响需同步更新显示。
- 每阶段结束按具体路径 `git add` 提交，不使用 `git add -A`，不 push。

## Acceptance Criteria

- [ ] D1–D10 全部 `task.json` 子任务状态为 done。
- [ ] `pytest nodes/quasheng` 全绿（含新增用例）。
- [ ] `bash scripts/smoke_quasheng.sh` 全绿。
- [ ] README / `.env.example` / `config.example.yaml` 与实现一致。
- [ ] 三阶段各一次独立提交。

## Notes

契约基准：`docs/node-agent-contract.md` v1.0（3.1 queue_len、3.3 chat 501、2 章 Bearer/X-Node-*）。
