# 准圣节点智能体（PC · Python + Ollama）

让一台 PC 成为**自治双模单元**：本地跑 Ollama，对外暴露 OpenAI 兼容推理服务，同时可在本地直接对话。
协议基准：[`docs/node-agent-contract.md`](../../docs/node-agent-contract.md) v1.0。

## 架构：薄代理 + 契约专属表面

```
调用方 ──▶ FastAPI ──┬─▶ 切面：bearer 鉴权 / 响应头 / 429 并发闸门 / 断连取消
                     ├─▶ 代理：Ollama /v1/chat/completions（SSE **原样透传**）
                     ├─▶ 代理：Ollama /v1/models · /v1/embeddings
                     ├─▶ 可选：<provider>/<model> 直通外部 OpenAI 兼容上游（默认关闭）
                     └─▶ 原生：/api/tags + /api/ps + psutil ──▶ /v1/health
```

Ollama ≥ 0.5.x **自带 OpenAI 兼容端点**，所以本节点不写转码层，只做代理并补上契约专属表面（health、并发治理、鉴权、取消）。
唯一原生实现的是 `/v1/health`——Ollama 没有这个端点。

## 要求

- **Ollama ≥ 0.5.x**（低于此版本没有 `/v1/*`，启动时会明确报错并退出）
- Python 3.11+（本项目用 3.13）

## 安装

```bash
# 本项目统一使用托管 3.13（scripts/*.sh 内置同一路径与回退逻辑）
PY=/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3
"$PY" -m venv .venv && source .venv/bin/activate
pip install -r nodes/quasheng/requirements.txt
```

## 配置

```bash
cp nodes/quasheng/config.example.yaml nodes/quasheng/config.yaml
export QUASHENG_CONFIG=nodes/quasheng/config.yaml   # 不设置则只读当前工作目录的 config.yaml
```

| 配置项 | 说明 |
|---|---|
| `host` | 监听地址，默认 `127.0.0.1`（仅本机）。非回环地址必须配 `shared_token`，否则拒绝启动 |
| `preload_models` | 启动即预热常驻的模型，消除"首次请求阻塞 30s+"；名称须与 `ollama list` 完全一致，不存在的模型会被跳过且不会卡住 `loading` |
| `capabilities_map` | 能力声明。**未登记的模型默认只有 `chat`**；键名同样是 `ollama list` 的真实名称（如 `nomic-embed-text:latest`） |
| `max_concurrency` | 并发上限，默认 4（契约建议准圣 ≥ 4），满载立即 `429` + `Retry-After`（不做本地排队） |
| `shared_token` | 配了就自动开启 bearer 鉴权（覆盖全部端点，含 health） |
| `mem_warn_pct` / `cpu_warn_pct` | `degraded` 判定阈值 |
| `shutdown_timeout` | 优雅停机等待在途请求的最长秒数，默认 10 |
| `upstreams` | 可选的外部 OpenAI 兼容上游列表（默认空 = 行为与未接入时完全一致），见下节 |

> ⚠️ 能力必须显式声明：模型没登记进 `capabilities_map` 就没有 embedding 能力，请求 `/v1/embeddings` 会直接 `501`；反过来，只有 embedding 能力的模型请求 `/v1/chat/completions` 也会 `501 capability_not_supported`。

> 🔒 默认只监听 `127.0.0.1`。要对外提供服务，必须同时把 `host` 改成实际地址**并**配置 `shared_token`；节点会在启动时校验，缺失则直接失败并打印明确提示（避免裸推理端点暴露到局域网/公网）。

### 环境变量（`.env.example`）

`nodes/quasheng/.env.example` 列出全部可被环境变量覆盖的配置项，与 `config.py` 的 `_ENV_OVERRIDES` 一一对应：
`QUASHENG_HOST` / `QUASHENG_OLLAMA_BASE` / `QUASHENG_PORT` / `QUASHENG_NODE_ID` / `QUASHENG_NODE_NAME` / `QUASHENG_SHARED_TOKEN` / `QUASHENG_MAX_CONCURRENCY` / `QUASHENG_SHUTDOWN_TIMEOUT`。

上游密钥变量名由各上游的 `api_key_env` 自行指定（示例见 `.env.example` 中的 `QUASHENG_UPSTREAM_RELAY_A_KEY`），节点启动时按名读取。

```bash
cp nodes/quasheng/.env.example .env
set -a && source .env && set +a     # 进程不会自动读取 .env，需手动导入后再启动
```

优先级：**环境变量 > config.yaml > 默认值**。其中 `QUASHENG_CONFIG` 只决定 YAML 路径（默认 `./config.yaml`），不属于字段覆盖；`QUASHENG_SHARED_TOKEN` 留空等同未设置。降级阈值 `mem_warn_pct` / `cpu_warn_pct` 暂无环境变量覆盖，只能写在 YAML。

### 外部上游（可选：静态清单 + 动态发现）

节点可以额外接入一个或多个**静态配置**的 OpenAI 兼容上游（中转站 / 其他推理服务），用于「本机自用外部模型」。不配置 `upstreams` 时，`/v1/chat/completions`、`/v1/models`、`/v1/health` 与未接入前逐字段一致。

```yaml
upstreams:
  - id: relay-a                 # provider id，同时作为远程模型 id 前缀
    base_url: https://relay.example.com/v1   # 必须指向上游 /v1 根
    enabled: true               # 默认 false：只有显式开启才参与探活/列表/路由
    api_key_env: QUASHENG_UPSTREAM_RELAY_A_KEY   # 密钥只从该环境变量读取
    connect_timeout_s: 10
    timeout_s: 120
    max_concurrency: 4          # 单上游独立名额；满载立即 429，不排队
    visibility: local           # local | cluster | both，默认 local
    data_leaves_lan: true       # 声明会把 prompt 送出局域网
    models:                     # 静态清单；P0 不做动态发现
      - id: gpt-4o-mini
        capabilities: [chat]
        context_len: 128000
```

行为约定：

- **模型命名**：远程模型对外 id 为 `<provider>/<model>`（如 `relay-a/gpt-4o-mini`）；本地模型 id 保持不变。启动时会校验本地模型首段不得占用已启用的 provider 前缀，冲突直接拒绝启动并列出冲突项。
- **路由**：精确匹配本地模型走原路径；`<provider>/<model>` 走对应上游；未知 provider/模型或当前调用方不可见，统一 `404 model_not_found`，不做模糊匹配、不泄漏模型是否存在。
- **直通**：流式与非流式 chat 原样转发（仅把 `model` 换回上游原生 id，其余字段不解析、不改写）；上游错误按原状态码 + OpenAI 错误体返回；流开始后上游中断会发一帧 SSE `error`（`upstream_stream_aborted`）并关闭，**不补 `data: [DONE]`**。
- **可见性**：`visibility: local` 时只允许回环来源调用；非回环来源访问该模型返回 404。`/v1/models` 只列出 `cluster` / `both` 的远程模型；`/v1/health` 对回环来源额外展示 `local` 模型的可用性。
- **密钥安全**：密钥只经 `api_key_env` 读取；缺失时启动直接失败并提示变量名（不会静默降级）。密钥与 `base_url` 不进入日志、`/v1/models`、`/v1/health`、错误体或响应头；节点使用自持密钥访问上游，**不转发调用方的 `Authorization`**。
- **健康**：上游（阵图/中转站）只是节点间感知通道，与本地能力解耦。本地正常时节点整体状态仍为 `ready`，上游不可达只体现在 `upstreams[]`（`status=down`）与远程模型的 `available=false`，不会把本地模型标成不可用，也不会让编排层把节点判成 `down`。本地不可用但远程仍可用时为 `degraded`。上游不影响全局 `max_concurrency` 与 `queue_len` 默认值。
- **出网提示**：`data_leaves_lan: true` 会在启动日志与 health 中明示「prompt 会送出局域网」；集群可见（`visibility != local`）时为警告级。请确认上游可信、链路加密（公网建议 `https`），并注意企业代理：**本地 Ollama 仍强制直连（`trust_env=False`），上游请求则遵循 `HTTP(S)_PROXY` 环境变量**。
- **P1 边界**：不做多上游聚合、跨上游回退、用量计费；`/v1/embeddings` 不桥接到上游（远程模型一律按不可用处理）。动态发现只做「上游清单 ∩ 白名单」，不做模型能力自动探测，也不订阅上游变更通知。

#### 动态发现（`discover: true`，P1）

静态清单适合模型固定的场景；中转站模型经常增减时，可以开启按上游 `/v1/models` 的动态发现。

```yaml
upstreams:
  - id: relay-a
    base_url: https://relay.example.com/v1
    enabled: true
    api_key_env: QUASHENG_UPSTREAM_RELAY_A_KEY
    discover: true
    discover_interval_s: 300     # 默认 300 秒，下限 60 秒（低于则拒绝启动）
    models:                      # 可选：留空表示不设白名单
      - gpt-4o-mini
```

- **白名单交集**：配置了 `models[]` 时，只暴露「上游 `/v1/models` 返回 ∩ 白名单」；能力与 `context_len` 沿用白名单项。白名单里但上游未返回的模型会从快照中移除，调用返回 `404 model_not_found`。
- **未配置白名单**：`discover: true` 且 `models` 为空时，暴露上游返回的全部模型，能力默认 `chat`、`context_len` 为 `null`，启动日志会以 WARNING 明示。这是「开启动态发现」的最小惊讶语义；不确定上游模型范围时应显式配置白名单。
- **失败不阻塞、不清空**：首轮发现也在后台任务中执行，启动流程不等待任何上游请求，因此发现失败既不阻断启动、也不拉长启动耗时；后续失败沿用上一次成功快照。首次成功前若配置了白名单则暂按白名单暴露，未配置白名单则暂时为空。失败只体现在日志与 health 可选字段，不改变 `visibility`、命名、鉴权与路由开关，也不触碰本地能力与已加载状态。
- **空列表**：上游 `200` + `data: []` 视为合法快照（上游当前没有模型），会替换旧快照并打 WARNING。
- **频率下限**：`discover_interval_s` 默认 300 秒、最小 60 秒；后台只有一个 `asyncio` 循环，不引入新的调度依赖。
- **health 可选字段**：仅对 `discover: true` 的上游追加 `discover: true`、`last_discovery_at`（最近一次成功快照时间，ISO）与 `discovery_error`（最近一次失败原因，成功后清空）。`discover: false` 时 health 与 P0 逐字段一致。
- **边界不变**：不新增任何管理端点；发现是节点自身对上游的只读 `GET /v1/models`，密钥仍只从 `api_key_env` 指定的环境变量读取。

### 回退策略（`fallback_mode`，P1）

上游偶发不可用时，可以配置显式回退链条，把请求交给下一个可用模型。回退只发生在 `/v1/chat/completions`。

```yaml
fallback_mode: auto            # manual（默认）| auto
fallback_chain:
  - newapi/qwen2.5:0.5b        # 远程：<provider>/<model>
  - local/qwen2.5:0.5b         # 本地：local/<model>（local 为保留前缀）
```

| 模式 | 行为 |
|---|---|
| `manual`（默认） | **不切换**。上游错误原样返回，仅在可回退类失败的 OpenAI 错误体里追加 `error.fallback_candidates`（调用方可见的候选 id），不包含 base_url 与密钥。 |
| `auto` | 首次尝试失败且属于可回退类错误时，按 `fallback_chain` 顺序逐个尝试；每个候选最多一次。成功时响应头 `X-Node-Fallback-Source: <实际条目>` 标记实际服务来源，并在 `/v1/logs` 记一条 `fallback` 事件。 |

- **只兜首字节前的失败**：连接失败、超时、5xx。**4xx 一律不兜**，尤其是 `401/403`（凭据/权限配置错误必须暴露，不能掩盖）；`429` 同样不兜。
- **流式**：首字节之后上游中断，保持原行为——发一帧 SSE `error`（`upstream_stream_aborted`）并关闭，**不补 `[DONE]`**，也不回退。
- **不重复目标**：请求原模型若也在链条里，会被跳过；同一目标只尝试一次。不可见（`visibility=local` 且来源非回环）的远程候选直接跳过，且不会出现在候选提示里。
- **耗尽错误**：链条走完仍失败时返回最后一次失败的状态码，`error.code=fallback_exhausted`，`error.fallback_attempts` 按顺序列出试过的模型与原因，`error.fallback_candidates` 列出候选；消息中同样包含这些信息。原因均为脱敏短码（如 `http_502`、`upstream_unavailable`、`upstream_timeout`）。
- **启动校验**：`local/<model>` 是硬校验——需存在于 `/api/tags` 且声明 chat，否则拒绝启动并指出条目。`<provider>/<model>` 只做可用性提示：不在静态清单/发现快照、或首次发现尚未完成时，记录 WARNING（标记为「未校验」），**不拦截启动**；阵图不可达只意味着该远程候选暂时不可用，节点自身照常提供服务。
- **未配置链条**：`fallback_mode` 保持默认、`fallback_chain` 为空时，行为与接入回退前逐字段一致。
- **后续方向**：跨节点（跨仙徒）的回退需要按能力查询候选，留待阵图提供接口；当前只在单节点内按显式链条回退。

## 运行

```bash
python -m nodes.quasheng.app
# 或（默认只监听本机；对外监听需显式指定并配置 shared_token）：
uvicorn nodes.quasheng.app:app --host 127.0.0.1 --port 4010
```

启动时若 Ollama 尚未就绪，节点会按有限次数指数退避重试（1/2/4/8 秒），而不是立即退出；Ollama 版本低于 0.5.x 或重试耗尽才会启动失败。

## 常驻运行（开机自启）

两套模板分别面向 macOS（launchd）与 Linux（systemd），都包含工作目录、环境变量、崩溃重启与日志去处。模板里的 `__REPO_ROOT__` / `__PYTHON__` / `__USER__` / `__LOG_DIR__` 需要替换为真实绝对路径。

### macOS · launchd

```bash
mkdir -p ~/Library/Logs/biyou-quasheng          # 对应 __LOG_DIR__
sed -e "s#__REPO_ROOT__#$PWD#g" \
    -e "s#__PYTHON__#$PWD/.venv/bin/python#g" \
    -e "s#__LOG_DIR__#$HOME/Library/Logs/biyou-quasheng#g" \
    nodes/quasheng/deploy/launchd/com.biyou.quasheng.plist \
    > ~/Library/LaunchAgents/com.biyou.quasheng.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.biyou.quasheng.plist

tail -f ~/Library/Logs/biyou-quasheng/quasheng.out.log
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.biyou.quasheng.plist   # 停止
```

`KeepAlive` 只在崩溃/异常退出时拉起；`launchctl bootout` 的正常停止不会触发重启循环。

### Linux · systemd

```bash
sudo sed -e "s#__REPO_ROOT__#$PWD#g" \
         -e "s#__PYTHON__#$PWD/.venv/bin/python#g" \
         -e "s#__USER__#$USER#g" \
         nodes/quasheng/deploy/systemd/quasheng.service \
         > /tmp/quasheng.service
sudo cp /tmp/quasheng.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now quasheng

journalctl -u quasheng -f
sudo systemctl stop quasheng    # SIGTERM → 优雅停机
```

### 守护与自恢复

- **进程崩溃**：由 launchd/systemd 按策略拉起；节点自身不退出。`RestartSec` / `ThrottleInterval` 防止重启风暴。
- **Ollama 未就绪**：启动阶段有限次数退避重试，不会立即退出。
- **Ollama 运行期抖动**：`/v1/health` 会变 `down`，但进程不崩；上游恢复后 health 自动回到 `ready`（每次 health 都实时探测 tags/ps）。
- **优雅停机**：收到 `SIGTERM` / `SIGINT` 后**立即停止接受新连接**（真实进程路径下 uvicorn 先关闭 listener，新请求表现为连接被拒绝），在途请求继续完成；超过 `shutdown_timeout` 才强制退出。uvicorn 以 `timeout_graceful_shutdown=shutdown_timeout` 运行，避免无限等待长流。应用内的 `503 node_shutting_down` 中间件只在应用内部主动置位 `shutting_down` 时生效（如单测直接改状态）；真实信号路径观察不到该响应。

## 可视控制面板（/ui）

节点启动后，用浏览器访问：

```text
http://<节点主机>:4010/ui/     # /ui 会自动 307 到 /ui/，不占用 /v1 前缀
```

页面是单文件 `nodes/quasheng/static/index.html`（原生 JS + CSS，无构建链、无前端依赖），由 FastAPI `StaticFiles` 挂载到 `/ui`。

**token 填写方式**：右上角输入 `shared_token`，点「保存 token」。token 存在**当前浏览器的 localStorage**（键 `quasheng_token`），之后页面发出的所有请求自动带 `Authorization: Bearer <token>`。token 不写进 HTML，也不落在后端任何位置；换浏览器或换设备需重新填写。未开启鉴权（`shared_token` 为空）时留空即可。

> 静态页本身不需要 token（Starlette `Mount` 不继承 app 级 `dependencies`），**但页面调用的 `/v1/*` 仍走同一套 bearer 鉴权**——后端没有为面板开任何鉴权例外。

六个区块与对应端点：

| 区块 | 内容 | 端点 |
|---|---|---|
| 节点状态 | status、runtime 与版本、contract_version、node id、max_concurrency、max_context_len、模型列表（标出 loaded） | `GET /v1/health` |
| 资源与并发 | cpu_pct、mem_used/total、disk_free、在途并发（进度条）、真实排队数 queue_len（满载立即 429，故通常为 0） | `GET /v1/health` |
| 对话测试 | 选模型、流式逐字显示、首 token 耗时与总耗时 | `POST /v1/chat/completions`（`stream: true`） |
| Embeddings 测试 | 选模型、返回维度与耗时；501 时展示错误码 | `POST /v1/embeddings` |
| 运行日志 | 最近 N 条事件（时间/级别/消息），按级别过滤 | `GET /v1/logs?limit=N` |
| 并发压测 | 并发流式请求、逐请求状态码/耗时、在途并发曲线、汇总 | `POST /v1/chat/completions` × N + `GET /v1/health` 采样 |

**轮询协调**：状态区与日志区共用**一个 4 秒节拍**（同一 tick 内拉 health + logs），不各自起定时器。压测进行中改由 500ms 采样读取 health（因此暂停常规 health 拉取），只保留日志 4 秒刷新；页面切走或关闭即停止全部轮询与采样。

**运行日志（`GET /v1/logs`）**：返回 `{lines:[{time,level,event,message}], limit, capacity, total}`，`limit` 默认 50、夹到 1..200。数据源是进程内的白名单环形缓冲（容量 200），只记录这些事件：

- `startup`：启动成功 / 探针失败
- `warmup`：模型名 + 路径 + 耗时（ms）
- `auth`：鉴权失败（不记录调用方 token 值）
- `concurrency`：并发拒绝（含上限）
- `upstream`：上游错误状态码 + `code` + 截短消息
- `disconnect`：客户端断连中止
- `exception`：未捕获异常类型与消息（不写堆栈）

写入前统一脱敏：`shared_token` 替换为 `***`，常见绝对路径替换为 `<path>`，消息截断 500 字。**不记录请求 body、上游请求头与日志文件路径**；该端点与其它 `/v1/*` 一样要求 bearer token，无任何例外。

**并发压测**：填并发数（1–8）、模型与提示词后发起 N 个流式请求，逐条显示状态码、耗时与备注；429 单独标色并展示 `Retry-After`。压测期间每 500ms 采样 `resources.active`（在途并发）画成柱状序列，结束后汇总成功数、429 数、失败数与耗时（最小/中位/最大）。`queue_len` 是真实排队数；本节点满载立即 429、不做本地排队，因此它恒为 0，不再用作并发指标。注意：结果受本机模型能力与 `max_concurrency` 配置影响，仅用于功能验证，不代表真实负载基线。

状态区每 4 秒轮询一次；页面切走（`visibilitychange`）或关闭（`pagehide`）即停止轮询。任何调用失败都按 OpenAI 错误体原样展示 `HTTP 状态 · code · message`，不会只显示「请求失败」；401 会停止轮询并提示重新填写 token。

**适用范围**：本机/内网调试与演示用的可视化壳（无登录态、无多用户隔离），不替代调用方 SDK，也不修改节点配置。

## 本地模式

```bash
python -m nodes.quasheng.cli --once "你好，介绍一下你自己"
python -m nodes.quasheng.cli            # 进入交互 REPL
```

CLI **不直连 Ollama**，而是走 `localhost` 的同一套 `/v1/*` 端点——以此证明本地模式与集群消费走完全相同的代码路径。

REPL 命令：

| 命令 | 作用 |
|---|---|
| `/reset` | 清空多轮上下文（下一轮从零开始） |
| `/model <name>` | 运行中切换模型，无需重启 |
| `/model` | 列出 `/v1/models` 的可用模型 |
| `/exit` / `/quit` | 退出 |

交互模式会把 `user` / `assistant` 历史逐轮带上，因此支持真正的多轮对话；失败轮次会回滚用户消息，避免污染上下文。非 2xx 响应会打印 `HTTP <status> · <error.code> · <error.message>`，`--once` 失败时以非零码退出，不会静默输出空行。

## 模型生命周期管理（CLI）

契约第五章明确节点**不提供模型 / 权重管理 API**，因此模型管理全部在命令行完成，直连本机 Ollama 原生 `/api/*`，不经过节点、不需要 `--token`。

```bash
OLLAMA=http://127.0.0.1:11434   # 默认读 QUASHENG_OLLAMA_BASE，再回退到此值

python -m nodes.quasheng.cli list
python -m nodes.quasheng.cli pull qwen2.5:7b-instruct-q4_K_M
python -m nodes.quasheng.cli import ~/models/qwen2.5-7b-instruct-q4_K_M.gguf --name qwen2.5:7b-instruct-q4_K_M
python -m nodes.quasheng.cli delete qwen2.5:7b-instruct-q4_K_M          # 交互确认
python -m nodes.quasheng.cli delete qwen2.5:7b-instruct-q4_K_M --yes    # 非交互环境必须显式 --yes
python -m nodes.quasheng.cli unload qwen2.5:7b-instruct-q4_K_M          # keep_alive=0
```

| 命令 | 行为 |
|---|---|
| `list` | `/api/tags` 列出已安装模型，并用 `/api/ps` 标出「已加载 / 未加载」，附大小与参数量 |
| `pull <model>` | 先查 registry manifest 估算体积做预检，再流式输出拉取进度 |
| `import <gguf> [--name] [--system] [--template]` | 校验文件与空间后调用 `/api/create`，并打印等价 Modelfile |
| `delete <model> [--yes]` | 默认要求确认；非交互终端且没有 `--yes` 时直接拒绝 |
| `unload <model>` | 以 `keep_alive=0` 调用 `/api/generate`，释放模型占用的内存 |

通用参数：`--ollama <url>` 指定本机 Ollama（缺省 `QUASHENG_OLLAMA_BASE`，再缺省 `http://127.0.0.1:11434`）。管理命令使用 `trust_env=False`，本机流量不会被 `HTTP(S)_PROXY` 改写。

**预检规则**：`pull` / `import` 在写入前检查磁盘与内存，不足时拒绝并给出差额。

- 磁盘：需要「模型体积 + 512 MB 预留」，与 `OLLAMA_MODELS`（默认 `~/.ollama/models`）所在分区可用空间比较。
- 内存：需要「模型体积 × 1.1」与当前可用物理内存比较（加载时还有 KV cache 与运行时开销）。
- `pull` 无法从 registry 取得体积（私有模型、离线、manifest 格式未知）时，打印提示并跳过预检，不阻断拉取。

失败一律返回非零退出码，错误信息格式为「步骤 + 模型 + 原因」，例如：
`拉取模型 qwen2.5:7b 失败：磁盘空间不足，需要 5.5 GB（模型 5.0 GB + 预留 512.0 MB），可用 1.0 GB，还差 4.5 GB`。

### 离线 GGUF 导入流程

适用于目标机器不能直连 Ollama registry、或只有 GGUF 权重的场景。

1. **准备 GGUF**：从模型发布页下载对应量化文件（如 `Q4_K_M`），放到本机任意目录，例如 `~/models/qwen2.5-7b-instruct-q4_K_M.gguf`。若只有 Hugging Face 原始权重，可用 llama.cpp 的 `convert_hf_to_gguf.py` 先转换再量化。
2. **确认 Modelfile 内容**（可选，`import` 会自动生成并打印）：

   ```text
   FROM /Users/you/models/qwen2.5-7b-instruct-q4_K_M.gguf
   SYSTEM """你是准圣节点上的本地助手。"""
   TEMPLATE """{{ if .System }}<|im_start|>system
   {{ .System }}<|im_end|>
   {{ end }}{{ if .Prompt }}<|im_start|>user
   {{ .Prompt }}<|im_end|>
   {{ end }}<|im_start|>assistant
   """
   ```

   最小可用版本只需要 `FROM <绝对路径>`；`TEMPLATE` 必须与模型族匹配，否则回答格式会异常。CLI 支持 `--system` / `--template` 直接写入。
3. **创建模型**：

   ```bash
   python -m nodes.quasheng.cli import ~/models/qwen2.5-7b-instruct-q4_K_M.gguf \
     --name qwen2.5:7b-instruct-q4_K_M
   ```

   等价手工命令：`ollama create qwen2.5:7b-instruct-q4_K_M -f ./Modelfile`。CLI 底层调用 `POST /api/create`，请求体包含 `model`、`from`（GGUF 绝对路径）以及可选的 `system` / `template`。
4. **验证**：

   ```bash
   python -m nodes.quasheng.cli list                 # 应出现新模型，状态为「未加载」
   ollama run qwen2.5:7b-instruct-q4_K_M "你好"       # 直接验证推理
   curl http://127.0.0.1:4010/v1/models              # 节点列表应包含该模型
   ```

   节点对未登记模型默认只声明 `chat` 能力；若要用于 `/v1/embeddings`，需在 `capabilities_map` 中显式声明 `embedding`。
5. **内存管理**：导入只把权重写入 `OLLAMA_MODELS`，不会立即加载。首次推理会加载模型；不用时执行 `unload` 释放内存。

> 注意：`delete` 只删 Ollama 中的模型条目与 blob，不会删除原始 GGUF 文件；确认无误后再自行清理源文件。


## 冒烟验证

```bash
BASE=http://127.0.0.1:4010

curl $BASE/v1/health                                   # status / models / resources / limits
curl $BASE/v1/models
curl -N -X POST $BASE/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 负向用例
curl -X POST $BASE/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","input":["hi"]}'        # → 501 capability_not_supported
curl $BASE/v1/health                                                 # 开启鉴权后无 token → 401
# 并发：发起 max_concurrency+1 个并发请求 → 429 + Retry-After
# 断连：流式请求中途 Ctrl-C，日志应出现"客户端断开，中止上游生成"

bash scripts/verify.sh    # 创建 .venv 并跑单测（不依赖 Ollama / 网络）
```

## 冒烟脚本

上面是手工 curl 清单；需要一条命令端到端跑完（含负向用例）时用冒烟脚本：

```bash
bash scripts/smoke_quasheng.sh
# 默认 mock 11499 / 节点 4012（均避开 11434 与 4010），可自定义：
SMOKE_MOCK_PORT=11499 SMOKE_NODE_PORT=4012 bash scripts/smoke_quasheng.sh
```

脚本复用 `tests/mock_ollama.py` 作为假上游，**不依赖真实 Ollama / 网络**；临时目录里生成配置并起节点，结束时 trap 清理所有子进程与临时目录。

前置条件：

- Python ≥ 3.11。优先使用托管路径 `/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3`，缺失时回退 `python3` 并打印提示。
- 首次运行若 `.venv` 不存在，会自动创建并按 `nodes/quasheng/requirements.txt` 安装依赖（不引入新依赖）。
- 默认端口 `11499` / `4012` 未被占用（占用时用上面的环境变量换端口）。

覆盖项：

| 断言 | 预期 |
|---|---|
| mock 上游 + 节点启动 | 两者就绪，`/v1/health` 200 |
| `/v1/health` 关键字段 | node（id/role/runtime/runtime_version/contract_version）、status、models（loaded/capabilities）、resources（queue_len=0、active=0、准圣专属字段为 null）、limits |
| `/v1/models` | `object=list`，`data[]` 每项 `object=model` |
| 流式 chat | 200 + `text/event-stream`，chunk 内容拼接完整，以 `data: [DONE]` 收尾 |
| 无 embedding 能力的模型 | 501 + `capability_not_supported` |
| 缺 token | 401（`/v1/health` 与 `/v1/models` 均覆盖，错误码 `invalid_api_key`） |
| 并发超限 | `max_concurrency=2` 时第 3 个并发流 429 + `Retry-After: 1` |
| 客户端中断 | 节点日志出现「中止上游生成」 |

失败时怎么看：全绿返回 0；任一断言失败返回 1，逐条打印 `[FAIL]` 项，并附 `node.log` / `mock.log` 的尾部日志。日志中的 `[FAIL]` 描述会同时给出期望值与实际值（如期望状态码 `200` 实际 `000` 通常表示连接失败）。

## 设计约束（改动前请先读）

1. **SSE 绝不重新解析**：代理层只转发字节。若将来要编织/聚合内容，必须先做 `delta.content is None` 保护。
2. **不转发调用方 `Authorization` 给 Ollama**（见 `ollama_client._proxy_headers`）。
3. Ollama 的 OpenAI 兼容是**实验性子集**：入参原样透传，不要替它做严格校验。
4. 资源采集失败一律降级为 `null`，不得抛 `500`；Ollama 不可达时 `status=down` 但进程不崩。
5. **本机流量不走 HTTP(S)_PROXY**：`OllamaClient` 用 `trust_env=False`。若部署环境设了 `HTTP_PROXY`（企业网/沙箱常见），httpx 默认会把 `127.0.0.1:11434` 这类本地请求也扔给代理，导致绝对 URI 被改写进 path（`/http://127.0.0.1:11434/api/...` → 404）。本地 Ollama 必须直连。

## 已知差异与限制（相对契约示例）

- **`/v1/models` 的 `owned_by`（已对齐契约）**：代理 Ollama `GET /v1/models`，仅把每项 `owned_by` 统一改写为契约 3.2 要求的 `biyou-node`（常量 `MODEL_OWNER`），其余字段原样透传；这是本节点唯一改写上游 JSON 的端点。
- **`show()` 未实现**：无调用方，模型详情与能力声明来自 `/api/tags` 和 `capabilities_map`，不依赖 `/api/show`。
- **降级阈值不可用环境变量覆盖**：`mem_warn_pct` / `cpu_warn_pct` 只读 YAML（`QUASHENG_*` 仅覆盖 `.env.example` 列出的 8 项）。
- **`resources.queue_len` 的真实语义**：契约 3.1 定义为"当前排队请求数"。节点采用"满载立即 429、不做本地排队"策略，因此该值恒为 0；在途并发作为附加字段 `resources.active` 暴露，面板与压测使用它。`queue_len` 不再表示在途并发。
