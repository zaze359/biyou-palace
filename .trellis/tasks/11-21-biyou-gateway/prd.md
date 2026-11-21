# PRD · 第二步：碧游宫网关接入（NewAPI 渠道 / 权重 / 故障隔离）

## 一、目标

把「碧游宫 = NewAPI 网关」从手工后台配置变成**仓库内可复现的配置 + 一条命令同步**，
并用可重复的验收工具证明三件事：

1. 网关能统一转发到任意仙徒渠道；
2. 同模型多渠道按权重分配流量；
3. 某渠道上游失联后，网关能自动隔离并继续服务。

## 二、范围

### 做

- `deploy/newapi/`：渠道与网关选项的声明式配置（JSON）+ 管理员同步 CLI（纯标准库）。
- NewAPI 管理员鉴权支持三种来源（按优先级）：
  `NEWAPI_ADMIN_TOKEN` → `NEWAPI_ADMIN_USERNAME`/`NEWAPI_ADMIN_PASSWORD` → 刷新会话 Cookie 文件。
- 渠道配置至少包含：现有 Ollama 直连渠道（type=4）与准圣节点渠道（type=1，OpenAI 兼容）。
- 网关选项：重试次数、自动禁用开关、禁用阈值、可重试状态码；验收时临时改、`finally` 恢复。
- `scripts/daojian/gateway_acceptance.py`：G1–G5 验收脚本，结果写 JSON + 控制台表。
- `docs/reports/biyou-gateway-2026-11-21.md`：实测报告（真实数据，注明未测项与原因）。
- `deploy/newapi/docker-compose.yml` + `.env.example`：与线上容器一致的编排模板（不要求重启现有容器）。

### 不做

- 不改 `nodes/**`、`zhentu/**` 与任何 HTTP 契约；不改散仙（Android）工程。
- 不做 Prometheus/Grafana 看板（第四步已完成的部分不重复）。
- 不提交任何明文密钥；管理员密码/token/refresh cookie 一律走环境变量或仓库外文件。
- 不做 NewAPI 版本升级、不改其数据库。

## 三、验收标准

| 编号 | 场景 | 判据 |
|---|---|---|
| G1 | 统一入口 | 用客户端 key 调 `GET /v1/models` 返回 200，含 `qwen2.5:7b` |
| G2 | 指定渠道转发 | 请求别名模型（`model_mapping` 指到 `qwen2.5:7b`）只命中准圣渠道，日志 `channel` 与响应 200 一致 |
| G3 | 权重分配 | 两渠道按 NewAPI 权重语义（无内存缓存时 `weight+10` 平滑）构造 3:1 目标流量（声明 23:1，预测 75%），连发 60 次短请求，按日志渠道计数，占比落在 62%–88% 区间 |
| G4 | 故障自动隔离 | 临时渠道指向可杀死的桩上游；杀掉后触发失败，达到阈值自动禁用（`status=3`），期间其它渠道请求仍 200 |
| G5 | 基础并发 | 16 个请求 / 并发 4，全部 2xx，记录 p50/p95 |
| G6 | 回归 | `zhentu` 测试全绿；E2E 桩模式 7/7 不受影响 |

**安全与清理**

- 验收创建的临时渠道名统一前缀 `biyou-verify-`，无论成功失败都在 `finally` 删除。
- 改动的网关选项先快照、`finally` 恢复；恢复失败必须以非零退出码报告。
- 同步 CLI 只增改**配置中声明的渠道**，不删除配置外的渠道。

## 四、依赖与前置

- NewAPI 已在本机 `127.0.0.1:3000` 运行（`calciumion/new-api:v1.0.0-rc.48`）；
- 管理员凭据在仓库外（`~/biyou/newapi/conf/credentials.env`），通过环境变量注入；
- 准圣节点 `127.0.0.1:4010` 运行中，模型 `qwen2.5:7b`；
- 客户端 key：`ZHENTU_NEWAPI_API_KEY`（与阵图共用）。
