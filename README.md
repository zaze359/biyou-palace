# biyou-palace（碧游宫）

一套**内网异构推理集群**：碧游宫网关、阵图编排、仙徒节点、灵脉监控、道鉴检定五层，
全部跑在局域网内，数据不出金鳌岛。

- **碧游宫**（NewAPI）：统一 OpenAI 兼容入口、加权负载均衡、故障隔离；
- **阵图**（ZhenTu）：节点注册/选路、复杂请求拆解、并行推演与道果聚合；
- **仙徒节点**：准圣（PC + Ollama）与散仙（安卓 + llama.cpp），同时是自治双模单元——
  本地跑模型并向集群暴露**完全一致**的 OpenAI 兼容 HTTP 服务，也保留本机对话能力；
- **灵脉**（Prometheus + Grafana）与**道鉴**（压测 / 故障注入 / 长时耐久）。

> 一键起中枢机：[集群一键部署与从零复现](#九集群一键部署与从零复现)。
> 节点协议仍是本仓库的地基，两套实现用不同语言、不同推理运行时，共享同一份协议契约，
> **不共享代码**：

| 节点 | 角色 | 平台 | 推理运行时 | 默认端口 |
|---|---|---|---|---|
| 准圣 `quasheng` | `quasheng` | PC | Python + Ollama | `4010` |
| 散仙 `sanxian` | `sanxian` | 安卓 App | Kotlin + llama.cpp | `4011` |

> 协议基准：[`docs/node-agent-contract.md`](docs/node-agent-contract.md)（**契约 v1.0**）。
> 设计核心：协议共享取代代码共享；能力驱动路由；一套实现同时服务集群与本地。

---

## 一、架构总览

```
调用方 / 碧游宫网关 / 阵图 / 灵脉 / 本地客户端
            │
            ▼
   ┌─────────────────────┐
   │  节点统一 HTTP 契约   │  基础路径 /v1/   契约版本头 Contract-Version: 1.0
   │  GET  /v1/health      │  健康+能力自述（节点唯一必须实现的元数据端点）
   │  GET  /v1/models      │  模型列表（OpenAI 兼容）
   │  POST /v1/chat/completions │ 核心推理（流式 SSE 必选）
   │  POST /v1/embeddings  │  向量化（无能力则 501）
   └─────────┬───────────┘
             │ 准圣：反向代理 Ollama /v1/*（SSE 原样透传）+ 原生 /v1/health
             │ 散仙：Ktor 本地实现 4 端点 + llama.cpp 推理
```

关键约定（详见契约文档）：

- **一套实现服务集群与本地**：本地对话客户端通过 `localhost` 调用同一套 `/v1/*` 端点，不存在独立「本地 API」。
- **能力驱动路由**：节点在 `/v1/health` 声明能力；不具备的能力返回 `501`，由上层绕开，不在节点内用 `if/else` 分支处理节点类型。
- **鉴权可选**：`Authorization: Bearer <shared-token>`；内网可免鉴权（建议开启，避免裸推理端点）。
- **错误格式**：统一 OpenAI 风格 `{"error": {"message","type","param","code"}}`。
- **取消语义**：客户端断开连接时，节点立即中止生成并释放资源。

---

## 二、快速开始

### 方式 A：统一入口 `start.sh`

```bash
# 无参数 → 弹出菜单选择组件
bash start.sh

# 无交互直跑指定组件（其余参数透传给该组件的启动命令）
bash start.sh quasheng                # 内门仙徒（PC，旧名准圣）
bash start.sh sanxian                 # 外门仙徒（安卓，旧名散仙）
bash start.sh zhentu                  # 阵图
bash start.sh cluster                 # 中枢机编排（--check 只校验，--up 才拉起）
bash start.sh desktop                 # 桌面客户端

bash start.sh --list                  # 只列组件
bash start.sh --help                  # 帮助与示例
```

| 组件 | 说明 | 底层命令 |
|---|---|---|
| `quasheng` | 内门仙徒 · PC（Python + Ollama，:4010；旧名准圣） | `bash scripts/run_quasheng.sh` |
| `sanxian` | 外门仙徒 · 安卓（Kotlin + llama.cpp，:4011；需真机） | `bash nodes/sanxian/build_and_run_sanxian.sh` |
| `zhentu` | 阵图 · 节点注册表与选路（:4020） | `bash scripts/run_zhentu.sh` |
| `cluster` | 中枢机 · 网关 + 阵图 + 灵脉编排 | `bash scripts/bootstrap_cluster.sh` |
| `desktop` | 桌面客户端 · 碧游宫壳 + 本地 dsh | `bash desktop/start.sh` |

旧名仍可用：`准圣`、`散仙`、`registry`、`gateway`、`client`。

### 方式 B：准圣节点（PC）

**前置条件**

- Python **3.11+**（本项目用 3.13；脚本内置托管路径回退逻辑）
- **Ollama ≥ 0.5.x**（低于此版本没有 `/v1/*`，启动会明确报错并退出）

**一键运行**

```bash
# 完整流程：校验环境 → 安装缺失组件 → 生成配置 → 启动服务（前台，Ctrl-C 停止）
bash scripts/run_quasheng.sh

# 常用参数
bash scripts/run_quasheng.sh --setup              # 只校验+安装+配置，不启动
bash scripts/run_quasheng.sh --no-ollama-install  # 不自动装 Ollama（缺失则退出）
bash scripts/run_quasheng.sh --pull               # 额外自动拉取 config 中的 preload_models
```

脚本会自动创建 `.venv` 并安装依赖、生成 `nodes/quasheng/config.yaml`、检测/启动 Ollama、预热模型，最后前台启动服务。

### 方式 C：散仙节点（安卓 App）

**前置条件**（本机/Mac/CI 必须）：JDK 17、Android SDK 35 + Build-Tools 35.0.0、NDK 27.x、CMake 3.22.1、adb、`ANDROID_HOME`。
> 本仓库不含工具链，无法在此环境编 APK，请在本机或 CI 执行。

```bash
export JAVA_HOME=$(brew --prefix openjdk@17)
export ANDROID_HOME=$HOME/Library/Android/sdk

cd nodes/sanxian

# 一键：补 local.properties → 编译 → 安装 → 拉起节点 → 探活
./build_and_run_sanxian.sh
./build_and_run_sanxian.sh --no-install   # 只编译 APK
./build_and_run_sanxian.sh --no-build     # 跳过编译（用已有 APK）
./build_and_run_sanxian.sh --push-model ~/Qwen3-0.6B-Q8_0.gguf  # 推送模型到设备
```

⚠️ 模型需自行 `adb push`（默认预热 `/sdcard/biyou/models/Qwen3-0.6B-Q8_0.gguf`），否则 `health=loading`、`chat=503`。

⚠️ 后台存活有系统限制：请保持 App 在前台或「最近任务」、电池设为「不受限制」；重装/升级后需重新确认。稳定恢复只有「打开 App」或「重启设备」。详见 [`nodes/sanxian/README.md`](nodes/sanxian/README.md#后台存活用户侧约定android-12实测结论)。

---

## 三、配置

### 准圣节点 `nodes/quasheng/config.yaml`

复制样例后按需编辑：

```bash
cp nodes/quasheng/config.example.yaml nodes/quasheng/config.yaml
export QUASHENG_CONFIG=nodes/quasheng/config.yaml   # 不设置则只读当前目录的 config.yaml
```

| 配置项 | 说明 |
|---|---|
| `ollama_base` | Ollama 地址，默认 `http://127.0.0.1:11434`（本机直连，不走 HTTP(S)_PROXY） |
| `port` | 服务端口，默认 `4010`（避开 Ollama 的 11434） |
| `preload_models` | 启动即预热常驻的模型，消除「首次请求阻塞 30s+」 |
| `capabilities_map` | **能力声明。未登记的模型默认只有 `["chat"]`** —— 不声明 embeddings 会直接 `501` |
| `max_concurrency` | 并发上限，满载立即 `429` + `Retry-After`（不做本地排队） |
| `shared_token` / `auth_enabled` | 配了 token 即自动开启 bearer 鉴权（覆盖全部端点，含 `/v1/health`） |
| `mem_warn_pct` / `cpu_warn_pct` | `degraded` 判定阈值 |

**环境变量覆盖**（`.env.example` 列出全部可覆盖项；进程不自动读 `.env`，需手动 `set -a && source .env && set +a`）：
`QUASHENG_OLLAMA_BASE` / `QUASHENG_PORT` / `QUASHENG_NODE_ID` / `QUASHENG_NODE_NAME` / `QUASHENG_SHARED_TOKEN` / `QUASHENG_MAX_CONCURRENCY`。

> 优先级：**环境变量 > config.yaml > 默认值**。`QUASHENG_CONFIG` 只决定 YAML 路径。

### 散仙节点 `nodes/sanxian/composeApp/src/androidMain/assets/sanxian.json`

示例配置同上思路（节点 ID、端口、能力声明、预热模型、并发上限、token 等），由 App 资产打包。

---

## 四、API 使用说明（统一契约端点）

所有端点基础路径 `/v1/`，请求体 `application/json`，流式用 SSE（`text/event-stream`）。开启鉴权后，所有请求（含 `/v1/health`）都需带 `Authorization: Bearer <token>`。

### 1. `GET /v1/health` — 健康与能力自述

供网关探活、灵脉采集、阵图路由消费。

```bash
curl http://127.0.0.1:4010/v1/health
# 开启鉴权后：
curl -H "Authorization: Bearer <token>" http://127.0.0.1:4010/v1/health
```

返回关键字段：`node`（id/name/role/runtime/runtime_version/contract_version）、`status`（`ready`｜`loading`｜`degraded`｜`down`）、`models[]`（name/family/loaded/capabilities）、`resources`（cpu/mem/disk/queue_len，散仙专属 `battery_pct`/`temperature_c`/`thermal_state`）、`limits`、`updated_at`。

### 2. `GET /v1/models` — 模型列表（OpenAI 兼容）

```bash
curl http://127.0.0.1:4010/v1/models
```

### 3. `POST /v1/chat/completions` — 核心推理

语义兼容 OpenAI；`stream: true` 走 SSE，以 `data: [DONE]` 收尾（**必选支持**）。

```bash
curl -N -X POST http://127.0.0.1:4010/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

### 4. `POST /v1/embeddings` — 向量化

```bash
curl -X POST http://127.0.0.1:4010/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"nomic-embed-text","input":["你好"]}'
```

### 常见响应码

| 场景 | 状态码 | 错误码 |
|---|---|---|
| 缺/无效 token（开启鉴权后） | `401` | `invalid_api_key` |
| 并发超 `max_concurrency` | `429` + `Retry-After: 1` | `concurrency_limit_exceeded` |
| 模型未声明该能力 | `501` | `capability_not_supported` |
| 上游（Ollama/模型）未就绪 | `503` | 视上游 |

---

## 五、验证与测试

### 准圣：单元测试（不依赖 Ollama / 网络）

```bash
bash scripts/verify.sh
```

创建 `.venv` 后运行 `nodes/quasheng/tests` 下的纯逻辑单测。

### 准圣：端到端冒烟（不依赖真实 Ollama）

```bash
bash scripts/smoke_quasheng.sh
# 自定义端口（默认 mock 11499 / 节点 4012，均避开 11434 与 4010）：
SMOKE_MOCK_PORT=11499 SMOKE_NODE_PORT=4012 bash scripts/smoke_quasheng.sh
```

复用 `tests/mock_ollama.py` 作假上游，覆盖：health 字段、models 形状、流式 SSE、无能力 `501`、缺 token `401`、并发 `429`、断连中止上游。全绿返回 `0`，失败逐条打印 `[FAIL]` 并附日志。

### 散仙：手工探活（需真机 / CI）

装好并拉起后（App 前台服务自动启动 Ktor），手机与电脑同局域网取手机 IP，或 `adb forward tcp:4011 tcp:4011` 后走 localhost：

```bash
curl http://127.0.0.1:4011/v1/health
curl http://127.0.0.1:4011/v1/models
curl -N -X POST http://127.0.0.1:4011/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<loaded>","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

准圣的 mock 冒烟已验证契约行为，散仙应对标准圣**逐条对照**。

---

## 六、设计约束与注意事项（改动前必读）

1. **能力必须显式声明**：模型没登记进 `capabilities_map` 就没有 embedding 能力，请求 `/v1/embeddings` 会直接 `501`。
2. **SSE 绝不重新解析**：代理层只转发字节（准圣）；散仙手工拼装 chunk。若将来要聚合内容，必须先做 `delta.content is None` 保护。
3. **不转发调用方 Authorization 给 Ollama**（准圣 `ollama_client._proxy_headers`）：Ollama 忽略 api_key，转发是多余的暴露面。
4. **本机流量不走代理**：Ollama 用 `trust_env=False`。若部署环境设了 `HTTP_PROXY`，httpx 默认会把 `127.0.0.1:11434` 这类本地请求也扔给代理，导致绝对 URI 被改写进 path → 404。**本地 Ollama 必须直连**。
5. **资源采集失败一律降级为 `null`**，不得抛 `500`；Ollama 不可达时 `status=down` 但进程不崩。
6. **客户端断连即中止生成**：准圣流式检查 `request.is_disconnected()`；散仙取消 Flow → `stopGeneration`。
7. **降级阈值 `mem_warn_pct` / `cpu_warn_pct` 仅可读 YAML**，暂无环境变量覆盖。

---

## 七、目录结构

```
biyou-palace/
├── docker-compose.yml             # 中枢机一键起：碧游宫 + 阵图 + 灵脉
├── .env.example                   # 中枢机编排变量模板
├── start.sh                       # 统一启动入口（节点 / 阵图 / 中枢机 / 桌面客户端）
├── desktop/                       # 桌面客户端：Electron 品牌壳 + dsh 运行时（见 desktop/README.md）
│   ├── start.sh / start.command   # 一键启动（Finder 可双击）
│   ├── electron/                  # 主进程 / preload / dsh 子进程管理
│   └── runtime/home/profiles/biyou/   # dsh profile 模板
├── deploy/
│   ├── newapi/                    # 碧游宫：渠道/选项声明 + 管理员 CLI + compose
│   ├── zhentu/Dockerfile          # 阵图镜像
│   └── nodes.example.json         # 批量注册清单模板
├── docs/
│   ├── deployment.md              # 从零复现部署手册
│   ├── operations.md              # 运维、备份、故障排查
│   ├── node-agent-contract.md     # 节点智能体统一契约 v1.2（协议基准）
│   └── orchestration-contract.md  # 阵图编排/任务/事件契约
├── monitoring/                    # 灵脉：Prometheus + Grafana 编排与看板
├── zhentu/                        # 阵图：注册表 / 选路 / 任务编排 / 指标
├── nodes/
│   ├── quasheng/                  # 准圣节点（PC · Python + Ollama）
│   │   ├── app.py                 # FastAPI 装配（薄代理 + 契约专属表面）
│   │   ├── config.example.yaml    # 配置样例
│   │   ├── cli.py                 # 本地对话 REPL（走 localhost 同端点）
│   │   ├── health.py / ollama_client.py / concurrency.py / errors.py
│   │   └── tests/                 # 单测 + mock_ollama
│   └── sanxian/                   # 散仙节点（安卓 · Kotlin + llama.cpp）
│       ├── build_and_run_sanxian.sh
│       ├── NATIVE_INTEGRATION.md  # 构建集成细节与已落地补丁
│       ├── shared/ composeApp/ nativelib/   # 底座本体 + 节点增量源码
│       └── README.md
└── scripts/
    ├── bootstrap_cluster.sh       # 中枢机本地配置生成 + 编排校验
    ├── run_quasheng.sh            # 准圣一键运行（校验/装依赖/配/启动）
    ├── run_zhentu.sh              # 阵图本机进程启动（不走容器时）
    ├── register_nodes.py          # 批量注册节点到阵图
    ├── collect_logs.py            # 容器/节点日志与诊断快照收集
    ├── verify.sh                  # 准圣单测
    ├── smoke_quasheng.sh          # 准圣端到端冒烟（含负向用例）
    └── daojian/                   # 道鉴：压测 / 故障注入 / 耐久 / 网关验收
```

---

## 八、灵脉：集群监控（Prometheus + Grafana）

准圣、散仙、阵图各自暴露 Prometheus 文本指标，`monitoring/` 提供一键编排：

```bash
cd monitoring
printf '%s' "$ZHENTU_REGISTRY_TOKEN" > prometheus/zhentu_token   # 阵图 Bearer
docker compose up -d
# Prometheus http://127.0.0.1:9090   Grafana http://127.0.0.1:3001（看板「灵脉 · 碧游宫集群」）
```

| 源 | 端点 |
|---|---|
| 准圣 | `GET /metrics`（鉴权策略与节点一致） |
| 散仙 | `GET /metrics`（同上；`adb forward` 后用本机端口抓取） |
| 阵图 | `GET /zhentu/v1/metrics`（Bearer，与注册用同一 token） |

`/metrics` 属运维端点，**不属于 `/v1` 契约**：不改响应头、不改既有响应形状、不占用并发名额。
指标清单、抓取地址改法、6 条告警规则见 [`monitoring/README.md`](monitoring/README.md)。

---

## 九、集群一键部署与从零复现

中枢机（一台常开 PC）用根目录 compose 起控制面；边缘节点按文档上线。

```bash
cp .env.example .env                 # 也可以交给 bootstrap 生成
bash scripts/bootstrap_cluster.sh    # 生成 token/密钥并校验编排（不启动）
docker compose up -d                 # new-api + zhentu + prometheus + grafana
docker compose ps
```

- `bootstrap_cluster.sh --check` 只检查不写文件；`--up` 生成后直接拉起。
- 需要保留旧栈数据时先停旧 compose；根编排卷名带 `biyou-palace_` 前缀。
- 完整步骤（NewAPI 管理员 / API key、渠道改地址、准圣 / 散仙上线、灵脉、道鉴验收）见
  [`docs/deployment.md`](docs/deployment.md)。
- 日常运维、备份升级、故障排查见 [`docs/operations.md`](docs/operations.md)。

---

## 十、碧游宫网关与阵图编排

### 碧游宫（NewAPI）

渠道与选项是声明式配置，一条命令同步：

```bash
set -a; source .env; set +a
python3 deploy/newapi/gateway_admin.py channel sync --dry-run
python3 deploy/newapi/gateway_admin.py channel sync
python3 deploy/newapi/gateway_admin.py option apply
```

- `deploy/newapi/channels.json`：渠道声明；远程节点把 `host.docker.internal` 换成局域网 IP。
- `deploy/newapi/gateway-options.json`：重试、自动隔离阈值与状态码。
- 验收：`python3 scripts/daojian/gateway_acceptance.py --strict`（G1–G5）。

### 阵图（ZhenTu）

阵图是控制面：只做节点注册 / 心跳、选路、任务拆解 / 调度 / 聚合与指标，不代理推理请求。

```bash
curl -H "Authorization: Bearer $ZHENTU_REGISTRY_TOKEN" \
  http://127.0.0.1:4020/zhentu/v1/nodes
curl -H "Authorization: Bearer $ZHENTU_REGISTRY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"capability":"chat","strategy":"balanced"}' \
  http://127.0.0.1:4020/zhentu/v1/route
```

- 端点与租约：[`zhentu/README.md`](zhentu/README.md)
- 请求 / 计划 / 事件 / 结果契约：[`docs/orchestration-contract.md`](docs/orchestration-contract.md)
- 批量注册：`python3 scripts/register_nodes.py --file deploy/nodes.example.json`
- 编排 E2E：`python3 scripts/daojian/orchestration_e2e.py --strict`

`ZHENTU_PLANNER_MODEL` 留空时阵图不拆步；填入网关可用模型后才会先规划再执行。

---

## 十一、已知限制

- **中枢机 / 网关单点**：阵眼失效则全阵不可用；高可用与多活列入 V1.1。
- **节点注册以自注册 + 静态清单为主**：没有自动发现与自动入阵。
- **阵图取消是批次边界语义**：在途步骤不会立即中断。
- **阵图指标进程内**：选路指标重启归零；任务指标按账本现状计算。
- **灵脉单实例**：15 天保留、无远程存储与高可用。
- **准圣 `/v1/models` 的 `owned_by`（已对齐契约）**：节点将每项 `owned_by` 统一改写为契约值 `biyou-node`，其余字段原样透传。
- **准圣 `show()` 未实现**：无调用方，模型详情与能力来自 `/api/tags` + `capabilities_map`。
- **散仙 `embeddings` 真实向量化（已落地）**：Plan A chat/embedding 双句柄 + JNI `embed` 桥，pooling 跟随模型元数据；未配置 `embedding_model` 或 embedding 句柄加载失败时返回 `503`，能力未声明时返回 `501`。
- 散仙本地聊天 UI 走 localhost 同端点：已完成（`NodeChatClient` 请求 `http://127.0.0.1:<port>/v1/chat/completions`，与集群消费同一路径；见 `NATIVE_INTEGRATION.md` §7）。

---

## 十二、参考文档

- 部署手册（从零复现）：[`docs/deployment.md`](docs/deployment.md)
- 运维手册（注册 / 日志 / 检定 / 备份）：[`docs/operations.md`](docs/operations.md)
- 协议基准：[`docs/node-agent-contract.md`](docs/node-agent-contract.md)
- 编排协议：[`docs/orchestration-contract.md`](docs/orchestration-contract.md)
- 联阵协议：[`docs/lan-formation-contract.md`](docs/lan-formation-contract.md)
- 阵图注册表：[`zhentu/README.md`](zhentu/README.md)
- 灵脉监控：[`monitoring/README.md`](monitoring/README.md)
- 准圣节点：[`nodes/quasheng/README.md`](nodes/quasheng/README.md)
- 散仙节点：[`nodes/sanxian/README.md`](nodes/sanxian/README.md) · [`NATIVE_INTEGRATION.md`](nodes/sanxian/NATIVE_INTEGRATION.md)
- 实测报告：[`docs/reports/`](docs/reports/)
