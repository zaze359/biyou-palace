# 灵脉监控接入（Prometheus + Grafana）

> 父任务：`10-13-biyou-v1-blueprint`（第四步「灵脉监控 + 道鉴检定」的监控切片）
> 契约：`docs/node-agent-contract.md` v1.2（响应头仍 `Contract-Version: 1.0`）
> 目标：把已有指标端点接成一套可看、可告警的监控，并补齐散仙缺的 `/metrics`。
> 状态：**已完成**（2026-11-19），实测记录见 `implement.md`。
> 非目标：道鉴压测报告（同属第四步，另立任务）、Grafana 之外的 APM/日志平台。

## 一、现状（开工前实测）

| 组件 | Prometheus `/metrics` | 说明 |
|---|---|---|
| 准圣 `nodes/quasheng` | ✅ 有（`quasheng_*`，13 档直方图） | 但本机 4010 跑的是 2026-11-08 的旧进程，实际返回 404 |
| 阵图 `zhentu` | ✅ 有（`zhentu_*`，Bearer 鉴权） | 当前未运行 |
| 散仙 `nodes/sanxian` | ❌ 无 | 只有 `/v1/health` JSON；契约允许第一步只给 JSON 快照 |
| 监控编排 | ❌ 无 | 仓库内无 Prometheus / Grafana / 告警任何资产 |

## 二、范围（本任务做什么）

1. **散仙 `/metrics`**（Kotlin/KMP，`androidMain`）
   - 新增 `NodeMetrics`：线程安全计数器 + 直方图 + 即时值渲染，纯 JVM 可测。
   - 指标命名与前缀：`sanxian_*`，标签不得含高基数/敏感值（不带 token、不带 endpoint URL）。
   - 端点免鉴权（与准圣一致：`/metrics` 不在 `/v1` 契约面内，供本机/内网 scrape；阵图例外保持 Bearer）。
2. **monitoring/ 编排**
   - `monitoring/docker-compose.yml`：Prometheus + Grafana，可选挂载节点宿主机。
   - `monitoring/prometheus/prometheus.yml`：scrape 三源（准圣 4010、散仙 4011 或 adb 转发口、阵图 4020）。
   - `monitoring/prometheus/alerts.yml`：节点离线、散仙发热降载、并发满载、模型未加载、阵图无在线节点。
   - `monitoring/grafana/`：数据源 + 看板 JSON 自动 provisioning，仙侠命名。
3. **准圣运行态修正**：以当前代码重启（获得 `/metrics`），并把能力声明与真实模型对齐。
4. **文档与验收**：README 增「灵脉」一节；本任务 `implement.md` 记录实测（含 curl 抓取与 Prometheus `up` 校验）。

## 三、散仙指标清单（最小集）

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `sanxian_http_requests_total` | counter | `endpoint,method,status` | 端点级请求量 |
| `sanxian_model_requests_total` | counter | `model,capability,status` | `capability` ∈ chat/vision/embedding |
| `sanxian_request_duration_seconds` | histogram | `model,capability` | 13 档，与准圣同桶 |
| `sanxian_first_byte_seconds` | histogram | `model,capability` | 流式为首个 content 分片，非流式等于总耗时 |
| `sanxian_inflight_requests` | gauge | — | 并发闸门占用（scrape 时读取） |
| `sanxian_queue_len` | gauge | — | 恒 0（满载立即 429），保持与 health 同语义 |
| `sanxian_effective_max_concurrency` | gauge | — | 降载后为 1 |
| `sanxian_thermal_shedding` | gauge | — | 0/1 |
| `sanxian_temperature_celsius` / `sanxian_battery_percent` / `sanxian_mem_used_mb` | gauge | — | scrape 时从健康采集器读取 |
| `sanxian_model_loaded` | gauge | `model` | 1/0，按引擎真实状态 |

## 四、验收标准

- [x] 散仙 `/metrics` 返回 200 + `text/plain; version=0.0.4`，含上表全部指标名。
- [x] 发一次流式 chat 后 `sanxian_http_requests_total{endpoint="/v1/chat/completions"}` 与 `sanxian_model_requests_total` 递增，直方图 `_count` 增加。
- [x] 满载时 `sanxian_inflight_requests` 与 health 的 `active` 一致；降载时 `sanxian_thermal_shedding=1` 且 `effective_max_concurrency=1`。
- [x] `/v1/*` 契约端点行为零变化（`NodeServerIntegrationTest` 全绿）。
- [~] Prometheus 三个 target 全部 `up=1`（已用官方二进制跑同一份配置验证；Docker 镜像拉取受本机网络限制未验证）；Grafana 数据源与看板已 provisioning、面板查询有数据（视觉呈现待人工确认）。
- [x] 告警规则 `promtool check rules` 通过，且规则名与标签不含高基数。

## 五、风险与边界

- **散仙无到点抓取**：设备在局域网、IP 会变；验收优先用 `adb forward` 的本机端口抓取，文档给出真机 IP 的写法。
- **不引入新契约字段**：`/metrics` 是运维端点，不改 `/v1/*`，不动响应头。
- **指标基数**：`model` 标签来自配置白名单（有限集合），不得使用请求里任意字符串；未声明的模型统一记 `unknown`。
- **准圣重启**：需先确认 Ollama 只有 `qwen2.5:0.5b` 与 `nomic-embed-text`，配置要与真实模型对齐，不能沿用 7B 预加载。
