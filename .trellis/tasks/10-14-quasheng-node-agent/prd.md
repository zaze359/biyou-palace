# PRD · 准圣 PC 节点智能体（Python + Ollama）

> 父任务：`10-13-biyou-v1-blueprint`（BiYouPalace V1.0 主规划）· 第一步子任务
> 协议基准：`docs/node-agent-contract.md` v1.0

## 一、目标与价值

让一台 PC 成为**独立可用的自治双模单元**：本地跑 Ollama（7B/14B）并对外暴露 OpenAI 兼容推理服务，同时可在本地直接对话。

这是自底向上节奏的**第一个交付物**，也是散仙（Kotlin / llama.cpp）实现时的**行为参照样板**——先跑通它验证契约自洽，再做散仙。

## 二、关键技术判断（已查证，决定了实现方式）

**Ollama ≥ 0.5.x 原生暴露 OpenAI 兼容端点**（依据：`docs.ollama.com/openai`）：`POST /v1/chat/completions`、`GET /v1/models`、`POST /v1/embeddings`（新版另有 `/v1/responses`）。

因此本智能体的定位是**「薄代理 + 契约专属表面」**，而不是重写 Ollama → OpenAI 的转换层：

| 契约端点 | 实现方式 |
|---|---|
| `/v1/chat/completions` | **反向代理**本机 Ollama `/v1/chat/completions`，SSE 流式**原样透传** |
| `/v1/models` | 代理 Ollama `/v1/models` |
| `/v1/embeddings` | 代理 Ollama `/v1/embeddings`（按能力判定，无能力则 501） |
| `/v1/health` | **节点原生实现**：`/api/tags`（模型清单）+ `/api/ps`（已加载模型/显存）+ `psutil`（资源）拼装契约 JSON |
| 通用切面 | 节点自实现：响应头、bearer 鉴权、OpenAI 错误归一、断连取消、并发闸门 |

**由此避免的工作**：不写 `/api/chat` → OpenAI 的字段映射，不做 SSE 形状转换（最容易出错的一块）。

> 不对称提示：散仙**没有这份便利**——llama.cpp 在 smallphone 中是 JNI 库集成、不走 `llama-server`，散仙必须在 Ktor 里手写完整 OpenAI 层。这也是本任务先做的理由之一。

## 三、已定运行策略

1. **模型加载 = 预加载白名单**：启动时对配置项 `preload_models` 中的模型做 warm-up 使其常驻；白名单外模型仍按需加载。**目的**：消除"首次请求阻塞 30s+"（`health` 需如实反映加载状态供上层判断）。
2. **满载策略 = 立即拒绝**：并发达到 `max_concurrency` 时返回 `429` + `Retry-After`，让上层（网关 / 阵图）立即改路由到其他仙徒，而非本地排队。理由：集群里有别的仙徒可能正闲着，本地排队不如立刻换节点。

## 四、范围与要求

须实现 `docs/node-agent-contract.md` v1.0 的全部端点与切面：

- `GET /v1/health`：`node`（`role=quasheng`、`runtime=ollama`、`arch`）/ `status` / `models[]`（含 `loaded` 与 `capabilities`）/ `resources` / `limits`（含 `max_concurrency`）/ `updated_at`。准圣的 `battery_pct`、`temperature_c`、`thermal_state` 置 `null`。
- `GET /v1/models`：代理 Ollama `models`。
- `POST /v1/chat/completions`：SSE **流式透传**（保留 `data: [DONE]`）。
- `POST /v1/embeddings`：按能力映射判定，不支持则返回 `501`。
- **并发闸门**：超限 `429` + `Retry-After`；`health.resources.queue_len` 反映在途请求数。
- **能力声明**：配置化的 `capabilities_map`（模型 → 能力列表），未登记模型默认 `["chat"]`。
- **通用切面**：`X-Node-Id` / `X-Node-Role` / `Contract-Version: 1.0` 响应头；OpenAI 风格错误；可选 bearer 鉴权（**代理时不得把客户端 `Authorization` 转发给 Ollama**）；**客户端断连即中止生成并释放资源**。
- **本地模式**：提供最小 CLI（`python -m nodes.quasheng.cli`），通过 `localhost` 调用同一套端点；**不存在第二套本地 API**。

**运行前置要求**：Ollama **≥ 0.5.x**（低于此版本无 `/v1/*`）。启动时须探测 `{ollama_base}/v1/models`，不可用则明确报错并退出。

## 五、验收标准

1. 四个端点的行为与契约 v1.0 **逐条一致**（字段、状态码、错误格式）。
2. `/v1/chat/completions` SSE 流式**原样透传**，以 `data: [DONE]` 结束，且节点不重新解析 chunk。
3. 不具备的能力返回 `501` 且 `error.code = capability_not_supported`。
4. `/v1/health` 返回**真实**数据；`role=quasheng`、`runtime=ollama`、`contract_version=1.0`；`models[].loaded` 与 `/api/ps` 结果一致。
5. **预加载生效**：启动完成后，白名单模型的 `loaded` 为 `true`，首次请求无 30s 级延迟。
6. **并发治理生效**：`max_concurrency=N` 时第 N+1 个请求返回 `429` 且带 `Retry-After`；`queue_len` 与实际在途数一致。
7. 响应头 `X-Node-Id`、`X-Node-Role`、`Contract-Version` 齐备。
8. bearer 开启时缺失 token 返回 `401`（**覆盖全部端点，含 health**）；关闭时可匿名访问。
9. 代理链路未向 Ollama 转发客户端 `Authorization`。
10. 请求中途断开：服务端确认中止生成、释放资源。
11. **本地 CLI 走 localhost 同一套端点可用**——证明本地与集群两条路径同一实现。
12. Ollama 不可用时 `health.status = down`，**进程本身不得崩溃**；资源采集失败降级为 `null`，不得 `500`。
13. Ollama < 0.5.x（无 `/v1/*`）时启动探测失败并给出明确错误提示，而不是带病运行。

## 六、非目标（第一步不做）

- 不含节点注册到碧游宫 / 拉取子任务（第二步）。
- 不含 Prometheus 指标格式（第四步灵脉直接 scrape `/v1/health`）。
- 不含本土 GPU / 显存调优（只读取 `/api/ps` 汇报，不做参数调整）。
- **不含 Ollama → OpenAI 转码层**（Ollama 自身已提供，代理即可）。
- 不含 PC 端 GUI 聊天界面（第一步 CLI 足矣）。

## 七、依赖与顺序

无前置子任务，**作为第一步首个开工项**；完成后散仙在 KMP 侧照此行为实现对齐。

## 八、风险与已识别的坑

| 坑 | 应对 |
|---|---|
| Ollama OpenAI 兼容为**实验性子集** | 入参**原样透传不校验**，避免我们替它做 validation 而放大差异 |
| 缺失/错误的 `usage` 字段 | 透传阶段不解析；未来若要聚合必须做 `None` 保护 |
| **Ollama 自身无限流** | 并发闸门必须由本节点实现（对应 `max_concurrency` / `queue_len`） |
| Ollama 忽略 `api_key` | 我们的 bearer 独立实现，且不向 Ollama 转发客户端 Authorization |
| 模型加载慢 / 首请阻塞 | 已由「预加载白名单」策略解决 + `health` 如实标注 `loaded` |
| `psutil` 在部分平台受限 | 采集失败降级 `null`，禁止 `500` |
| 版本漂移（<0.5 无 `/v1/*`） | 启动探针强制校验，失败即退出并提示 |
