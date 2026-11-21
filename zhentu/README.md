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
| `ZHENTU_ARTIFACT_DIR` | `zhentu/data/artifacts` | 藏经阁产物落盘目录 |

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
| POST | `/zhentu/v1/taskboard/tasks` | Bearer | 创建任务（按 `ticket_id` 幂等） |
| GET | `/zhentu/v1/taskboard/tasks` | Bearer | 紧凑列表（`status` / `owner` / `limit`） |
| GET | `/zhentu/v1/taskboard/tasks/{id}` | Bearer | 紧凑详情（不含历史与大产物） |
| POST | `/zhentu/v1/taskboard/tasks/{id}/claim` | Bearer | CAS 认领 + 租约 + 写域冲突提示 |
| POST | `/zhentu/v1/taskboard/tasks/{id}/update` | Bearer | CAS 更新状态 / owner / 租约 |
| POST | `/zhentu/v1/taskboard/tasks/{id}/results` | Bearer | 追加结果（`result_id` 幂等） |
| GET | `/zhentu/v1/taskboard/tasks/{id}/results` | Bearer | 结果增量（`after_seq`） |
| POST | `/zhentu/v1/taskboard/reap` | Bearer | 回收过期租约 |
| POST | `/zhentu/v1/taskboard/artifacts` | Bearer | 藏经阁落盘（内容哈希去重） |
| GET | `/zhentu/v1/taskboard/artifacts` | Bearer | 产物索引（按 `ticket_id` 过滤） |
| GET | `/zhentu/v1/taskboard/artifacts/{id}` | Bearer | 读取产物原文 |
| PUT | `/zhentu/v1/costs/prices/{price_version}` | Bearer | 写入价格表 |
| GET | `/zhentu/v1/costs/prices` | Bearer | 查询价格表 |
| PUT | `/zhentu/v1/costs/budgets/{ticket_id}` | Bearer | 设置香火预算 |
| GET | `/zhentu/v1/costs/budgets/{ticket_id}` | Bearer | 查询预算使用与状态 |
| POST | `/zhentu/v1/costs/entries` | Bearer | 记录用量（`entry_id` 幂等） |
| GET | `/zhentu/v1/costs/entries` | Bearer | 查询用量明细 |
| GET | `/zhentu/v1/costs/summary/{ticket_id}` | Bearer | 按仙徒/步骤/模型汇总 |
| GET | `/zhentu/v1/costs/alerts` | Bearer | 预算 80% / 100% 告警 |
| POST | `/zhentu/v1/costs/admission` | Bearer | 开工前预算准入判断 |

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
| `zhentu_tasks_created_total` | 任务账本中创建过的任务总数 |
| `zhentu_tasks_terminal_total{status}` | 按终态统计：`completed` / `partial` / `failed` / `canceled` |
| `zhentu_tasks_active` | 活跃任务数（`queued` / `running` / `aggregating`） |

选路指标只保存在进程内、重启归零；任务指标在 scrape 时由账本现状计算，重启不丢。
两类指标都刻意不记录 `task_id` / `node_id` / `model` / prompt，避免高基数与信息泄漏。

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

## 任务板与香火账本

- **CAS**：认领与更新都要求 `expected_revision`；不匹配返回 409 `revision_conflict`，不覆盖写入。
- **依赖**：`blocked_by` 中任一任务未 `completed` 时认领返回 409 `task_blocked`；依赖完成后即时可认领，无需重算缓存。
- **租约**：认领写入 `lease_expires_at`；`POST /taskboard/reap` 将过期任务回到 `pending` 并递增 `revision`。
- **写域**：`write_scopes` 只返回 `write_scope_hints`，不构成锁，也不阻止认领。
- **结果**：只追加；`result_id` 幂等；内联内容上限 8192 字符，超出必须走藏经阁 `artifact_ref`。
- **紧凑视图**：列表与详情不返回历史与大产物；模型上下文只注入该视图。
- **缓存计量**：命中/未命中分开计价；`cache_unknown=true` 时全部按未命中价估算，且 `cache_savings=0`。
- **预算**：峰值用量比 ≥ 80% 记 `warning`，≥ 100% 记 `paused` 并写一次告警；`/costs/admission` 在 `paused` 时拒绝新香火。
- **幂等**：任务按 `ticket_id`、结果按 `result_id`、账目按 `entry_id` 去重，重复回禀不会重复记账。

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

覆盖鉴权、报文校验、upsert、心跳、租约过期、boot_id 冲突、`need_snapshot`、注销、过滤、能力聚合、候选排序、跨重启持久化、与散仙 Kotlin 共用的 `models_rev` 向量，以及 `/route` 的过滤/评分/确定性/缺字段退化/参数错误/脱敏；另覆盖任务板 CAS、阻塞依赖、租约回收、写域提示、结果幂等与藏经阁去重，以及香火缓存分计、预算告警与准入。
