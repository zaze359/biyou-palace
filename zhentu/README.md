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

```bash
TOKEN='<cluster-token>'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4020/zhentu/v1/nodes
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4020/zhentu/v1/capabilities
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:4020/zhentu/v1/candidates?capability=chat&limit=5"
```

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

常见 code：`unauthorized`、`invalid_payload`、`node_id_mismatch`、`node_unknown`、`boot_mismatch`、`payload_too_large`。

## 测试

```bash
.venv/bin/python -m pytest zhentu/tests -q
```

覆盖鉴权、报文校验、upsert、心跳、租约过期、boot_id 冲突、`need_snapshot`、注销、过滤、能力聚合、候选排序、跨重启持久化、与散仙 Kotlin 共用的 `models_rev` 向量。
