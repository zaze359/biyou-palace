# 设计 · 节点接入外部 OpenAI 兼容上游（上游桥接）

> 任务：`11-08-upstream-provider-bridging`（设计稿，不落地运行时代码）
> 父规划：`.trellis/tasks/10-13-biyou-v1-blueprint/prd.md`
> 契约基准：`docs/node-agent-contract.md` v1.0
> 日期：2026-11-08（设计评审用）

**结论：可以做，但节点侧只应做「单上游 + 静态模型清单 + 原样直通」的最小桥接，且默认仅本地自用、默认不暴露给集群；多上游聚合、跨上游回退、密钥集中管理与用量计费应留在碧游宫网关，不应下沉到每个节点。**

---

## 1. 既有规划里的定位（调研结论）

### 1.1 已有结论

| 来源 | 已有结论 | 对本次方向的含义 |
|---|---|---|
| 蓝图 `10-13/prd.md` 模块 C | 节点是「自治双模单元」：本地推理对外暴露 OpenAI 兼容服务，同时可「本地独立对话 / 调外部模型自用」 | **外部模型在蓝图中是「节点自用」能力**，没有承诺把它作为集群能力对外供给 |
| 蓝图阶段二 | 外部/异构能力通过 **NewAPI 碧游宫网关** 作为「渠道」接入，网关负责权重、心跳、超时、故障隔离 | 集群级的多上游接入、路由、限流、计费，蓝图层面的归属是**网关** |
| 蓝图阶段三 | 集群级重试 / 降级属于 **阵图 ZhenTu** 编排职责 | 跨节点、跨上游的回退不是节点职责 |
| 蓝图验收 / 边界 | 「数据不出金鳌岛」「仅内网，不对外公网暴露」 | 接入公网中转站会**把 prompt 送出局域网**，与硬边界直接冲突，必须显式开关与审计 |
| 契约 v1.0 §一 | 能力驱动路由：节点只声明本地能力，上层据 `/v1/health` 路由；节点不承接的能力返回 501 | 契约中**没有 provider / 上游概念**，节点不应自行成为第二层路由器 |
| 契约 v1.0 §五 | 「不含集群级重试 / 降级：属第三步阵图编排职责」 | 节点内做跨上游回退会与契约职责划分冲突 |
| 准圣 PRD §二 | 定位是「薄代理 + 契约专属表面」，不写转码层，SSE 原样透传 | 节点做上游聚合会显著加厚代理层，偏离既有定位 |
| 准圣 PRD §四/§六 | 本地 CLI 走 localhost 同一套端点；非目标不含注册 / 拉取子任务 | 若做上游桥接，「节点自用外部模型」可复用同一套端点，不需要第二套 API |
| 散仙 PRD §四 | 与准圣同一份契约；非目标含集群级重试 / 降级 | 同一结论适用于散仙 |

### 1.2 冲突点（需要在评审中拍板）

| # | 冲突 | 说明 |
|---|---|---|
| C1 | **数据边界**：蓝图要求数据不出局域网，公网中转站必然出网 | 不是技术细节，是产品边界；必须默认关闭、逐模型显式开启，并在 `/v1/health` 标注 `data_leaves_lan` |
| C2 | **聚合位置**：网关 vs 节点 | 每个节点各接一套上游，会形成「节点内聚合 + 网关再聚合」的嵌套路由，密钥、限额、计费、排障全部分散 |
| C3 | **薄代理原则**：节点原本只代理本机 Ollama | 加路由、回退、按上游并发、上游健康后，节点变成小型聚合层，测试面与故障面显著扩大 |
| C4 | **契约缺口**：v1.0 无 `provider` / 上游健康语义 | 远程模型若进 `/v1/models`，需要新增可选字段；`owned_by`、`loaded`、`status` 的含义要重新定义 |
| C5 | **并发语义**：现有闸门是节点全局，`queue_len` 恒 0 | 远程上游有独立配额；需要按上游信号量，但不能改变既有 `max_concurrency=4`、`queue_len=0` 的默认契约 |
| C6 | **计费归属**：网关按令牌统计，节点自持上游 key | 节点侧消耗对网关不可见，账单与用量归属会失真 |
| C7 | **自用 vs 集群暴露**：蓝图允许「自用外部模型」，未允许「对外供给外部模型」 | 需要把「本地可见」与「集群可见」分成两种可见性，默认只允许本地回环调用 |

---

## 2. 目标与非目标

### 2.1 目标

1. 节点可在**不改动现有四端点契约**的前提下，把某个 OpenAI 兼容中转站接入为可选模型来源。
2. 调用方只持有节点 token；上游密钥由节点代持，任何日志、接口、错误体都不出现密钥。
3. 本地模型与远程模型在 `/v1/models`、`/v1/health` 中可区分、不冲突，旧客户端不受影响。
4. 上游不可达时节点不崩、不整体 `down`，故障影响被限制在对应模型/上游。
5. 提供从最小可用到完整形态的分阶段路径，每阶段有可验收边界。

### 2.2 非目标（本设计不承诺）

- 不在节点内做跨上游负载均衡、跨节点聚合或全局配额。
- 不替代碧游宫网关的渠道管理、令牌统计、计费。
- 不做模型输出转码、工具调用适配、OpenAI 不同子集的兼容修补——仍按契约「入参原样透传」。
- 不在本任务写任何运行时代码。

---

## 3. 配置结构

### 3.1 配置形态（新增段，缺省不配置时行为与现状完全一致）

```yaml
upstreams:
  - id: relay-a                       # 必填，唯一；同时作为模型命名前缀
    enabled: true                     # 默认 false；关闭时不探活、不暴露
    base_url: "https://relay-a.example.com/v1"   # OpenAI 兼容根，不含 /chat/completions
    api_key_env: "QUASHENG_UPSTREAM_RELAY_A_KEY" # 推荐：只写环境变量名
    # api_key: "sk-..."               # 仅本地开发允许；启动打印警告，所有输出一律脱敏
    connect_timeout_s: 10             # 建连超时，默认 10
    timeout_s: 120                    # 非流式总超时 / 流式空闲读超时，默认 120
    max_concurrency: 8                # 该上游独立信号量；不影响节点全局 max_concurrency
    visibility: local                 # local | cluster | both，默认 local
    data_leaves_lan: true             # 声明该上游会把 prompt 送出局域网，启动与 health 都会提示
    allow_insecure: false             # 仅本机/内网调试可开；公网必须 https
    discover: false                   # MVP 用静态清单；true 时从上游 /v1/models 拉取并缓存
    # allow_all_models: false         # P1 动态发现时是否允许白名单外模型，默认 false
    # retry_on_429: false            # P2 回退策略用，默认 false
    models:
      - id: gpt-4o-mini               # 上游真实模型 id
        capabilities: [chat]
        context_len: 128000
        fallback_model: null          # 可选的本地兜底模型 id；默认不回退
      - id: text-embedding-3-small
        capabilities: [embedding]
```

### 3.2 字段契约

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` | 是 | — | 上游唯一标识；正则 `[a-z0-9][a-z0-9_-]{0,31}`；保留字不能与本地模型首段冲突 |
| `enabled` | 否 | `false` | 关闭时完全不参与启动探活、模型列表与路由 |
| `base_url` | 是 | — | 必须是 `http(s)://host[:port]/v1` 形式；拒绝带 userinfo 的 URL |
| `api_key_env` | 二选一 | — | 启动时读取环境变量；缺失且 `enabled=true` 时启动失败 |
| `api_key` | 二选一 | `null` | 仅开发用；进入内存后包成 Secret 类型，禁止 `repr`/日志输出 |
| `connect_timeout_s` | 否 | `10` | 建连超时 |
| `timeout_s` | 否 | `120` | 非流式总超时；流式按「两个 chunk 之间的空闲时间」计 |
| `max_concurrency` | 否 | `4` | 该上游独立并发上限；满载立即 429，不排队 |
| `visibility` | 否 | `local` | `local` 仅回环来源可用；`cluster` 任意已鉴权调用方可用；`both` 两者皆可 |
| `data_leaves_lan` | 否 | `true` | 数据出网声明；只在 `visibility != local` 时为告警级，`local` 时为信息级 |
| `allow_insecure` | 否 | `false` | 允许 `http://`；仅建议回环 / 内网中转站使用 |
| `discover` | 否 | `false` | 是否从上游 `/v1/models` 动态发现；发现结果仍受 `models` 白名单约束 |
| `models[].id` | 是 | — | 上游模型 id，原样使用 |
| `models[].capabilities` | 否 | `[chat]` | 与本地 `capabilities_map` 同语义 |
| `models[].context_len` | 否 | `null` | 仅用于 health 展示，不参与路由 |
| `models[].fallback_model` | 否 | `null` | 可选本地兜底模型；见 §5.3 |

### 3.3 密钥规则（硬约束）

1. **只存节点侧**：密钥来自环境变量（推荐）或本地 `config.yaml`；`config.yaml` 已在忽略清单内，不得提交。
2. **不进日志**：`quasheng` logger 与 `logbuffer` 白名单不记录请求头/请求体；新增脱敏规则覆盖所有 `api_key_env` 解析出的值，以及 `Authorization`、`api_key`、`token`、`key` 字段。
3. **不进 `/v1/models`**：远程模型项只出现 `provider` 与模型 id，不出现 `base_url`、账户名、密钥指纹。
4. **不进 `/v1/health`**：上游段只出现 `id`、`status`、`active`、`max_concurrency`，不出现 URL 与密钥。
5. **不回传给调用方**：上游错误体先脱敏再包装成 OpenAI 错误形状；上游响应头一律不转发（避免 `x-api-key`、`set-cookie`、内部路由头泄漏）。
6. **不转发调用方 Authorization**：节点向上游发起的请求使用节点自持密钥，调用方的 bearer 只用于节点鉴权，与现有「不向 Ollama 转发客户端 Authorization」同一条原则。
7. **启动校验**：`enabled=true` 且密钥解析为空 → `ConfigError`，拒绝启动并打印明确中文提示（不打印密钥、不打印 URL 查询串）。
8. **内存表示**：建议引入 `Secret` 包装（`__repr__` 固定返回 `<redacted>`），避免异常栈或调试输出误带密钥。

---

## 4. 模型命名与发现

### 4.1 命名规则

| 来源 | 对外模型 id | 示例 |
|---|---|---|
| 本地 | 与 `ollama list` 完全一致，保持不变 | `qwen2.5:7b-instruct-q4_K_M` |
| 远程 | `<provider_id>/<upstream_model_id>` | `relay-a/gpt-4o-mini` |
| 上游 id 自带斜杠 | 首段固定为 provider，其余原样保留 | 上游 `openai/gpt-4o` → `relay-a/openai/gpt-4o` |

- **斜杠作为命名空间分隔符**：OpenAI 客户端普遍接受带 `/` 的 model 字符串；本地 Ollama 模型首段一般不是已配置的 provider id，冲突概率低。
- **启动期唯一性校验**：若本地模型 id 的首段与某个 upstream `id` 相同，或两个远程模型归一后相同 → `ConfigError`，避免静默覆盖。
- **不提供无前缀别名**：不把远程 `gpt-4o-mini` 直接暴露成裸名，避免与本地/其他上游冲突，也避免调用方误以为模型在本机。

### 4.2 `/v1/models` 共存形状

```json
{
  "object": "list",
  "data": [
    { "id": "qwen2.5:7b-instruct-q4_K_M", "object": "model", "owned_by": "biyou-node",
      "provider": null, "source": "local" },
    { "id": "relay-a/gpt-4o-mini", "object": "model", "owned_by": "biyou-node",
      "provider": "relay-a", "source": "upstream", "available": true }
  ]
}
```

- `owned_by` **统一保持 `biyou-node`**：它表达「该模型由本节点这个端点提供」，不是模型版权方；与契约 v1.0 §3.2 完全一致。
- 新增可选字段 `provider`、`source`、`available`；旧客户端忽略未知字段即可，不构成破坏性变更。
- `visibility=local` 的远程模型**不进入** `/v1/models`（避免集群发现一个调用方无权使用的模型）；本地 CLI 仍可按完整 id 调用。

### 4.3 `/v1/health` 的能力表达

`models[]` 保留 v1.0 字段，新增可选字段：

| 字段 | 本地模型 | 远程模型 | 说明 |
|---|---|---|---|
| `provider` | `null` | upstream id | 区分来源 |
| `source` | `local` | `upstream` | 显式来源 |
| `loaded` | 真实 | `false` | 远程不占本地显存，不能用 `true` 冒充 |
| `available` | 与本地一致 | 上游当前可达且模型在清单内 | 远程可服务性 |
| `upstream_status` | `null` | `ready` / `degraded` / `down` | 逐模型标注上游状态 |
| `data_leaves_lan` | `false` | 取自配置 | 数据边界提示 |

另在 health 顶层新增可选段：

```json
"upstreams": [
  { "id": "relay-a", "status": "ready", "active": 0, "max_concurrency": 8,
    "visibility": "local", "data_leaves_lan": true, "last_error": null }
]
```

`last_error` 只放脱敏后的错误码与短消息（如 `upstream_timeout`），不放开 URL、密钥、上游原始响应体。

### 4.4 发现策略

| 模式 | 行为 | 适用阶段 |
|---|---|---|
| 静态（`discover: false`，默认） | 只用 `models[]` 清单；启动不依赖上游可达；上游挂时模型标记 `available=false` | MVP |
| 动态（`discover: true`） | 启动/每 60s 拉 `{base_url}/models`；结果与静态白名单**取交集**，`allow_all_models` 未开时不允许新增未批准模型 | P1 |
| 动态全量（`allow_all_models: true`） | 上游返回什么就暴露什么；仅建议可信中转站 | 不推荐，需单独评审 |

- 动态发现失败**不得**阻塞节点启动，也不得清空已有静态清单；沿用上次成功快照并标记 `stale: true`。

---

## 5. 路由与回退

### 5.1 路由决策

| 请求 `model` | 条件 | 行为 |
|---|---|---|
| 精确命中本地模型 | 本地清单内 | 走现有本地代理路径（行为不变） |
| `<provider>/<rest>` | provider 已配置、`enabled=true`、模型在清单内、可见性允许 | 路由到该上游 |
| `<provider>/<rest>` | provider 未知 / 未启用 / 模型未批准 | `404 model_not_found` |
| `<provider>/<rest>` | 已知但 `visibility=local` 且来源非回环 | `404 model_not_found`（不泄漏存在性） |
| 裸名且非本地模型 | 维持现状 | 交给本地 Ollama，由其返回上游错误（不静默改道到远程） |

- **不做模糊匹配**：远程模型必须带 provider 前缀；避免「同名模型在本地与远程之间静默漂移」。
- **能力校验前置**：`/v1/embeddings` 路由到 `capabilities` 不含 `embedding` 的模型 → `501 capability_not_supported`，与本地行为一致。
- **可见性判断**：MVP 用回环来源（`request.client.host == 127.0.0.1/::1`）区分本地自用与集群调用；反向代理场景需显式 `trusted_proxy` 配置，MVP 不支持。长期改为 token scope（见 §7.3）。

### 5.2 上游错误映射

| 上游结果 | 节点返回 | code |
|---|---|---|
| 建连失败 / DNS / TLS | `503` | `upstream_unavailable` |
| 上游 401 / 403（节点密钥问题） | `503` | `upstream_auth_failed`（不回显上游原文） |
| 上游 429 | `429` + 透传 `Retry-After`（若有） | `upstream_rate_limited` |
| 上游 400 | `400` | `upstream_bad_request`（消息脱敏截断） |
| 上游 5xx | `502`/`503` | `upstream_error` |
| 上游超时 | `504` | `upstream_timeout` |
| 上游返回非 OpenAI 形状 | `502` | `upstream_invalid_response` |

所有错误仍用现有 OpenAI 错误体：`{"error":{"message","type","param","code"}}`，并带节点响应头。

### 5.3 回退策略

| 策略 | 默认 | 触发条件 | 上限 |
|---|---|---|---|
| `none` | ✅ 默认 | 任何失败 | — |
| 本地兜底（`fallback_model`） | 关闭 | 建连失败 / 5xx / 首次字节前超时；且本地兜底模型存在、能力匹配 | **最多 1 次**，不递归 |
| 其他上游 | 关闭 | 仅 P2 提供显式链；同一模型语义可替代 | **最多 1 跳** |

硬性规则：

1. **首字节之后禁止回退**：一旦已向调用方写过任何 SSE chunk，不得切上游或本地，只能按 §5.4 报错。
2. **4xx 不盲目回退**：`400`（请求本身问题）、`401/403`（密钥/权限）不回退；`429` 仅在显式 `retry_on_429: true` 且未发出首字节时允许一次。
3. **回退不改变模型身份**：回退到本地模型时，响应仍按原请求的远程 model id 语义返回；日志记录 `fallback_from` / `fallback_to`，不向调用方伪装模型来源（可在响应头加 `X-Node-Fallback: local`）。
4. **回退有总预算**：单请求最多 1 次回退、总耗时不得超过 `timeout_s`；超预算直接报错。

### 5.4 流式失败语义

| 阶段 | 行为 |
|---|---|
| 尚未返回 200/SSE 头 | 返回标准非 2xx JSON 错误（上表），调用方按普通 HTTP 错误处理 |
| 已开始 SSE，上游中途断流/超时 | 发送一帧 `data: {"error":{"message":"上游中断","type":"server_error","param":null,"code":"upstream_stream_aborted"}}`，随后关闭连接；**不发送 `data: [DONE]`** |
| 调用方主动断开 | 关闭上游流、释放并发配额；行为与现有断连取消一致 |

- 明确写入契约补充说明：**没有 `[DONE]` 的流视为未完成**；客户端必须把显式 error 帧或连接提前关闭视为失败，不能把已收到的部分内容当成功。
- 不在 SSE 中插入自定义 `event:` 名称，保持与现有 `data:` 解析器兼容；错误帧仍是一条合法 `data:` JSON。

---

## 6. 降级与健康

### 6.1 `status` 判定

| 本地运行时 | 已暴露上游 | `node.status` | 说明 |
|---|---|---|---|
| 正常 | 全部正常 / 未配置 | `ready` | 与现状一致 |
| 正常 | 部分或全部不可用 | `degraded` | 本地模型仍可服务；逐模型标 `available=false` |
| 不可用 | 至少一个可用 | `degraded` | 远程仍可服务，但节点基线能力受损 |
| 不可用 | 无可用远程 / 未配置 | `down` | 与现状一致（本地不可用时节点不可服务） |

**原则：上游故障不得单独把节点判成 `down`**，否则网关会把仍能提供本地 embedding/chat 的节点整体剔除，放大故障。

### 6.2 上游探测与熔断

- **被动优先**：请求失败即更新该上游最近状态，不额外消耗上游配额。
- **主动探活**：仅对 `enabled=true` 且已暴露的上游，每 30s 调一次轻量 `GET /v1/models`；不做推理探活。
- **熔断**：连续 3 次失败进入 open（默认 30s），期间直接快速失败/走回退；半开状态用一次 `/v1/models` 试探。
- **429 视为 `degraded` 而非 `down`**；遵守 `Retry-After`，期间不打满配额。

### 6.3 并发闸门

| 层 | 归属 | 默认 | 满载行为 |
|---|---|---|---|
| 节点全局闸门 | 现有 `max_concurrency` | **4，不改** | 立即 `429` + `Retry-After`；`queue_len` 仍恒 0 |
| 每上游信号量 | 新增 `upstreams[].max_concurrency` | 4 | 立即 `429` + `Retry-After`，code `upstream_capacity`；不排队 |

- `resources.active` 继续表示节点在途请求总数（本地 + 远程）；新增 `upstreams[].active` 表示分上游在途。
- **不改变**既有队列/并发默认值，也不引入本地排队。

---

## 7. 鉴权与安全

### 7.1 令牌边界

```
调用方 ──(节点 shared_token)──▶ 节点 ──(节点自持 upstream key)──▶ 中转站
```

- 调用方永远拿不到上游密钥；上游只信任节点这一跳。
- 上游 key 与节点 token 互不复用；节点 token 泄漏不等于上游账户泄漏。
- 日志/错误体/模型列表/health 四处的泄漏面见 §3.3。

### 7.2 模型授权

| 阶段 | 机制 |
|---|---|
| MVP | 单节点 token；远程模型用 `visibility` 控制「仅本地 / 集群 / 两者」；`local` 靠回环来源判断 |
| P1 | 多 token + `allowed_models`：每个 token 声明可访问的模型集合（本地与远程同一套匹配规则）；未授权 → `404 model_not_found` |
| 长期 | 与网关令牌体系对齐：网关下发 token scope，节点只做执行点 |

### 7.3 网络安全

| 风险 | 约束 |
|---|---|
| SSRF | `base_url` 必须是显式配置，不接受调用方传入；可选 `allowed_upstream_hosts` 白名单；拒绝云元数据地址段 |
| 明文传输 | 公网必须 `https`；`allow_insecure=true` 仅限回环 / 私网，启动打印警告 |
| 重定向 | 不自动跟随跨主机 3xx；避免密钥被带到重定向目标 |
| 响应膨胀 | 非流式响应体设置上限（如 16MB）；流式按空闲超时中断 |
| 数据出网 | `data_leaves_lan=true` 在启动日志与 health 中明示；集群可见的远程模型必须逐条显式批准 |
| 请求体泄漏 | 继续不记录请求 body；上游错误消息做密钥/URL/头字段脱敏后再落日志与返回 |

---

## 8. 与现有契约的兼容性

| 变更 | 类型 | 兼容性 | 说明 |
|---|---|---|---|
| `config.yaml` 新增 `upstreams` 段 | 新增 | ✅ 完全兼容 | 不配置时行为与现在一致 |
| `/v1/models` 新增 `provider` / `source` / `available` | 新增可选字段 | ✅ 向后兼容 | OpenAI 客户端忽略未知字段 |
| `/v1/health` `models[]` 新增同名可选字段 | 新增可选字段 | ✅ 向后兼容 | 旧消费者只读已知字段 |
| `/v1/health` 新增 `upstreams[]` | 新增可选段 | ✅ 向后兼容 | 未配置时为空或省略 |
| `node.status` 增加「上游故障 → degraded」 | 语义扩展 | ⚠️ 行为变化（仅配置上游后） | 不配置上游时判定完全不变 |
| 远程模型 id 带 provider 前缀 | 新增命名空间 | ✅ 不冲突 | 本地 id 保持不变 |
| `owned_by` 保持 `biyou-node` | 不变 | ✅ | 不用 `relay-a` 作为 owned_by |
| 新增错误码 `model_not_found` / `upstream_*` | 新增 | ✅ | 错误体形状不变；旧客户端按 `code` 兼容 |
| 流式新增 error data 帧 | 新增语义 | ⚠️ 需客户端配合 | 明确「无 `[DONE]` 即失败」；不改变成功流形状 |

**契约版本建议**：这些字段全部可选、旧行为不变，可继续标 v1.0，并在契约文档加「v1.1 草案：上游桥接可选字段」。若未来要求 `provider` 必填或改 `owned_by`，才升 v1.1 主版本；本设计**不提议**任何破坏性变更。

---

## 9. 分阶段落地

### P0 · 最小可用（单上游 + 静态清单 + 直通）

范围：

- 单 `upstream`，`discover=false`，只支持 `chat`（流式 + 非流式）；`embedding` 可选但同一路由骨架。
- 远程模型 `relay-a/<model>`，默认 `visibility=local`，不进入 `/v1/models`；本地 CLI 可用。
- 密钥只从环境变量读取；四处不泄漏；上游错误映射与流式错误帧。
- health 增加 `provider/source/available/upstream_status/data_leaves_lan` 与 `upstreams[]`；`status` 按 §6.1。
- 每上游 `max_concurrency`（默认 4），全局并发/队列默认值不动。
- 无回退（`fallback_model=null`）。

验收：

1. `relay-a/gpt-4o-mini` 流式请求逐 chunk 原样透传，以 `[DONE]` 结束；非流式形状与上游一致。
2. 本地模型全部既有用例不回归；不配置 `upstreams` 时 `/v1/health`、`/v1/models` 与现状逐字段一致。
3. 上游不可达：远程模型 `available=false`、节点 `status=degraded`（本地正常时）；本地模型仍可服务。
4. 上游 429：节点返回 429 + `Retry-After`，code `upstream_rate_limited`，不重试。
5. 密钥泄漏专项：日志、`/v1/models`、`/v1/health`、错误体、响应头扫描均不出现密钥或 `base_url`。
6. `visibility=local` 时非回环来源请求远程模型返回 404，且不泄漏模型存在性。
7. 客户端断连：上游流被中止，节点在途配额释放。

### P1 · 多上游 + 动态发现 + 授权

- 多 `upstreams`；每上游独立并发与熔断。
- `discover=true` + 白名单取交集，缓存 60s，失败沿用旧快照。
- 多 token + `allowed_models`；远程模型按 token 授权。
- `/v1/health` 增加逐上游延迟/最近错误（仍脱敏）。

验收：两个上游同名模型不冲突；一个上游挂不影响另一个；未授权模型 404；发现失败不阻塞启动。

### P2 · 回退 + 用量 + 可观测

- 显式回退链（最多 1 跳），仅首字节前；`retry_on_429` 可选。
- 用量统计：按上游记录请求数、token 数（若上游返回 `usage`）、耗时、失败码；不记录 prompt 内容。
- 灵脉指标导出（可选）：上游成功率、429 率、P95 延迟。
- 流式中途错误帧纳入契约补充与 CLI 展示。

验收：回退只发生一次且可审计；用量字段缺失时不报错；指标可在日志/health 观察。

### P3 · 与网关分工评估（不写实现）

- 对比「节点单上游桥接」与「全部上游收敛为 NewAPI 渠道」两条路径的成本、密钥治理、计费归属、故障半径。
- 若集群需要统一计费/配额/模型目录，**优先收敛到网关**；节点仅保留 `visibility=local` 的自用桥接。

---

## 10. 取舍与风险

| 风险 / 取舍 | 影响 | 建议 |
|---|---|---|
| **与「薄代理」原则冲突** | 节点加路由、回退、上游并发后，代码面与故障面扩大；与「节点只代理本机 Ollama」的定位偏离 | 限制为「单上游 + 直通 + 无跨上游聚合」；多上游与回退只放在网关/阵图 |
| **数据出网与蓝图硬边界冲突** | 「数据不出金鳌岛」被破坏，合规与隐私风险 | 默认 `visibility=local`；集群可见远程模型逐条审批；health/启动明示 `data_leaves_lan` |
| **上游被墙 / 限流** | 首 token 慢、429、流中断，体验不稳定 | 建连/空闲超时分离；429 透传 `Retry-After`；熔断快速失败；可选本地兜底；不做静默重试 |
| **计费与用量归属** | 节点自持 key，网关账单看不到；多节点重复 key 难以对账 | 集群用途优先由网关持 key；节点 key 只用于本地自用；P2 再做按上游用量统计，并注明「不等于账单」 |
| **每个节点各接一套的重复建设** | 密钥分散、配置漂移、限流策略不一致、排障困难 | 若目标是集群能力，统一在碧游宫建渠道；节点桥接只服务本机自用 |
| **嵌套聚合 / 双重回退** | 节点回退 + 网关重试 + 阵图降级叠加，故障放大、难以归因 | 明确「回退只允许一层」；节点回退默认关闭；网关负责集群级重试 |
| **上游 OpenAI 子集差异** | 字段缺失、usage 为空、错误形状不同 | 继续原样透传；只做错误形状归一与脱敏；对 `usage` 做 `None` 保护 |
| **模型命名冲突** | 本地/远程同名、上游 id 带斜杠 | 强制 provider 前缀；启动期唯一性校验；不做裸名别名 |
| **密钥泄漏面** | 日志、错误体、health、models 任一处理不当即泄漏 | 统一 Secret 包装 + 集中脱敏 + 专项泄漏测试（P0 验收项） |
| **可见性靠回环判断** | 反向代理/容器网络下来源可能失真 | MVP 明示不支持反代；P1 改 token scope 作为正式授权 |

---

## 11. 建议的最小第一步

**只做 P0 的一个纵切，不做多上游、不做回退、不做动态发现。**

1. **配置**：`config.yaml` 增加单个 `upstreams[0]`（`id/base_url/api_key_env/timeout/max_concurrency/models/visibility=local`）；不配置时行为完全不变。
2. **密钥**：只读环境变量；引入 `Secret` 包装与集中脱敏；启动校验缺失即失败。
3. **路由**：仅 `<provider>/<model>` 前缀命中远程；精确本地模型走原路径；未知/未授权统一 `404 model_not_found`。
4. **透传**：chat 流式/非流式原样转发；错误映射见 §5.2；流式中断发 error 帧、不发 `[DONE]`。
5. **健康**：`models[]`/`upstreams[]` 加可选字段；上游故障只降为 `degraded`；全局并发与 `queue_len` 默认值不动。
6. **测试**：新增 mock 上游（可复用 `tests/mock_ollama.py` 思路）覆盖：成功直通、上游 429、上游断连、流式中断、密钥不泄漏、非回环 404、本地全量回归。

**P0 完成即可回答的问题**：节点能否在不破坏现有契约与本地能力的前提下，安全地把一个中转站模型当作「本机自用模型」使用。多上游与集群化留到网关侧评审后再决定。
