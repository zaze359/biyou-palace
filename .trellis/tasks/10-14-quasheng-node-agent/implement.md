# Implement · 准圣 PC 节点智能体（Python + Ollama）

> 协议基准：`docs/node-agent-contract.md` v1.0 ｜ 验收标准见 `prd.md`
> 已定策略：**预加载白名单模型** · **满载立即拒绝 429**
> 技术判断：**薄代理 + 契约专属表面**（Ollama ≥ 0.5.x 自带 `/v1/*`，不写转码层）

## 架构一句话

FastAPI 只负责三件事——**鉴权 / 响应头 / 并发闸门**；OpenAI 语义全部**反向代理**给本机 Ollama `/v1/*`（SSE 原样透传）；**只有 `/v1/health` 是节点原生拼装**。

```
调用方 ──▶ FastAPI ──┬─▶ 切面：bearer / 响应头 / 429 闸门 / 断连取消
                     ├─▶ 代理：Ollama /v1/chat/completions（SSE 透传）
                     ├─▶ 代理：Ollama /v1/models · /v1/embeddings
                     └─▶ 原生：/api/tags + /api/ps + psutil ──▶ /v1/health
```

## 前置条件

- Ollama **≥ 0.5.x**（必须；低于此版本没有 `/v1/*`），`ollama serve` 运行中，目标模型已 pull。
- Python 用托管版 `/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3`，依赖统一装进隔离 venv。

## 有序实施清单（14 个步骤）

1. **建包**：`nodes/quasheng/`（目录组织遵循 `.trellis/spec/backend/directory-structure.md`）。
2. **依赖**：`fastapi` / `uvicorn` / `httpx` / `pydantic` / `psutil`；测试加 `pytest`（用 `httpx.MockTransport` 模拟上游，不额外引入 `respx`）。
3. **配置层**：`config.example.yaml` + `.env.example`
   - `ollama_base`（默认 `http://127.0.0.1:11434`）
   - `port`（建议 `4010`，避开 Ollama 自身的 11434）
   - `node_id` / `node_name`
   - `shared_token` / `auth_enabled`
   - `preload_models: []`（白名单，启动预热常驻）
   - `capabilities_map: {model: [cap, ...]}`（未登记模型默认 `["chat"]`）
   - `max_concurrency`（默认 `2`）
   - `request_timeout`（默认 `300` 秒）
   - 降级阈值：`mem_warn_pct`（默认 85）、`cpu_warn_pct`（默认 90）
   - 环境变量：`QUASHENG_CONFIG` 指定 YAML 路径；其余 6 项（`OLLAMA_BASE` / `PORT` / `NODE_ID` / `NODE_NAME` / `SHARED_TOKEN` / `MAX_CONCURRENCY`）覆盖字段，与 `config.py` 的 `_ENV_OVERRIDES` 一一对应；降级阈值暂无环境变量覆盖
4. **启动探针**：`GET {ollama_base}/v1/models` 成功才继续；失败则明确报错退出，提示「需要 Ollama ≥ 0.5.x」。
5. **Ollama 客户端封装**：`probe()` / `version()` / `tags()` / `ps()`（httpx 元数据读取）、`stream_post()` / `post()` / `get()`（代理转发）；统一超时与错误归一。不实现 `show()`（无调用方，模型详情取自 `/api/tags` 与 `capabilities_map`）。
6. **并发闸门中间件**：原子计数在途请求；超过 `max_concurrency` 返回 `429` + `Retry-After`；计数同时喂养 `health.resources.queue_len`。
7. **预热**：启动时对 `preload_models` 逐个 warm-up
   - 推荐：`POST /api/generate {"model": name, "prompt": " ", "keep_alive": -1}`
   - 若该 Ollama 版本拒绝空 prompt，退化为一次 `POST /api/chat`（`num_predict: 1`、`keep_alive: -1`）的最小对话——**以实测为准**
   - 逐模型记录耗时（ms）与所走路径；预热失败仅告警，不阻断启动。
8. **`/v1/health`**：`tags` + `ps` + `psutil` 组装；`models[].loaded` 取自 `ps`；`status` 按下方规则判定。
9. **`/v1/models`**：直接代理 Ollama `GET /v1/models`，JSON **原样透传**，**不再**由 `/api/tags` 自行拼装 OpenAI 形状。
   - ⚠️ **待定**：`owned_by` 目前沿用上游值（Ollama 通常返回 `library`），与契约 3.2 示例要求的 `biyou-node` 不一致；两种候选——改实现补齐 `owned_by="biyou-node"`，或修改契约与文档——待用户拍板
10. **`/v1/chat/completions`**：httpx **流式透传**（保留 SSE 头与 `data: [DONE]`），**不解析 body**。
    - 上游 `4xx/5xx`：先读上游响应头，按原状态码返回 OpenAI 错误体；**不得**包装成 `200` + SSE（否则错误体会被调用方当成 chunk）
11. **`/v1/embeddings`**：按 `capabilities_map` 判定；无 embedding 能力返回 `501`。
12. **切面中间件**：
    - 响应头 `X-Node-Id` / `X-Node-Role` / `Contract-Version: 1.0`
    - bearer 校验**覆盖全部端点（含 health）**；**代理时剥离客户端 `Authorization`**，不转发给 Ollama
    - OpenAI 风格错误归一：非法 JSON → `422`、未知路由 → `404`、未捕获异常 → `500`，均为 `{error:{message,type,param,code}}` 且保留 `X-Node-*` 头；`401` / `429` / `501` / `503` 形状不变
    - `429` 响应带 `Retry-After`
    - **断连取消**：检测客户端断开 → 关闭上游流 / 中止生成
13. **本地 CLI**：`.venv/bin/python -m nodes.quasheng.cli`，通过 localhost 调用同一套端点。
14. **README + 验证脚本**：启动方式、配置项/环境变量说明、curl 样例；`scripts/verify.sh`（单测）与 `scripts/run_quasheng.sh`（一键运行）统一使用托管 3.13 解释器。

## `status` 判定规则

优先级：`down` > `loading` > `degraded` > `ready`。

- `down`：Ollama 不可达（tags / ps 均失败）
- `loading`：白名单模型尚未全部出现在 `/api/ps`（预热未完成或首次加载中）
- `ready`：Ollama 可达 **且** 白名单模型均已 `loaded`
- `degraded`：资源越界（内存 ≥ `mem_warn_pct` 或 CPU ≥ `cpu_warn_pct`）

> 阈值一律写进配置，不在代码里硬编码。

## 验证命令

```bash
# 解释器统一为托管 3.13（scripts/*.sh 内置同一路径，缺失时才回退 PATH 上的 python3）
PY=/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3
"$PY" -m venv .venv

# 启动（示例端口 4010）
.venv/bin/python -m uvicorn nodes.quasheng.app:app --host 0.0.0.0 --port 4010

# 端点冒烟
curl localhost:4010/v1/health
curl localhost:4010/v1/models
curl -N -X POST localhost:4010/v1/chat/completions \
  -H 'Content-Type: application/json' -H "Authorization: Bearer <token>" \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 负向用例（逐一验证）
# 1) 无 embedding 能力的模型请求 embeddings → 501
# 2) 缺 token → 401（含 /v1/health）
# 3) 并发超限：发起 max_concurrency+1 个并发请求 → 429 + Retry-After，并检查 health 的 queue_len
# 4) 断连取消：curl 流式请求中途 Ctrl-C，观察服务端日志确认生成已中止
# 5) 流式上游错误：请求不存在的模型 → 原状态码（如 404）+ OpenAI 错误体，而非 200 + SSE

bash scripts/verify.sh    # 创建 .venv 并跑 nodes/quasheng/tests
```

## 风险点与回滚

- **绝不重解析 SSE**：透传是默认策略；若将来必须编织/聚合内容，先补 `delta.content is None` 保护。
- Ollama 的 OpenAI 兼容是**实验性子集**：入参**原样透传**，不要替它做严格校验而放大差异。
- `psutil` 受限时采集失败降级为 `null`，**禁止抛 500**。
- Ollama 不可达 → `health.status = down`，**进程不得崩溃**。
- **回滚边界**：本子任务只新增 `nodes/quasheng/` 与其配置，不触碰 `deploy/`、`services/`；出问题可整目录回退。
