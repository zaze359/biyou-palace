# 灵脉 · 集群监控（Prometheus + Grafana）

把三处指标接成一套可看、可告警的监控：

| 源 | 指标端点 | 鉴权 |
|---|---|---|
| 准圣 `quasheng` | `http://<host>:4010/metrics` | 与节点一致（`auth_enabled` 为真时需 Bearer） |
| 散仙 `sanxian` | `http://<host>:14011/metrics`（adb forward 到设备 4011） | 同上 |
| 阵图 `zhentu` | `http://<host>:4020/zhentu/v1/metrics` | Bearer（`ZHENTU_REGISTRY_TOKEN`） |

## 启动

```bash
cd monitoring

# 阵图 token：内容与节点/阵图使用的 ZHENTU_REGISTRY_TOKEN 完全一致（不要带换行）
printf '%s' "$ZHENTU_REGISTRY_TOKEN" > prometheus/zhentu_token

docker compose up -d
```

- Prometheus：<http://127.0.0.1:9090>（Targets 页应看到三个 job 全 `UP`）
- Grafana：<http://127.0.0.1:3001> → 看板「灵脉 · 碧游宫集群」（匿名只读）

## 抓取地址怎么改

`prometheus/prometheus.yml` 默认用 `host.docker.internal` 指向宿主机：

- **Docker Desktop（macOS/Windows）**：保持默认即可（compose 已加 `host-gateway` 映射）。
- **Linux**：把 targets 改成宿主机内网 IP，或保留 `host.docker.internal` 并确认 compose 的 `extra_hosts` 生效。
- **散仙真机**：设备 IP 会变，最稳的是先 `adb forward tcp:14011 tcp:<sanxian.json 的 port>`，
  再让 Prometheus 抓 `host.docker.internal:14011`；设备固定 IP 时可直接写 `http://192.168.x.y:4011`。

改完热加载：`curl -X POST http://127.0.0.1:9090/-/reload`。

**节点开了鉴权（`auth_enabled: true`）时**：`/metrics` 与其它端点走同一套 bearer 策略，
需要给对应 job 打开 `authorization` 块（`prometheus.yml` 里已注释备好），
token 写进 `prometheus/quasheng_token` / `sanxian_token`（已被 `.gitignore` 忽略）。
阵图 `/zhentu/v1/metrics` 则始终需要 Bearer。

## 指标一览

节点侧（准圣/散仙同构，前缀不同）：

| 指标 | 说明 |
|---|---|
| `<prefix>_http_requests_total{endpoint,method,status}` | 端点级请求量（endpoint 已归一化，未知路径记 `other`） |
| `<prefix>_model_requests_total{model,capability,status}` | 模型请求量与结果（散仙 `capability` ∈ chat/vision/embedding） |
| `<prefix>_request_duration_seconds` / `_first_byte_seconds` | 13 档直方图，P95 用 `histogram_quantile` |
| `<prefix>_inflight_requests` / `_queue_len` / `_effective_max_concurrency` | 并发现状（散仙 queue_len 恒 0：满载立即 429） |
| `sanxian_thermal_shedding` / `_temperature_celsius` / `_battery_percent` / `_mem_used_mb` | 散仙设备状态 |
| `sanxian_model_loaded{model}` | 模型是否真正加载（配置声明不算） |
| `sanxian_up` | 散仙抓取成功 |

阵图侧：

| 指标 | 说明 |
|---|---|
| `zhentu_route_total{strategy,result}` / `zhentu_route_rejected_total{reason}` | 选路决策与拒绝原因 |
| `zhentu_route_duration_seconds` | 决策耗时直方图 |
| `zhentu_nodes_total` / `zhentu_nodes_online` | 注册表节点存量（只给计数，不带 node_id，避免高基数） |

## 告警

`prometheus/alerts.yml`（6 条，`promtool check rules` 通过）：

- `LingMaiNodeDown`：任一 job `up == 0` 持续 1 分钟
- `LingMaiModelNotLoaded`：`sanxian_model_loaded == 0` 持续 5 分钟
- `LingMaiThermalShedding`：散仙发热降载
- `LingMaiConcurrencySaturated`：在途并发顶满有效上限持续 2 分钟
- `LingMaiNoOnlineNode`：阵图在线节点为 0 持续 3 分钟
- `LingMaiRouteNoCandidate`：5 分钟窗口内出现 `no_route` 决策

## 已验证 / 未覆盖

- 已验证（2026-11-19，本机）：三源 `up=1`；`sanxian_*`、`quasheng_*`、`zhentu_nodes_*` 均能在 Prometheus 查到；
  Grafana 自动 provisioning 出数据源与看板；`promtool check rules` 6 条规则通过。
- 未覆盖：Docker 镜像拉取（本机 registry 不可达，改用官方二进制验证同一份配置）；
  Grafana 面板的像素级效果需人工打开确认。
- 已知小事：准圣 `quasheng_inflight_requests` 在抓取瞬间通常为 1——`/metrics` 自身也算一个在途请求。
  散仙的 `sanxian_inflight_requests` 由闸门读数，不含 `/metrics`（该端点不占用并发名额）。
