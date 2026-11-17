# 阵图 · 节点注册表（zhentu registry）

节点存在性、心跳租约与能力索引的旁路服务。它不参与推理转发，也不修改 NewAPI；
NewAPI（默认 `127.0.0.1:3000`）继续只做渠道转发。

## 启动

```bash
export ZHENTU_REGISTRY_TOKEN='<cluster-token>'
bash scripts/run_zhentu.sh
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ZHENTU_HOST` | `0.0.0.0` | 监听地址；非回环且 token 为空时拒绝启动 |
| `ZHENTU_PORT` | `4020` | HTTP 端口 |
| `ZHENTU_DB` | `zhentu/data/registry.db` | SQLite 路径 |
| `ZHENTU_REGISTRY_TOKEN` | 空 | Bearer token；非回环监听时必填 |
| `ZHENTU_MAX_BODY_BYTES` | `65536` | 注册体上限 |
| `ZHENTU_TTL_MAX_S` | `600` | 租约上限 |
| `ZHENTU_ROUTE_TTL_MS` | `5000` | `/route` 决策的建议有效期 |

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/zhentu/v1/health` | 免 | 注册表自身健康与在线节点数 |
| POST | `/zhentu/v1/nodes/register` | Bearer | 注册/覆盖完整快照 |
| POST | `/zhentu/v1/nodes/{id}/heartbeat` | Bearer | 刷新租约/状态/资源/loaded |
| POST | `/zhentu/v1/nodes/{id}/deregister` | Bearer | 优雅停机注销 |
| GET | `/zhentu/v1/nodes` | Bearer | `include_offline/role/status/capability/model/limit` |
| GET | `/zhentu/v1/nodes/{id}` | Bearer | 单节点详情 |
| GET | `/zhentu/v1/capabilities` | Bearer | 按能力聚合在线节点与模型 |
| GET | `/zhentu/v1/candidates` | Bearer | 按能力/模型返回回退候选 |
| POST | `/zhentu/v1/route` | Bearer | 单跳选路决策：`selected` + `alternates` + `rejected` |
| GET | `/zhentu/v1/metrics` | Bearer | Prometheus 文本指标（进程内，重启归零） |

```bash
TOKEN='<cluster-token>'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4020/zhentu/v1/nodes
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4020/zhentu/v1/capabilities
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:4020/zhentu/v1/candidates?capability=chat&limit=5"
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"capability":"chat","strategy":"balanced"}' \
  http://127.0.0.1:4020/zhentu/v1/route
```

## 选路决策 `/route`

控制面接口：只返回「用哪个节点的哪个模型」，不代理请求、不返回 endpoint 与任何凭据；
执行仍由调用方经 NewAPI 完成。决策只在内存中计算，不落库。

请求字段（全部可选，默认 `capability=chat`、`strategy=balanced`、`max_candidates=3`）：

| 字段 | 说明 |
|---|---|
| `capability` | 能力过滤，`embeddings`/`embed` 会归一为 `embedding` |
| `model` | 指定模型名；其他模型不参与决策 |
| `context_len` | 请求上下文长度；已知且更小的模型被排除 |
| `strategy` | `balanced` / `latency` / `local_first` / `power_saving` |
| `max_candidates` | 1..10，夹取后生效；`selected` + `alternates` 总数不超过该值 |
| `exclude_nodes` / `exclude_models` | 排除列表；模型名可带可选 `newapi/` 前缀 |
| `allowed_models` | 白名单，最多 200 项；为空表示不限制 |
| `allow_loading` / `allow_shedding` | 放行 `loading` 节点 / 热降载节点 |

硬过滤：租约非 `online`、`status=down`、能力/模型不匹配、上下文不足、显式排除、
`thermal_shedding=true`、并发上限显式为 0。软评分：负载 0.35、新鲜度 0.15、
已加载 0.20、热状态 0.15、角色 0.10、余量 0.05；`degraded` 乘 0.85、`loading`
乘 0.70。缺字段取中性值或保守估算并在 `reasons` 标注，不会 500。散仙自契约 v1.2
起上报真实 `resources.active`；缺失时按 `queue_len` 估算并标 `estimated_load`。

排序固定为 `score` 降序 → `queue_len` 升序 → `last_seen` 降序 → `node_id` → `model`，
同一输入多次调用结果一致。无候选返回 200 + `selected=null` + `reason=no_route`
（不是 5xx）；参数错误返回 `invalid_capability` / `invalid_strategy` / `too_many_models`，
存储不可用时返回 503 `registry_unavailable`。

## 指标

`GET /zhentu/v1/metrics` 输出 Prometheus 文本格式，沿用 Bearer 鉴权，不新增鉴权例外：

| 指标 | 说明 |
|---|---|
| `zhentu_route_total{strategy,result}` | 决策总数，`result=selected\|no_route` |
| `zhentu_route_rejected_total{reason}` | 被拒候选数，按原因码累计 |
| `zhentu_route_duration_seconds` | 决策耗时直方图（1ms ~ 2.5s，含 `+Inf`） |

指标只保存在进程内、重启归零；刻意不记录 `node_id` / `model` 标签，避免高基数与拓扑泄漏。

## 租约

- 节点上报心跳周期（10–120s）；`ttl = clamp(3 × interval, role_floor, 600)`。
- `quasheng` floor 90s；`sanxian` floor 180s（容忍手机 doze）。
- 读取时才计算 `online/offline`，不做后台清扫；节点时钟不参与判定。
- `lease.state` 与节点自报 `status` 是两个维度：节点可以 `online` 但 `degraded`。

## 能力

- 能力挂在模型上：`models[].capabilities`，节点级能力是并集。
- 规范名：`chat` / `embedding` / `vision` / `ocr` / `rerank`；输入兼容 `embeddings` 别名。
- `models_rev = "sha256:" + SHA256("name|cap1,cap2|context_len" 按 name 排序后以 \n 连接)`，`loaded` 不参与。
- `routable = lease.online && status ∈ {ready, degraded}`；`loaded` 单独返回。

## 错误

统一 OpenAI 形状：

```json
{"error":{"message":"...","type":"invalid_request_error","param":null,"code":"node_unknown"}}
```

常见 code：`unauthorized`、`invalid_payload`、`node_id_mismatch`、`node_unknown`、`boot_mismatch`、`payload_too_large`、`registry_unavailable`。

## 测试

```bash
.venv/bin/python -m pytest zhentu/tests -q
```

覆盖鉴权、报文校验、upsert、心跳、租约过期、boot_id 冲突、`need_snapshot`、注销、过滤、能力聚合、候选排序、跨重启持久化、与散仙 Kotlin 共用的 `models_rev` 向量，以及 `/route` 的过滤/评分/确定性/缺字段退化/参数错误/脱敏。
