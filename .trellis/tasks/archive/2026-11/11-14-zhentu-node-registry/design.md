# 设计：阵图节点注册与心跳

## 边界

- NewAPI（3000）继续只做渠道转发；Registry（4020）是阵图第一个旁路组件。
- Registry 只存节点元数据：node、endpoint、status、models/capabilities、limits、resources、lease。不存 token、prompt、权重。
- 节点侧只有出站 reporter，不新增任何入站端点；契约 v1.0 的四个端点行为不变。

## 拓扑

```
准圣 :4010 ─┐  register / heartbeat   ┌─→ zhentu Registry :4020 (SQLite)
散仙 :4011 ─┼────────────────────────→│
            │                          └─→ GET nodes / capabilities / candidates
NewAPI :3000 ── 渠道 ollama-local ──→ Ollama（不变）
```

## 数据模型

SQLite `nodes` 表，主键 `node_id`：

- `node_id, role, name, endpoint, boot_id, status, contract_version`
- `payload_json`（注册快照）、`models_json`、`capabilities_json`、`models_rev`
- `heartbeat_interval_s, ttl_s, revision`
- `registered_at_ms, first_seen_ms, last_seen_ms, deregistered_at_ms`

租约在读取时计算：`lease.state = online | offline | deregistered`，`age_s = now - last_seen`。不做后台清扫。

## 协议

### 注册

`POST /zhentu/v1/nodes/register`，Bearer 鉴权，`X-Node-Id` / `X-Node-Role` 与 body 交叉校验。

```json
{
  "node": {"id":"quasheng-01","name":"准圣·01","role":"quasheng","arch":"x86_64",
           "runtime":"ollama","runtime_version":"0.5.x","contract_version":"1.0"},
  "endpoint": "http://192.168.1.20:4010",
  "status": "ready",
  "models": [{"name":"qwen2.5:7b-instruct-q4_K_M","capabilities":["chat"],
              "context_len":32768,"loaded":true}],
  "limits": {"max_concurrency":4,"max_context_len":32768},
  "resources": {"queue_len":0,"active":0,"cpu_pct":12.5,"battery_pct":null,
                "temperature_c":null,"thermal_state":null},
  "heartbeat_interval_s": 15,
  "boot_id": "uuid",
  "models_rev": "sha256:...",
  "sent_at": "ISO-8601"
}
```

响应：`{ok, node_id, revision, lease:{ttl_s, expires_at}, server_time, registry_version}`。

### 心跳

`POST /zhentu/v1/nodes/{node_id}/heartbeat`

```json
{"boot_id":"uuid","status":"ready","models_rev":"sha256:...",
 "loaded_models":["..."],"resources":{"queue_len":0},
 "limits":{"max_concurrency":4},"sent_at":"ISO-8601"}
```

- 未知节点 → 404 `node_unknown`；boot_id 不一致 → 409 `boot_mismatch`；两者都触发节点重新注册。
- `models_rev` 不一致 → 200 + `need_snapshot: true`，节点立即补注册。
- 心跳只刷新 `last_seen/status/resources/loaded`，不改注册快照。

### 租约

- 准圣 interval 15s，散仙 30s；允许 10–120s。
- `ttl = clamp(3 × interval, role_floor, 600)`；quasheng floor 90s，sanxian floor 180s。
- 服务器时钟为准；节点 `sent_at` 仅展示。

### 能力

- 模型级 `capabilities`；规范名 `chat|embedding|vision|ocr|rerank`，输入把 `embeddings` 归一为 `embedding`。
- `routable = lease.online && status ∈ {ready, degraded}`。
- `loaded` 与 `routable` 分开，支持按需加载模型。

### 查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/zhentu/v1/nodes` | `online/role/status/capability/model/include_offline/limit` |
| GET | `/zhentu/v1/nodes/{id}` | 详情 + 租约 |
| GET | `/zhentu/v1/capabilities` | 按能力聚合在线节点与模型 |
| GET | `/zhentu/v1/candidates` | 按能力/模型返回候选，ready → queue_len → 空闲率排序 |
| GET | `/zhentu/v1/health` | 注册表自身健康与在线数 |

## 安全

- `ZHENTU_REGISTRY_TOKEN` 只从环境变量读取；非回环监听且为空拒绝启动。
- 常量时间比较；不记录 token；`endpoint` 拒绝用户名/密码/查询串/片段；注册体上限 64KB。
- 错误统一 OpenAI 形状。

## 节点接入

### 准圣

- `config.py`：`registry` 段（enabled/base_url/token_env/heartbeat_interval_s/public_endpoint/timeout_s），默认关闭。
- `registry.py`：独立 httpx 客户端 + 后台循环；注册成功转心跳，`need_snapshot` 补注册；失败指数退避 1→60s。
- `app.py`：lifespan 中 `create_task`，**不 await**；停机取消并 best-effort deregister（≤1s）；health 仅在启用时追加 `registry` 块。
- reporter 快照复用 `/api/tags`、`/api/ps` 与 `capability_for`，不新增事实来源。

### 散仙

- `NodeRegistryReporter`（androidMain），`NodeForegroundService` 在 server 启动后拉起、停止前取消。
- `NodeConfig` 增加 `registry_url` / `registry_heartbeat_interval_s` / `registry_public_endpoint`；token 优先 DataStore，回退 `shared_token`。
- 上报 battery/temperature/thermal；endpoint 由 NetworkInterface 取 LAN IPv4，取不到时用配置值。

## 回退链

P1 只查询；P2 增加 `fallback_source=registry`：失败后查 candidates，仅保留配置的 NewAPI 上游快照中存在的模型，拼成 `newapi/<model>`；300ms 超时、10s 缓存，失败回退静态链条。不做节点间直连。
