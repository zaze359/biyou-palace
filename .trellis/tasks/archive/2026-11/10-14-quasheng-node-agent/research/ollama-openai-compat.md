# Research · Ollama 自带 OpenAI 兼容端点（决定准圣实现方式）

> 来源：`docs.ollama.com/openai`（官方文档），辅以社区实践验证
> 影响：直接决定了 `10-14-quasheng-node-agent` 是「写转码层」还是「写代理层」

## 结论

**Ollama ≥ 0.5.x 原生暴露 OpenAI 兼容端点**，无需我们在节点智能体里重写 Ollama → OpenAI 的字段转换。

官方支持的调用形式：

- `POST /v1/chat/completions`（含 vision 的 multimodal content 支持）
- `POST /v1/completions`
- `GET /v1/models`、`GET /v1/models/{model}`
- `POST /v1/embeddings`
- 新版另有 `/v1/responses`

官方用法示例要点：客户端 `base_url='http://localhost:11434/v1/'`，`api_key` **必填但被 Ollama 忽略**（`'ollama'` 即可）。

→ **准圣智能体定位改为「薄代理 + 契约专属表面」**：OpenAI 语义代理透传，`/v1/health` 与健康/能力/并发治理由节点原生实现。

## 必须写进实现的 5 个坑

1. **首次请求可能阻塞 30s+**（模型首次加载）。生产实践建议给客户端设 `timeout=60`。
   → 我们已选择**预加载白名单模型**策略消除该延迟，并由 `/v1/health` 如实标注 `loaded`。
2. **流式 chunk 的 `choices[0].delta.content` 可能为 `None`**（角色/结束标记 chunk）。
   → 透传阶段风险低；**一旦将来要聚合 `usage` 或编织内容，必须先加 `is not None` 保护**。
3. **官方标注为「实验性、可能破坏式变更」的子集**。
   → 入参**原样透传、不替它做严格校验**，避免我们放大版本差异。
4. **Ollama 自身没有任何限流**，可能被打爆到 OOM。
   → 并发管控**必须由节点智能体实现**（对应契约的 `limits.max_concurrency` 与 `resources.queue_len`）。
5. **Ollama 忽略 `api_key`**。
   → 我们的 bearer 鉴权必须**独立实现**；且代理时**不得把客户端 `Authorization` 头转发给 Ollama**。

## 版本强约束

低于 0.5.0 **完全没有 `/v1/chat/completions`**。因此：
- 文档需声明 **Ollama ≥ 0.5.x** 为运行前置；
- 启动时须探测 `{ollama_base}/v1/models`，失败要明确报错退出，而不是带病 degrade 运行。

## 与散仙的不对称

散仙走的是 smallphone 的 **llama.cpp JNI 库集成**（并非 `llama-server` 进程），**不自带任何 HTTP/OpenAI 服务**，必须在 Ktor 中手写完整 OpenAI 层（含 SSE 拼装）。

这是准圣与散仙在同一份契约下工作量差异最大的地方，也再次支持「**先做准圣**」的顺序：它便宜，且能先把契约行为验证清楚。
