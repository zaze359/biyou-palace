# 节点智能体统一契约（Node Agent Contract）v1.0

> **实现方**：准圣节点（PC · Python / Ollama）、散仙节点（安卓 App · Kotlin / llama.cpp）
> **消费方**：碧游宫网关（渠道转发 / 探活）、阵图 ZhenTu（任务路由）、灵脉 LingMai（指标采集）、节点自身本地客户端
> **定位**：这是**协议级统一**契约——两套智能体用不同语言、不同推理运行时实现，但对外暴露完全一致的 HTTP 行为。取代早期设想的 KMP 代码共享方案。

## 一、设计原则

1. **协议共享取代代码共享**：准圣(Python) 与散仙(Kotlin) 不共享任何代码，共享的是本契约。这是「统一底座 + 独立实现 + 禁止 if/else 混写」在跨语言场景下的落地形态。
2. **一套实现同时服务集群与本地**：节点自身的本地对话客户端通过 `localhost` 调用同一套端点，**不存在独立的「本地 API」**。集群请求与本地请求走完全相同的代码路径。
3. **能力驱动路由（capability-driven）**：节点在 `/v1/health` 中声明能力、负载与健康状态，由上层（碧游宫 / 阵图）据此路由；节点不承接的能力返回 `501`，由上层绕开，而非在节点内部用 if/else 分支处理节点类型。
4. **实现无关**：契约不假定底层是 Ollama 还是 llama.cpp，只看行为。

## 二、通用约定

| 项 | 约定 |
|---|---|
| 基础路径 | `/v1/` |
| 内容类型 | `application/json`；流式用 SSE（`text/event-stream`） |
| 契约版本 | 响应头 `Contract-Version: 1.0`，并在 `/v1/health` 中以 `contract_version` 回显 |
| 溯源头 | 响应带 `X-Node-Id`（节点唯一 ID）、`X-Node-Role`（`quasheng` \| `sanxian`）——为 V1.1「日志审计 / 道果溯源」预留 |
| 鉴权 | 可选。`Authorization: Bearer <shared-token>`；内网可配置为免鉴权（建议开启，避免裸推理端点） |
| 取消语义 | 客户端断开连接时，节点须立即中止生成并释放资源（散仙尤其影响发热与耗电） |
| 错误格式 | 统一 OpenAI 风格：`{"error": {"message": "...", "type": "...", "param": null, "code": "..."}}` |

## 三、端点

### 3.1 `GET /v1/health` — 健康与能力自述

供网关探活、灵脉采集、阵图路由消费。**第一步唯一必须实现的元数据端点**；第四步灵脉可直接 scrape 此端点的 JSON 快照。

```json
{
  "node": {
    "id": "quasheng-01",
    "name": "准圣·01",
    "role": "quasheng",
    "arch": "x86_64",
    "runtime": "ollama",
    "runtime_version": "0.5.x",
    "contract_version": "1.0"
  },
  "status": "ready",
  "models": [
    {
      "name": "qwen2.5:7b-instruct-q4_K_M",
      "family": "qwen2.5",
      "size_label": "7B",
      "context_len": 32768,
      "loaded": true,
      "capabilities": ["chat", "embedding"]
    }
  ],
  "resources": {
    "cpu_pct": 12.5,
    "mem_used_mb": 6144,
    "mem_total_mb": 16384,
    "disk_free_mb": 204800,
    "queue_len": 0,
    "battery_pct": null,
    "temperature_c": null,
    "thermal_state": "nominal"
  },
  "limits": { "max_concurrency": 4, "max_context_len": 32768 },
  "updated_at": "2026-10-14T01:20:00+08:00"
}
```

字段约束：
- `role`：固定 `quasheng`（准圣 / PC）或 `sanxian`（散仙 / 手机）。
- `runtime`：固定 `ollama` 或 `llama.cpp`。
- `status`：`ready` 可服务 ｜ `loading` 模型加载中（上层暂勿路由）｜ `degraded` 高温 / 内存吃紧等降质状态（上层应降权）｜ `down` 拒绝服务。
- `models[].capabilities`：`chat` ｜ `embedding` ｜ `vision` ｜ `ocr` ｜ `rerank`。
- 散仙专属：`battery_pct`、`temperature_c`、`thermal_state`（`nominal` \| `warn` \| `hot`）须真实填充；准圣置 `null`。
- `queue_len`：当前排队请求数，供网关加权 / 阵图避免堆积。

### 3.2 `GET /v1/models` — 模型列表（OpenAI 兼容）

由 `models` 派生，供上层发现可用模型。

```json
{ "object": "list", "data": [ { "id": "qwen2.5:7b-instruct-q4_K_M", "object": "model", "owned_by": "biyou-node" } ] }
```

### 3.3 `POST /v1/chat/completions` — 核心推理端点

请求 / 响应语义兼容 OpenAI。

- 请求：`{"model": "...", "messages": [{"role": "user", "content": "..."}], "stream": false, "temperature": 0.7, "max_tokens": 512, ...}`
- 非流式：返回标准 OpenAI `ChatCompletion` 对象。
- 流式（`stream: true`）：SSE `data: {chunk}`，以 `data: [DONE]` 结束，语义同 OpenAI。**必选支持**——散仙本地聊天的打字机效果与阵图的流式聚合都依赖它。
- 不具备该能力时：返回 `501`，`code` 为 `capability_not_supported`。

### 3.4 `POST /v1/embeddings` — 向量化端点

`{"model": "...", "input": ["..."]}` → 标准 OpenAI embeddings 响应。无 embedding 能力的节点返回 `501`。

## 四、能力矩阵（第一步预期）

| 能力 | 准圣（PC / Ollama） | 散仙（手机 / llama.cpp） |
|---|---|---|
| `chat` 主 LLM 推演 | ✅ 7B / 14B，长上下文 | ⚠️ 轻量模型，短上下文 |
| `embedding` | ✅ | ✅ 主要承接方 |
| `vision` / `ocr` | 视模型而定 | ✅ 主要承接方 |
| `rerank` | 可选 | 可选 |
| `max_concurrency` | 较高（建议 ≥ 4） | 低（建议 1~2） |
| 温度 / 电池上报 | 视机型 | ✅ 必填 |
| `X-Node-Role` | `quasheng` | `sanxian` |

## 五、V1.0 边界（第一步不做）

- **不含注册 / 任务拉取接口**：节点注册到碧游宫、接收子任务属第二步（网关接入），本步只保证上述四类端点。
- **不含模型 / 权重管理 API**：模型放置由节点本地管理（散仙由 App 管理）。
- **不含 Prometheus 指标格式**：第四步灵脉再定义；本步 `/v1/health` 返回 JSON 快照即可，灵脉后续直接 scrape 该端点。
- **不含集群级重试 / 降级**：属第三步阵图编排职责。
