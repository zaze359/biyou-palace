# 阵图编排（P3）设计

状态：已确认（P3.0 + P3.1；第 10 节 4 项按推荐方案锁定）
范围：单次请求的单跳选路（决定用哪个节点的哪个模型），不做多步任务编排
前置：P1 注册/心跳/查询、P2 `fallback_source=registry` 已落地

## 0. 结论先行

推荐 **决策面方案**：阵图只返回排序后的路由决策，不代理请求；执行仍由调用方经 NewAPI 完成。

- 阵图新增 `POST /zhentu/v1/route`，返回 `selected` + `alternates` + `rejected`，带 `decision_id` 与 `ttl_ms`。
- 现有 `GET /zhentu/v1/candidates` 保留不动，`/route` 是增量接口。
- P3 只做单跳；多步编排留到 P4。
- 阵图不持有任何节点/上游凭据，不做数据面转发；NewAPI 的计费、配额、渠道与重试职责不变。
- P3 的消费方先接准圣（复用 `fallback_source=registry`）；散仙和外部客户端后续接入。
- 默认行为不变：`fallback_source=static` 时节点不调用 `/route`。

## 1. 编排边界：决策面 vs 代理

### 方案 A：决策面（推荐）

调用方在请求失败或需要选路时调用阵图，拿到「节点 + 模型 + 备选 + 决策有效期」，再由调用方通过 NewAPI 用具体模型 id 执行。

代价：
- 阵图只承担控制面，数据面零改动；NewAPI 的计费、配额、渠道、鉴权完全保留。
- 阵图故障只影响「下一次决策」，不影响已有转发链路；节点可用 P2 的静态链条或 10s 缓存兜底。
- 需要调用方改造来消费决策；P3 先改准圣，外部客户端/散仙后续接入。
- 决策与执行之间存在时间窗（节点可能在执行前掉线），必须靠 `ttl_ms`、备选与执行侧重路由兜住。

### 方案 B：阵图作为入口代理

阵图暴露 OpenAI 兼容入口，收到请求后直接转发到选中节点。

代价：
- 需要集中保存节点凭据或要求节点互信，违反「不引入节点间凭据集中存储」。
- 绕过 NewAPI，计费、配额、API key、渠道日志全部失效；若在阵图里重做，等于重复实现网关。
- 阵图进入数据面，成为新的单点故障与带宽瓶颈；阵图故障将直接打断推理请求，违反「阵图故障不得影响现有转发链路」。
- 需要自己处理 SSE、超时、断连、背压、重试、审计，和 NewAPI 高度重复。
- 节点 endpoint 多为内网地址，暴露给阵图做直连还会扩大网络与安全边界。

结论：P3 不采用 B。若未来确需全局入口，应优先评估「NewAPI 插件/渠道适配」，而不是让阵图做代理。

## 2. 与 NewAPI 的分工

| 层 | 职责 | P3 是否变化 |
|---|---|---|
| 阵图 | 节点注册、租约、能力视图、排序决策、决策审计 | 新增 `/route`，仍是控制面 |
| NewAPI | API key、配额、计费、渠道选择、上游重试、日志 | 不变 |
| 节点 | 本地推理、健康上报、按模型服务 | 准圣新增消费 `/route`；散仙补一个可选负载字段 |
| 调用方 | 发起请求、按决策选择具体模型 id、执行回退 | 准圣先接；外部客户端后续 |

- 决策结果必须能表达成 NewAPI 可接受的模型 id。P3 沿用 P2 约定：调用方用自己配置的 NewAPI 上游快照做交集，拼成 `newapi/<model>`；阵图不感知 NewAPI 的渠道表，也不保存渠道密钥。
- 阵图返回 `node_id`/`model`，不返回凭据；`endpoint` 默认不返回，仅运维查询节点详情时可见。
- 计费与配额仍发生在 NewAPI；阵图只记录「选了谁、为什么」，便于审计，不参与账单。

## 3. 决策依据

### 3.1 现有可用字段

| 字段 | 来源 | 用途 | 缺失时 |
|---|---|---|---|
| `lease.state` / `age_s` / `ttl_s` | 阵图租约 | 硬过滤离线；新鲜度评分 | 不可能缺失；离线直接排除 |
| `status` | 节点 health | `ready` 优先，`degraded` 降权，`down` 排除，`loading` 默认排除 | 按 `down` 处理 |
| `models[].capabilities` | 节点上报 | 按 `chat/embedding/vision/ocr/rerank` 硬过滤 | 无匹配能力即排除 |
| `models[].name` | 节点上报 | 精确模型匹配、`allowed_models` 交集 | 无匹配即排除 |
| `models[].context_len` | 节点上报 | 请求上下文长度硬过滤 | 视为未知，不因长度排除 |
| `models[].loaded` | 心跳 | 已加载强偏好 | 未上报按未加载处理 |
| `resources.queue_len` | 节点 health | 负载评分 | 按 0 处理并标记 `missing` |
| `resources.active` | 准圣 health；散仙当前未上报 | 在途并发/利用率 | 散仙按 `queue_len` 估算；P3 可选补字段 |
| `limits.max_concurrency` | 节点 health | 计算利用率/余量 | 缺失时用 `queue_len/8` 的保守估算 |
| `limits.effective_max_concurrency` | 散仙降载时提供 | 降载后的真实并发上限 | 回退到 `max_concurrency` |
| `resources.thermal_shedding` | 散仙 | 降载中直接排除（可配置放行） | 视为 false |
| `resources.temperature_c` | 散仙电池温度 | 热惩罚；仅作代理指标 | 中性 |
| `resources.battery_pct` | 散仙 | 低电量降权 | 中性 |
| `resources.thermal_state` | 散仙 | `warn/severe` 降权 | 中性 |
| `role` | 注册 | 策略偏好（准圣/散仙） | 中性 |

说明：散仙的 `temperature_c` 是电池温度，不是 CPU/GPU 温度；真正强信号是 `thermal_shedding` 与 `thermal_state`，温度只做辅助。

### 3.2 硬过滤（不参与打分）

1. 租约非 `online`、或已注销。
2. `status=down`；`loading` 仅在 `allow_loading=true` 时放行；`degraded` 默认放行。
3. 能力不匹配。
4. 指定 `model` 时模型名不匹配；指定 `allowed_models` 时不在交集内。
5. `context_len` 已知且小于请求值。
6. 节点或模型在 `exclude_nodes` / `exclude_models` 中。
7. `thermal_shedding=true`（除非 `allow_shedding=true`）。
8. `limit` 明确为 0。

### 3.3 软评分（0..1，加权）

| 分量 | 计算 | 默认权重 |
|---|---|---|
| `load` | `1 - min(1, (active + queue_len) / effective_max_concurrency)` | 0.35 |
| `freshness` | `1 - age_s / ttl_s`（clamp 0..1） | 0.15 |
| `loaded` | 已加载 1.0；未加载 0.4 | 0.20 |
| `thermal` | 温度 ≤45℃ 1.0，45–55℃ 线性降到 0；电量 >30% 1.0，10–30% 0.6，<10% 0.2；取较低值 | 0.15 |
| `role` | 由策略决定（见下） | 0.10 |
| `headroom` | `max(0, (max - active - queue_len) / max)` | 0.05 |

- 缺失字段取中性 0.5，不改变权重、不报错。
- `score` 保留 4 位小数，便于测试与审计。
- 排序：`score` 降序 → `queue_len` 升序 → `last_seen` 降序 → `node_id` → `model`，保证确定性。
- 同分不随机，避免同一请求在不同阵图实例上抖动。

策略预设：

| 策略 | 变化 | 角色偏好 |
|---|---|---|
| `balanced`（默认） | 使用上表权重 | 准圣 0.6 / 散仙 0.5 |
| `latency` | 提高 `load`、`freshness` | 准圣 0.9 / 散仙 0.6 |
| `local_first` | 提高 `role` 权重 | 准圣 1.0 / 散仙 0.3 |
| `power_saving` | 提高 `thermal` 权重 | 准圣 1.0 / 散仙 0.2 |

权重放在阵图配置里，P3 先不开放逐请求自定义权重；逐请求只允许选 `strategy`。

### 3.4 缺数据退化原则

- 缺 `active`：用 `queue_len` 估算；散仙 P3 可选补 `active`。
- 缺 `max_concurrency`：用 `queue_len / 8` 兜底，并在 `reasons` 标 `estimated_load`。
- 缺温度/电量：热分量取 0.5。
- 缺 `context_len`：不因长度排除，`reasons` 标 `context_unknown`。
- 全部字段都缺：退化为 `status` + `loaded` + 租约新鲜度 + 字典序，仍能给出确定结果。

## 4. API 设计

### 4.1 `POST /zhentu/v1/route`

鉴权：与现有 `/zhentu/v1/*` 相同，Bearer `ZHENTU_REGISTRY_TOKEN`。

请求：

```json
{
  "capability": "chat",
  "model": "qwen2.5:7b",
  "context_len": 8192,
  "strategy": "balanced",
  "max_candidates": 3,
  "exclude_nodes": ["quasheng-01"],
  "exclude_models": ["newapi/qwen2.5:0.5b"],
  "allowed_models": ["qwen2.5:7b", "qwen2.5:0.5b"],
  "allow_loading": false,
  "allow_shedding": false,
  "request_id": "req-..."
}
```

- `capability` 默认 `chat`，按现有别名归一。
- `model`、`context_len`、`allowed_models`、`exclude_*` 均可选。
- `allowed_models` 最多 200 项，超出返回 400 `too_many_models`。
- `max_candidates` 默认 3，范围 1..10。
- `strategy` 非法返回 400 `invalid_strategy`。

响应 200：

```json
{
  "registry_version": "1",
  "server_time": "2026-11-16T12:00:00+08:00",
  "decision_id": "dec-...",
  "decided_at": "2026-11-16T12:00:00+08:00",
  "ttl_ms": 5000,
  "strategy": "balanced",
  "selected": {
    "node_id": "quasheng-01",
    "node_name": "准圣一号",
    "role": "quasheng",
    "model": "qwen2.5:7b",
    "capabilities": ["chat"],
    "score": 0.8731,
    "reasons": ["loaded", "queue=0", "util=0.0", "lease_age=3s"]
  },
  "alternates": [],
  "rejected": [
    {"node_id": "sanxian-01", "model": "Qwen3-0.6B", "reason": "thermal_shedding"}
  ]
}
```

- `selected=null` 表示无候选；响应仍是 200，附 `reason` 与 `rejected`。
- `rejected` 只给脱敏原因码，不含 endpoint、错误详情或凭据。
- 默认不返回 `endpoint`；如需排障，走现有 `GET /zhentu/v1/nodes/{id}`。
- `ttl_ms` 默认 5000，可按配置调整；执行方应在 TTL 内使用决策，失败后重新决策。

### 4.2 兼容性

- `GET /zhentu/v1/candidates` 保持不变，P2 的准圣继续可用。
- `/route` 是新增只读决策接口，不改变注册/心跳/注销协议。
- 决策不落库；只在内存中计算，避免 SQLite 写放大。审计走日志/指标。

### 4.3 错误语义

| 状态 | code | 场景 |
|---|---|---|
| 400 | `invalid_capability` / `invalid_strategy` / `too_many_models` | 参数非法 |
| 401 | `invalid_token` | 鉴权失败 |
| 200 + `selected=null` | `no_route` | 没有可路由候选 |
| 503 | `registry_unavailable` | 阵图自身存储异常 |

「无候选」不是 5xx：调用方能区分「阵图说没有」和「阵图挂了」。

## 5. 多步编排

P3 不做「先 chat 再 embedding 再组合」的任务级编排，只做单次请求的单跳选路。

原因：
- 多步需要任务图、步骤间产物存储、幂等、重试/补偿、成本归集与超时预算，属于执行面能力，不应塞进注册表。
- 阵图做多步会自然滑向代理/执行器，重新引入凭据与数据面单点。
- 单跳已经能解决「选哪个节点/模型」的核心问题，收益/风险比最高。

P4 可新增 `POST /zhentu/v1/plan`：只返回步骤 DAG（每步 capability、依赖、候选），由独立执行器执行；阵图仍不碰请求体与凭据。

## 6. 失败与降级

| 场景 | 处理 |
|---|---|
| 选中节点在请求中途掉线 | 执行方看到可回退错误后，带 `exclude_nodes`/`exclude_models` 重新调用 `/route`，拿到下一个候选再经 NewAPI 重试；4xx/401 不重路由，直接暴露。 |
| 阵图不可用 | 执行方沿用 P2：300ms 超时、10s 缓存（含失败），回退 `fallback_chain`；本地能力、health、现有转发不受影响。 |
| 候选全不可路由 | `/route` 返回 `selected=null` + `rejected`；执行方保留原始上游错误（P2 行为），不把阵图抖动改写成新的错误语义。 |
| 决策过期 | `ttl_ms` 到期后执行方重新决策；过期决策仍可作为备选参考，但必须重新校验租约。 |
| 节点刚被选中就进入降载 | 执行方收到 `503 thermal_shedding` 后按可回退错误处理，排除该节点并重新决策；阵图下一次心跳会更新 `thermal_shedding`。 |
| 阵图决策与实际能力不一致 | 以执行结果为准；阵图只做建议，不保证成功。执行方的重路由与回退是最终兜底。 |

不在 P3 做：全局熔断、跨请求失败记忆、反馈闭环。P3 依赖租约过期 + 单请求排除；若实测「死节点被反复选中」，P4 再加 `POST /route/feedback` 与短期失败缓存。

## 7. 节点侧改动

### 准圣

- 新增 `/route` 客户端调用（出站，300ms 超时、5s 决策缓存，失败回退 P2 静态链条）。
- `fallback_source=registry` 时优先使用 `/route` 的 `selected` + `alternates`，仍用本地 NewAPI 上游快照做 `newapi/<model>` 交集。
- 回退成功时沿用 `X-Node-Fallback-Source` / `X-Node-Fallback-Origin`，可追加 `X-Node-Route-Decision`（`decision_id`）便于审计。
- 不新增入站端点，不改变契约。

### 散仙

- 无新增入站/出站协议；它继续只做注册与心跳。
- 可选：在 `Resources` 中补 `active`（当前只有 `queue_len`），让负载评分更准确；字段可选，旧阵图忽略即可。
- 可选：后续补 `charging` 以区分「低电量但在充电」；P3 不做。

### 共同

- 不向阵图提交 prompt、响应、API key 或节点凭据。
- 不要求节点之间互信；节点仍只信任 NewAPI/调用方。
- 阵图故障时节点启动耗时、本地 chat/embeddings、health 与现状一致。

## 8. 可观测性

阵图侧：
- 日志：`decision_id`、`capability`、`strategy`、`selected.node_id/model`、`score`、`rejected` 数量、决策耗时；不记录 prompt。
- 指标：`zhentu_route_total{strategy,result}`、`zhentu_route_duration_seconds`、`zhentu_route_rejected_total{reason}`。
- 可选：`zhentu_route_selected_total{node_id,model}`，用于观察流量倾斜。

准圣侧：
- `/v1/logs` 的 `fallback` 事件追加 `decision_id` 与 `node_id`（仅 registry 模式）。
- 现有 `X-Node-Fallback-Source`、`X-Node-Fallback-Origin` 保持不变。

## 9. 分期建议

| 阶段 | 内容 | 默认行为 |
|---|---|---|
| P3.0 | `/route` 接口、评分、过滤、单测；`/candidates` 不动 | 无变化，纯增量 |
| P3.1 | 准圣消费 `/route`；`fallback_source=registry` 下启用；真实进程验证 | 默认 `static`，不调用 `/route` |
| P3.2 | 散仙补 `active`；阵图指标/README/契约 v1.2 | 字段可选，旧节点兼容 |
| P4 | 多步 `/plan`、反馈/熔断、客户端 SDK、NewAPI 适配方案 | 另立设计 |

## 10. 待确认点（最少 4 个）

1. **决策面 vs 代理**：确认 P3 只做决策面，执行仍经 NewAPI；不引入节点直连与凭据集中存储。
2. **单跳范围**：确认 P3 不做多步编排，多步留 P4。
3. **评分默认值**：确认默认 `balanced` + 上述权重；`strategy` 允许逐请求选择，但权重不逐请求开放。
4. **散仙字段**：确认 P3.2 给散仙补可选 `active`；暂不补 `charging`，也不做反馈/熔断（P4）。

## 11. P3 验收

- `/route` 在真实阵图进程返回稳定排序；相同输入多次调用结果一致。
- 缺 `active`、缺 `max_concurrency`、缺温度/电量时仍能返回结果，不 500。
- 准圣 `fallback_source=registry` 时可消费 `/route`，执行仍走 NewAPI；响应头/日志可看出 `decision_id`。
- 停掉阵图后，准圣请求仍可用静态链条完成；本地 chat/embeddings/health 不受影响。
- 无候选时返回 `selected=null`，准圣保留原始上游错误。
- 阵图中不出现任何上游 key、节点 token 或 prompt。
