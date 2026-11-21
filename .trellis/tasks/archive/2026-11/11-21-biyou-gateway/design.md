# 设计 · 碧游宫网关接入（NewAPI 渠道 / 权重 / 故障隔离）

## 一、问题

碧游宫（NewAPI）目前只有一条手工创建的 `ollama-local` 渠道，渠道配置、权重、
重试与自动隔离策略都停留在后台点选状态：无法复现、无法代码评审、新机器部署要从头点一遍。
上一步的编排 E2E 已证明「阵图 → NewAPI → Ollama」真实链路可用，但网关本身仍是手工资产。

本任务把网关配置变成**仓库内的声明式配置 + 一条命令同步**，并用可重复的验收脚本
证明转发、权重分配、故障隔离与并发四项能力。

## 二、边界

- 只新增 `deploy/newapi/` 与 `scripts/daojian/gateway_acceptance.py`，不改 `zhentu/`、`nodes/`。
- 只操作 NewAPI 的管理 API 与网关客户端 API，不改其数据库、不升级版本。
- 密钥（管理员密码/token/refresh cookie、客户端 key）只从环境变量或仓库外文件读取，
  不写入仓库、不打印到日志。

## 三、管理员 API 形态（rc.48 实测）

| 动作 | 端点 | 关键点 |
|---|---|---|
| 登录 | `POST /api/user/login` | 返回 `data.access_token`（约 15 分钟）、`data.user.id` |
| 刷新 | `POST /api/user/auth/refresh` | 需 `new_api_refresh` Cookie，返回同构 bundle |
| 列表 | `GET /api/channel/?p=0&page_size=50` | `data.items`；列表**不返回真实 key**（置空） |
| 创建 | `POST /api/channel/` | body 必须为 `{"mode":"single","channel":{...}}`；`key` 不能为空 |
| 更新 | `PUT /api/channel/` | body 为**平铺** `{id, ...fields}`，PATCH 语义；`status` 字段会导致 `Invalid parameters`，状态需另行处理 |
| 删除 | `DELETE /api/channel/{id}` | 幂等友好，不存在时返回失败 JSON |
| 渠道测试 | `GET /api/channel/test/{id}?model=...` | 返回 `success` 与耗时 |
| 选项 | `GET/PUT /api/option/` | `PUT` body `{key,value}`，value 一律字符串 |
| 日志 | `GET /api/log/?p=0&page_size=N&type=2` | 每条含 `channel`/`channel_name`/`model_name`/`created_at`/`request_id`；写入有秒级延迟 |

套接请求头：`Authorization: Bearer <access_token>` + `New-Api-User: <user_id>`。

**状态字段限制**：更新渠道时传 `status` 会绑定失败。同步只负责「声明的静态字段」，
状态由 NewAPI 的启停 / 自动隔离机制管理；`channel delete` 仅用于验收清理。

## 四、目录与文件

```
deploy/newapi/
  gateway_admin.py       # 管理员 CLI（纯标准库）
  channels.json          # 渠道声明：ollama-local（type=4）与 quasheng-01（type=1）
  gateway-options.json   # 网关基线选项（显式 option apply 才生效）
  docker-compose.yml     # 与线上容器一致的编排模板
  .env.example           # 环境变量模板（无真实值）
scripts/daojian/
  gateway_acceptance.py  # G1–G5 验收
docs/reports/
  biyou-gateway-2026-11-21.md
```

## 五、gateway_admin.py

### 鉴权优先级

1. `NEWAPI_ADMIN_TOKEN`（+ `NEWAPI_ADMIN_USER_ID`，默认 1）——适合 CI / 短期令牌；
2. `NEWAPI_ADMIN_USERNAME` / `NEWAPI_ADMIN_PASSWORD`——登录换取 access_token；
3. `NEWAPI_ADMIN_COOKIE_FILE`（可选 `NEWAPI_ADMIN_SESSION`）——用 `new_api_refresh`
   调用刷新端点。

三者都缺失时给出可操作的错误提示（列出三个变量名），不猜测默认密码。
鉴权失败时输出 NewAPI 的 `message`，不打印 token。

### 子命令

| 命令 | 行为 |
|---|---|
| `channel list [--json]` | 列出渠道（id/name/type/base_url/models/weight/priority/status） |
| `channel sync [--config] [--dry-run] [--json]` | 按声明创建或更新；**不删除**配置外渠道 |
| `channel test <name|id> [--model]` | 调 NewAPI 的渠道测试端点 |
| `channel delete <name|id> [--force]` | 默认只允许删除 `biyou-verify-` 前缀的临时渠道 |
| `option get [key...] [--json]` | 读取选项 |
| `option set key=value...` | 逐项写入并回读校验 |
| `option apply [--config] [--dry-run]` | 应用 `gateway-options.json` 的基线选项 |
| `log list [--since] [--model] [--channel] [--limit] [--json]` | 查询消费日志，供验收计数 |

### sync 语义

- 按 `name` 匹配；缺失则创建，存在则比较声明字段。
- 比较字段：`type`、`base_url`、`models`（集合比较）、`group`、`groups`、`weight`、
  `priority`、`auto_ban`、`model_mapping`（JSON 规范化）、`setting`（JSON 规范化）。
- `key` **不参与比较**（列表接口不回传真实 key）；更新时按声明值重写，
  因此声明里的 key 是「占位值」，真实密钥应通过 `key_env` 读取。
- 更新请求不带 `status`；创建请求固定 `status=1`。
- 输出每个渠道的动作：`created` / `updated` / `unchanged`，退出码只反映是否发生错误。

### 渠道声明

`channels.json` 两条：

1. `ollama-local`：type=4（Ollama 协议），base_url `http://host.docker.internal:11434`，
   models `qwen2.5:0.5b,qwen2.5:7b,nomic-embed-text:latest`，weight 1。
2. `quasheng-01`：type=1（OpenAI 兼容），base_url `http://host.docker.internal:4010`，
   models 同上 + 别名 `quasheng/qwen2.5:7b`，`model_mapping` 把别名映射到 `qwen2.5:7b`，weight 1。

两条渠道都声明 `qwen2.5:7b`，用于验证「同模型多副本分摊」；别名只挂在准圣渠道上，
用于验证「按模型路由」。

### 网关基线选项

`gateway-options.json` 只声明与故障隔离直接相关的五项：

| 键 | 基线 | 理由 |
|---|---|---|
| `RetryTimes` | `1` | 允许一次换渠道重试，避免单次抖动直接失败 |
| `ChannelDisableThreshold` | `5` | 连续 5 次失败才隔离，避免瞬时错误误杀 |
| `AutomaticDisableChannelEnabled` | `true` | 故障自动隔离 |
| `AutomaticEnableChannelEnabled` | `false` | V1.0 不做自动恢复，防止抖动反复启停 |
| `AutomaticDisableStatusCodes` | `401,403,500,502,503,504` | 鉴权/上游 5xx 视为渠道故障；**429 不隔离**（限流是暂时背压，应重试而非摘除） |

`channel sync` **不会**自动应用选项；必须显式 `option apply`。验收脚本会先快照、
临时收紧（阈值 1、仅 500），再在 `finally` 恢复，避免验收改变基线。

## 六、gateway_acceptance.py

复用 `scripts/daojian/common.py` 的 HTTP / 报告 / 统计设施；管理员操作复用
`deploy/newapi/gateway_admin.py`（按路径导入，不复制鉴权逻辑）。

| 编号 | 方法 | 判据 |
|---|---|---|
| G1 | 客户端 key 调 `GET /v1/models` | 200 且包含 `qwen2.5:7b` |
| G2 | 请求别名 `quasheng/qwen2.5:7b` | 200；按 `model_name=别名` 与时间窗查日志，`channel_name` 必须是 `quasheng-01` |
| G3 | 建两条 `biyou-verify-w` 临时渠道（同上游、声明权重 23:1、别名映射到 `qwen2.5:0.5b`），连发 60 次 | 按日志 `request_id` 去重后计渠道占比，落在 62%–88%（NewAPI 无内存缓存时按 `weight+10` 平滑，23:1 预测 75%） |
| G4 | 进程内桩上游（0.0.0.0 随机端口），临时渠道 `biyou-verify-down` 指向它；先正常返回 200，再关停桩 | 失败达到阈值后渠道 `status=3`；期间主模型请求仍 200 |
| G5 | 临时 type=4 渠道指向 Ollama（别名映射到 `qwen2.5:7b`），16 请求 / 并发 4 | 全部 2xx；记录 p50/p95（避免准圣节点 429 名额干扰网关并发结论） |

约束：

- 所有临时渠道名以 `biyou-verify-` 开头，`finally` 删除；删除失败以非零退出码报告。
- 选项先快照（含 `RetryTimes`、`ChannelDisableThreshold`、`AutomaticDisableChannelEnabled`、
  `AutomaticDisableStatusCodes`），`finally` 逐项恢复并回读确认。
- 桩上游绑定 `0.0.0.0`，因为容器经 `host.docker.internal` 访问宿主机。
- 日志写入有延迟：按 `created_at >= 起始时间` 过滤并轮询等待，直到条数稳定或超时。
- 不使用 `None` 计数做「长度相等」断言；无数据时判失败并写明原因。

G6（回归）不在脚本内：单独运行 `pytest zhentu` 与编排 E2E 桩模式 7/7，结果写入报告。

## 七、风险与对策

| 风险 | 对策 |
|---|---|
| 更新渠道时误带 `status` 导致整条更新失败 | 更新 payload 显式剔除 `status`；sync 前先做字段白名单 |
| 验收临时渠道残留 | 名称前缀约束 + `finally` 删除 + 结束时 list 校验 |
| 选项恢复失败污染线上 | 逐项回读校验；任一恢复失败则退出码非零 |
| 日志延迟导致 G2/G3 假阴性 | 轮询 + 时间窗过滤 + 按 `request_id` 去重 |
| 权重随机波动 | 样本 60 次，接受 62%–88% 宽区间；失败时报告实际分布 |
| 权重语义与直觉不符 | NewAPI 无内存缓存时按 `(weight+10)` 计算占比：3:1 实际 54%，故验收用 23:1 构造 75% 目标流量，并在报告写明 |
| 桩上游被容器访问不到 | 绑定 `0.0.0.0`，先用渠道测试确认可达 |
