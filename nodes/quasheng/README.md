# 准圣节点智能体（PC · Python + Ollama）

让一台 PC 成为**自治双模单元**：本地跑 Ollama，对外暴露 OpenAI 兼容推理服务，同时可在本地直接对话。
协议基准：[`docs/node-agent-contract.md`](../../docs/node-agent-contract.md) v1.0。

## 架构：薄代理 + 契约专属表面

```
调用方 ──▶ FastAPI ──┬─▶ 切面：bearer 鉴权 / 响应头 / 429 并发闸门 / 断连取消
                     ├─▶ 代理：Ollama /v1/chat/completions（SSE **原样透传**）
                     ├─▶ 代理：Ollama /v1/models · /v1/embeddings
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
| `preload_models` | 启动即预热常驻的模型，消除"首次请求阻塞 30s+" |
| `capabilities_map` | 能力声明。**未登记的模型默认只有 `chat`** |
| `max_concurrency` | 并发上限，满载立即 `429` + `Retry-After` |
| `shared_token` | 配了就自动开启 bearer 鉴权（覆盖全部端点，含 health） |
| `mem_warn_pct` / `cpu_warn_pct` | `degraded` 判定阈值 |

> ⚠️ 能力必须显式声明：模型没登记进 `capabilities_map` 就没有 embedding 能力，请求 `/v1/embeddings` 会直接 `501`。

### 环境变量（`.env.example`）

`nodes/quasheng/.env.example` 列出全部可被环境变量覆盖的配置项，与 `config.py` 的 `_ENV_OVERRIDES` 一一对应：
`QUASHENG_OLLAMA_BASE` / `QUASHENG_PORT` / `QUASHENG_NODE_ID` / `QUASHENG_NODE_NAME` / `QUASHENG_SHARED_TOKEN` / `QUASHENG_MAX_CONCURRENCY`。

```bash
cp nodes/quasheng/.env.example .env
set -a && source .env && set +a     # 进程不会自动读取 .env，需手动导入后再启动
```

优先级：**环境变量 > config.yaml > 默认值**。其中 `QUASHENG_CONFIG` 只决定 YAML 路径（默认 `./config.yaml`），不属于字段覆盖；`QUASHENG_SHARED_TOKEN` 留空等同未设置。降级阈值 `mem_warn_pct` / `cpu_warn_pct` 暂无环境变量覆盖，只能写在 YAML。

## 运行

```bash
python -m nodes.quasheng.app
# 或：uvicorn nodes.quasheng.app:app --host 0.0.0.0 --port 4010
```

## 可视控制面板（/ui）

节点启动后，用浏览器访问：

```text
http://<节点主机>:4010/ui/     # /ui 会自动 307 到 /ui/，不占用 /v1 前缀
```

页面是单文件 `nodes/quasheng/static/index.html`（原生 JS + CSS，无构建链、无前端依赖），由 FastAPI `StaticFiles` 挂载到 `/ui`。

**token 填写方式**：右上角输入 `shared_token`，点「保存 token」。token 存在**当前浏览器的 localStorage**（键 `quasheng_token`），之后页面发出的所有请求自动带 `Authorization: Bearer <token>`。token 不写进 HTML，也不落在后端任何位置；换浏览器或换设备需重新填写。未开启鉴权（`shared_token` 为空）时留空即可。

> 静态页本身不需要 token（Starlette `Mount` 不继承 app 级 `dependencies`），**但页面调用的 `/v1/*` 仍走同一套 bearer 鉴权**——后端没有为面板开任何鉴权例外。

四个区块与对应端点：

| 区块 | 内容 | 端点 |
|---|---|---|
| 节点状态 | status、runtime 与版本、contract_version、node id、max_concurrency、max_context_len、模型列表（标出 loaded） | `GET /v1/health` |
| 资源与并发 | cpu_pct、mem_used/total、disk_free、queue_len（进度条） | `GET /v1/health` |
| 对话测试 | 选模型、流式逐字显示、首 token 耗时与总耗时 | `POST /v1/chat/completions`（`stream: true`） |
| Embeddings 测试 | 选模型、返回维度与耗时；501 时展示错误码 | `POST /v1/embeddings` |

状态区每 4 秒轮询一次；页面切走（`visibilitychange`）或关闭（`pagehide`）即停止轮询。任何调用失败都按 OpenAI 错误体原样展示 `HTTP 状态 · code · message`，不会只显示「请求失败」；401 会停止轮询并提示重新填写 token。

**适用范围**：本机/内网调试与演示用的可视化壳（无登录态、无多用户隔离），不替代调用方 SDK，也不修改节点配置。

## 本地模式

```bash
python -m nodes.quasheng.cli --once "你好，介绍一下你自己"
python -m nodes.quasheng.cli            # 进入交互 REPL，/exit 退出
```

CLI **不直连 Ollama**，而是走 `localhost` 的同一套 `/v1/*` 端点——以此证明本地模式与集群消费走完全相同的代码路径。

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
| `/v1/health` 关键字段 | node（id/role/runtime/runtime_version/contract_version）、status、models（loaded/capabilities）、resources（queue_len=0、准圣专属字段为 null）、limits |
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
- **降级阈值不可用环境变量覆盖**：`mem_warn_pct` / `cpu_warn_pct` 只读 YAML（`QUASHENG_*` 仅覆盖 `.env.example` 列出的 6 项）。
