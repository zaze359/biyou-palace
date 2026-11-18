# 执行记录：灵脉监控接入

## 1. 散仙 `/metrics`（Kotlin）

- 新增 `NodeMetrics`（`shared/src/androidMain/.../node/NodeMetrics.kt`）：计数器 + 13 档直方图 + 即时值 gauge 渲染，
  线程安全（`LongAdder` / `synchronized`），纯 JVM 可测。
- `NodeApi` 新增 `GET /metrics`，与新端点外的行为零耦合：
  - **HTTP 计数**走统一出口 `respondTracked` / `respondJson`，状态码由调用点显式给出；
    实测 Ktor 2.3 在 `sendPipeline`（Before/After）与 `onCallRespond` 里 `call.response.status()` 都是 `null`，
    依赖它会把所有响应记成 `status="0"`，故改成显式传值。
  - **模型计数**用 `ModelObservation`：流式在首个 content 分片记首字节，非流式以整体耗时充当；
    异常按 `NodeException.status` 归类，客户端断连记 `cancelled`。
  - 标签归一化：`endpoint` 只认固定集合（其余记 `other`），`model` 只认配置白名单（其余记 `unknown`）。
- 即时值在 scrape 时现算：并发、有效上限、降载、温度/电量/内存、`model_loaded`；
  健康采集失败只省略对应 gauge，不让 `/metrics` 自己 5xx。
- 顺手抽了 `loadedModelIds()`，让 `/v1/health`、`/v1/models`、`/metrics` 共用同一份「真正加载」判定（原先两处重复）。

## 2. 阵图补充节点存量 gauge

`zhentu/app.py` 的 `/zhentu/v1/metrics` 在路由指标后追加 `zhentu_nodes_total` / `zhentu_nodes_online`，
值取 `store.counts(clock)`；只给计数、不带 node_id/model，避免高基数与拓扑泄露。

## 3. monitoring/ 编排

```
monitoring/
├── docker-compose.yml            # prometheus 2.54.1 + grafana 11.2.0
├── prometheus/prometheus.yml     # 三源抓取（含鉴权块，按需取消注释）
├── prometheus/alerts.yml         # 6 条告警
├── grafana/provisioning/...      # 数据源 + 看板自动挂载
├── grafana/dashboards/lingmai-cluster.json
└── README.md
```

- 看板 10 个面板：在线（up）、阵图在线节点、温度、散热状态、并发占用、请求速率、推理时延 P95、
  4xx/5xx、模型加载态、阵图选路。
- 看板用 `${DS_LINGMAI}` 数据源变量，换数据源不用改面板。
- `prometheus/zhentu_token` 与 `data/` 已进 `monitoring/.gitignore`。

## 4. 准圣运行态修正

- 本机 4010 上跑的是 2026-11-08 的旧进程（`/metrics` 404）。已用当前代码重启，`/metrics` 200。
- `nodes/quasheng/config.yaml` 与真实模型对齐：`preload_models` 改 `qwen2.5:0.5b`、
  `capabilities_map` 的 embedding 键改 `nomic-embed-text:latest`（精确匹配，裸名会退化成 chat-only → 501）。
  重启后 `/v1/embeddings` 返回 200 且维度正常。
- 注意：`python -m nodes.quasheng.app` 默认读 **CWD 下的 config.yaml**；在仓库根启动必须设
  `QUASHENG_CONFIG=nodes/quasheng/config.yaml`，否则静默走默认值（本次踩到并纠正）。

## 5. 验证（本机，2026-11-19）

| 项 | 结果 |
|---|---|
| 散仙 `/metrics`（真机 XQ-AT72） | 200，`text/plain; version=0.0.4`；`sanxian_up=1`、3 个模型 `loaded=1`、温度 28℃ |
| 散仙计数 | chat/embeddings 后 `http_requests_total` 按 endpoint+status 递增；501 路径记 `status="501"`；模型计数与直方图 `_count` 递增 |
| 准圣 `/metrics` | 200，`quasheng_*` 指标可抓 |
| 阵图 `/metrics` | 200（Bearer），新增 `zhentu_nodes_total` / `zhentu_nodes_online` |
| Prometheus 抓取 | 三 job 全 `up=1`（本机以官方二进制跑同一份配置；Docker registry 在本机不可达，镜像拉取改用二进制验证） |
| 指标回读 | `sanxian_up`、`sanxian_temperature_celsius`、`sanxian_model_loaded`（3 个模型）、`quasheng_inflight_requests`、`zhentu_nodes_online` 均可查询 |
| Grafana | provisioning 生效：数据源 `LingMai`、看板「灵脉 · 碧游宫集群」，匿名访问 200；面板表达式经 datasource proxy 查询有数据 |
| 告警规则 | `promtool check rules` → 6 rules SUCCESS；`promtool check config` 通过（凭据文件就位时） |
| `docker compose config -q` | 通过 |
| 降载联动 | `thermalservice override-status 3` → `/metrics` 中 `thermal_shedding=1`、`effective_max_concurrency=1`，chat 返回 503；`reset` 后回到 0 / 2 |
| 单测 | 准圣 + 阵图 **248 passed**；散仙 shared **79** + composeApp **6**，全绿 |

## 6. 未覆盖 / 已知小事

- Docker 镜像拉取未验证（本机 registry 不可达）；Compose 拓扑本身用 `docker compose config` 校验。
- Grafana 面板的视觉呈现需人工打开确认（本次验证到「数据能查出来」）。
- 准圣 `quasheng_inflight_requests` 抓取瞬间常为 1：`/metrics` 自身也被计为一个在途请求（既有行为，未改）。
- 散仙 `/metrics` 与其它端点共用同一套 bearer 策略：`auth_enabled: true` 时抓取要带 token（已加注释块与文档）。

## 7. 现场复现命令

```bash
# 散仙（真机）
adb forward tcp:14011 tcp:4011 && curl -s http://127.0.0.1:14011/metrics | head
# 准圣
QUASHENG_CONFIG=nodes/quasheng/config.yaml .venv/bin/python -m nodes.quasheng.app &
curl -s http://127.0.0.1:4010/metrics | head
# 阵图
ZHENTU_REGISTRY_TOKEN=... .venv/bin/python -m zhentu.app &
curl -s -H "Authorization: Bearer ..." http://127.0.0.1:4020/zhentu/v1/metrics | tail
# 监控栈
cd monitoring && printf '%s' "$ZHENTU_REGISTRY_TOKEN" > prometheus/zhentu_token && docker compose up -d
```
