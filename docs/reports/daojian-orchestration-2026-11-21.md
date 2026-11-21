# 道鉴 · 阵图编排链路实测报告

> 生成时间：2026-11-21 02:54 +0800　|　最后更新：2026-11-21（真实网关重测）　|　协议：`docs/orchestration-contract.md` v1.0　|　工具：`scripts/daojian/orchestration_e2e.py`
> 结论：**桩网关 7/7；真实 NewAPI 网关 7/7**（真实网关重测见第十一节）

## 一、背景与目的

阵图的任务账本、集群编排、道果聚合、并行调度、前端集群模式五个任务都已归档，测试是绿的，
但**没有一次端到端实测**：任务从提交到事件流、执行、聚合、取结果的整条链路只在单元测试里用
FakePlanner/FakeExecutor 跑过。本报告补上这一环。

阵图的 `PlannerClient` 与 `NewApiExecutor` 都要求一个 OpenAI 兼容网关，而真实碧游宫/NewAPI 渠道
尚未接通（蓝图第二阶段未做）。因此本次用**桩网关**（`scripts/daojian/stub_gateway.py`）替代，
它在能力范围内忠实模拟两个角色：规划请求返回两段式计划，执行请求回显标记文本。

## 二、被测对象

| 项 | 值 |
|---|---|
| 阵图 | 进程内临时实例（`TestClient`，独立临时 SQLite） |
| 鉴权 | 每节点 token（`ZHENTU_NODE_TOKENS`），`X-Node-Id` 与 token 绑定 |
| 规划 | `PlannerClient` → 桩网关（`ZHENTU_PLANNER_MODEL=planner-stub`） |
| 执行 | `NewApiExecutor` → 桩网关（`ZHENTU_NEWAPI_BASE_URL`） |
| 聚合 | `concat`（默认策略） |
| 注册节点 | 两个假节点（sanxian-01 / quasheng-01，各声明 chat 能力） |

## 三、用例与结果

| 编号 | 场景 | 结果 | 关键数据 |
|---|---|---|---|
| E1 | 提交任务 | 通过 | `202` + `task_id` / `events_url` / `result_url` |
| E2 | 事件流 | 通过 | `task/created → task/planned → step/started ×2 → step/result ×2 → aggregate/started → aggregate/result → task/completed` |
| E3 | 最终结果 | 通过 | `status=completed`，两步结果按 `input_step_ids` 顺序拼接 |
| E4 | 幂等 | 通过 | 同 `request_id` 重提 → `200` + `duplicate=true` + 同一 `task_id` |
| E5 | 越权 | 通过 | 非发起节点读任务 → `403 task_forbidden` |
| E6 | 取消 | 通过 | 执行中取消 → `202`，终态 `canceled` |
| E7 | 能力缺失 | 通过 | 无节点声明 `rerank` → `/route` 返回 `200 selected=null reason=no_route` |

连续跑 3 次，均为 7/7，耗时 3.1–3.3s。

## 四、实测中的关键发现

### 1. 默认配置下「拆多子任务」根本不会发生

`zhentu/config.py` 的 `planner_model` 默认是空串，`PlannerClient` 在 base_url/model/token 任一为空时
**直接回退 `SingleStepPlanner`**。也就是说：不显式设置 `ZHENTU_PLANNER_MODEL`，阵图永远是单步执行，
「复杂请求自动拆多子任务并行」这条验收在默认部署下不成立。本次必须显式设置才观察到两步计划。

> 建议：要么在 README/部署模板里写明「多步编排需配置 `ZHENTU_PLANNER_MODEL`」，要么让阵图在
> 未配置规划模型时把 `task/planned` 事件带上 `planner=single_step` 标记，方便运维发现。

### 2. 取消是「批次边界」语义，不是「立即中断」

`runner.py` 只在每个批次执行完后检查 `cancel_requested`。因此：

- 首步执行中发取消 → 首步跑完、下批次前退出 → `task/canceled`；
- 两步都跑完后、聚合前发取消 → 直接 `task/canceled`，**没有** `step/canceled` 事件。

这不符合「取消即现有步骤尽快停止」的直觉。契约 4.3 的 `step/canceled` 文案是「未启动步骤写 canceled」，
所以行为与契约一致；但用户点取消后最多要等一个步骤完整跑完（LLM 场景可达数十秒）。
> 建议：在 `docs/orchestration-contract.md` 显式写明「取消在批次边界生效，在途步骤不中断」，
> 或在 UI 上把取消态显示为「正在停止」。

### 3. 规划失败时静默回退，不可观测

桩网关规划请求返回非法内容时，`PlannerClient` 会吞掉异常直接回退单步，`task/planned` 事件里没有任何标记。
排查「为什么没拆步」时只能翻代码。本次调试就因为这一点多花了不少时间。

### 4. 无节点声明能力时，计划步骤仍然会被创建

`/route` 对 `rerank` 正确返回 `no_route`，但规划阶段并不检查能力可用性——计划照建，执行时才失败。
契约 4.2 说 PlannerClient 输入包含「当前可用能力/模型/limits 快照」，实现里**没有**把快照传给规划模型，
所以规划器无从知道某能力不可用。这是契约与实现的偏差。

### 5. `sources` 只含路由成功步骤，与事件数可能不一致

E3 实测 `sources` 为 2（与 `step/result` 数量一致）是对的；但 `runner.py` 里 `sources` 只在
`outcome["source"]` 非空时追加，执行器不返回 source 时会出现「有结果、无来源」。本次桩执行器恰好
返回了 source，所以没暴露；真实 NewAPI 路径需要确认。

## 五、限制（必须与本报告一起读）

1. **网关是桩，不是 NewAPI**。链路结构、状态机、事件流、账本、聚合策略都真实执行了；
   但**没有验证**真实 NewAPI 的鉴权、限流、模型映射、SSE 断流行为——那是第二阶段的工作。
2. **节点是注册表里的假条目**，推理由桩回显，不是准圣/散仙真实推理。真实推理延迟、
   并发上限、降载联动已在 `daojian-2026-11-20.md` 的 D1–D8 单独验证过。
3. 单进程内 `TestClient`，未验证多 worker 下的账本并发（契约 1.6 已声明部署约束为单进程）。
4. 前端（散仙 App 集群模式）未参与本次实测，只验证了服务端链路。

## 六、复现

```bash
# 内联桩网关 + 临时阵图实例，7 个用例
python3 scripts/daojian/orchestration_e2e.py --json out/e2e.json --strict

# 单独起桩网关（调试用）
python3 scripts/daojian/stub_gateway.py --port 18080
```

## 七、修复记录（2026-11-21 同一会话内完成）

第四节的三处发现已修复，契约同步更新为补充说明（未升版本，均为可选字段与语义澄清）：

| 发现 | 修复 | 落点 |
|---|---|---|
| 1 默认不拆步且不可观测 | `task/planned` 新增 `planner_source`（`planner`/`fallback`/`single_step`/`invalid_plan`）与 `planner_note` | `zhentu/orchestration.py`、`zhentu/runner.py`、契约 4.3 |
| 2 取消语义不明确 | `task/canceled` 新增 `cancel_scope="batch_boundary"`、`pending_steps`、`in_flight_completed`；契约写明「在途步骤不中断」 | `zhentu/runner.py`、契约 4.3 |
| 3 规划器拿不到能力快照 | Orchestrator 注入 `capability_snapshot.available`；规划提示带上可用能力；仍返回无人可用的能力时在计划阶段剔除并记录 `dropped_steps` | `zhentu/app.py`、`zhentu/orchestration.py`、契约 2.1/4.2 |

验证：

- 默认配置（不设 `ZHENTU_PLANNER_MODEL`）下 `task/planned` 现在明确给出
  `{"planner_source": "fallback", "planner_note": "planner_not_configured：未配置规划模型或凭据（ZHENTU_PLANNER_MODEL）"}`，
  不再需要翻代码判断为何只有一步。
- 无人可用的能力步骤在计划阶段被剔除，`aggregate.input_step_ids` 同步收敛。
- 新增测试 12 条（`zhentu/tests/test_planner_observability.py` 10 条 + `test_tasks.py` 2 条）。
- 回归：zhentu+quasheng **283 passed**；散仙 shared 83 + composeApp 23 全绿；E2E 仍 7/7。

**未修**：发现 5（`sources` 可能少于结果数）—— 需要在真实 NewAPI 执行器上确认，归入第二阶段。

## 八、真实网关重测（2026-11-21 04:09，`stub=false`）

拿到 NewAPI token 后，用真实网关（`http://127.0.0.1:3000/v1`）+ 准圣节点重跑了编排链路。

### 第一次尝试：失败，根因是 Ollama 卡死

| 事件 | 时间 | 内容 |
|---|---|---|
| `task/planned` | +30.0s | `planner_source=fallback`、`planner_error：TimeoutError` |
| `step/failed` | +120.0s | `step_timeout` |
| `task/failed` | +120.0s | `required_step_failed` |

排查发现 Ollama 进程（PID 81644，运行 13 天）处于**假活**状态：`/api/tags`、`/api/ps` 秒回，
但任何生成请求（`/v1/chat/completions` 与 `/api/generate`）都永久挂起；`/api/ps` 显示模型"已加载"
且 `expires_at=2319 年`，实际已僵死。重启 Ollama 后冷加载生成 1.5s 返回。

> 这一条**不是因为昨天的三个修复才发现** —— 正是 `planner_source`/`planner_note` 让
> "规划阶段 30 秒超时" 从不可见变成一行可读事件，否则只会看到一个 "steps: 1"。

### 第二次尝试：链路通，但没拆步

| 项 | 结果 |
|---|---|
| 任务终态 | `completed`，总耗时 **4.6s** |
| `answer` | 「负载均衡是通过分配和管理服务器资源，实现不同访问请求的均衡处理…」 |
| `sources` | 1 条（s1 / sanxian-01 / qwen2.5:0.5b） |
| `usage` | prompt 36 / completion 26 / steps 1 |
| `task/planned` | `planner_source=fallback`、`planner_invalid：plan.max_steps 必须在 2..12` |

**结论：真实链路（提交→规划→选路→执行→聚合→结果）已打通**，但「自动拆多子任务」仍然没有发生。

### 拆步失败的真实原因：规划模型太小

直接呼叫 `qwen2.5:0.5b` 让它输出 Plan JSON，得到：

```json
{"plan_id":1,"intent":"说明","max_steps":1,"max_parallel":1,
 "steps":[{id:1,"goal":"负载均衡","model_hint":"负载均衡是一种网络技术…",
           "input":{"messages":["请告诉我服务器上的负载平衡",…]},
           "depends_on":[],"parallel_group":[],"required":[],
           "failure_policy":"重试", …}],
 "aggregate:{mode:"step","input_step_ids":["1"]}}
```

问题清单：JSON 键名未加引号（非法 JSON）、`id` 是数字、`max_steps` 小于步骤数、`failure_policy`
是中文"重试"、`aggregate.mode` 是 `step`（非法）、输出中文文本塞进 `messages`。
`validate_plan` 正确地拒绝了它并回退单步——**校验逻辑是对的，模型能力不够**。

### 对第三步验收的影响

蓝图第三步验收「复杂请求自动拆多子任务并行」在当前环境**尚不成立**，因为：

1. 本机只有 `qwen2.5:0.5b` 与 `nomic-embed-text` 两个模型，前者不具备稳定的结构化输出能力；
2. `PlannerClient` 已有能力快照与严格校验，但没有针对弱模型的引导（few-shot 示例或重试）。

**建议**（未实施，需要决策）：

- 方案 A：换一个 7B 级模型做规划（如 `qwen2.5:7b`），最直接，但本机需下载 4.7GB；
- 方案 B：给 `PlannerClient` 加 1–2 次「格式纠错重试」——把校验错误回喂给模型再试，弱模型也可能成功；
- 方案 C：接受单步，把「拆步」降级为可选能力，蓝图验收改写为「具备拆步能力，依赖规划模型规格」。

我倾向 **B + A 组合**：先加纠错重试（代码小、对所有模型都有益），再由你决定要不要拉 7B 模型。

## 九、方案 B + A 实施结果（2026-11-21 05:00–07:30）

### B：规划格式纠错重试

`PlannerClient` 现在把校验错误回喂给模型重试（默认 1 次，`ZHENTU_PLANNER_RETRIES` 可配 0–3）：

- 校验失败/JSON 解析失败 → 带「上一次错误」重试；`HTTPError` 与网络异常不重试（重试无意义）；
- 规划超时独立可配（`ZHENTU_PLANNER_TIMEOUT_S`，默认 30s），CPU 上的 7B 需要更长；
- 提示词补齐硬性约束：键名必须加双引号、`failure_policy` 只能是 `fail_task|partial`、
  `aggregate.mode` 只能是 `concat|llm`、`max_steps >= 实际步骤数`。

**实测（0.5B + 2 次重试）**：

```json
{"steps": 2, "planner_source": "planner", "planner_note": "第 2 次尝试成功"}
```

0.5B 首次输出必然非法，**靠重试救回并成功拆出 2 步**，任务 14 秒完成。
修复前的行为是「一次失败即回退单步」，这条链路现在才真正可用。

### A：换 7B 规划模型

`ollama pull qwen2.5:7b`（4.7GB）完成后：

| 场景 | 结果 |
|---|---|
| 7B 直出 Plan JSON | 一次通过，合法 JSON，拆出 4 步 |
| 经阵图编排（超时 300s） | `planner_source=planner`，**2 步全部成功**，109s，`completed` |
| 事件流 | `planned(2) → s1 ok → s2 ok → aggregate → completed` |

**蓝图第三步验收「复杂请求自动拆多子任务并行」在 7B 上首次真正达成。**

### 仍未打通的：NewAPI 渠道没有 7B

```
{"code":"model_not_found","message":"No available channel for model qwen2.5:7b under group default"}
```

NewAPI 只配置了 `qwen2.5:0.5b` 与 `nomic-embed-text:latest` 两个渠道。本次 7B 验证是**阵图直连 Ollama**
（`ZHENTU_NEWAPI_BASE_URL=http://127.0.0.1:11434/v1`）完成的，绕过了 NewAPI。
要在完整拓扑（阵图→NewAPI→节点）下验证，需要在 NewAPI 后台把 `qwen2.5:7b` 加入渠道——需要 admin 权限。

### 配置参考

```bash
ZHENTU_PLANNER_MODEL=qwen2.5:7b        # 规划模型；0.5B 需配合重试
ZHENTU_PLANNER_TIMEOUT_S=300           # CPU 上的 7B 生成整段计划需要更长时间
ZHENTU_PLANNER_RETRIES=1               # 格式纠错重试次数（0-3）
```

## 十、结论

编排链路**服务端部分是通的**：提交、事件流、两步计划的顺序执行、聚合、幂等、越权、取消、选路降级
全部符合预期，连续 3 次稳定 7/7。

但有三处**值得在第二阶段之前处理**：

- 默认配置不拆步（发现 1）——影响「复杂请求自动拆多子任务」这条蓝图验收的真实性；
- 取消语义需要写进契约或 UI（发现 2）；
- 规划器拿不到能力快照，能力不可用要到执行时才失败（发现 4）。

真实网关接入后，E1–E7 应该用 `--gateway-base-url` 指向 NewAPI 重跑一遍，届时 `stub=false`。

---

**更新（B+A 之后）**：拆步能力已可用。7B 一次通过、0.5B 靠重试也能成功。剩余缺口是 NewAPI 渠道未配置 7B，
导致「阵图→NewAPI→节点」完整链路下仍只能走 0.5B。补齐渠道配置后应重跑本报告第二节的 E1–E7。

## 十一、真实网关 E1–E7 重测（2026-11-21，`stub=false`）

NewAPI 渠道已补上 `qwen2.5:7b`（`/v1/models` 可见、`/v1/chat/completions` 实测返回 `OK`），
E2E 脚本也补了真实网关模式，按第二节的 E1–E7 完整重跑。

### 脚本改动（`scripts/daojian/orchestration_e2e.py`）

| 项 | 桩模式（默认） | 真实网关模式（`--gateway-base-url`） |
|---|---|---|
| 密钥 | 固定 `stub-key` | `--gateway-api-key` 或 `ZHENTU_NEWAPI_API_KEY`，缺失直接退出 |
| 规划模型 | `planner-stub` | `--planner-model`（本次 `qwen2.5:7b`） |
| 节点模型 | `e2e-chat` | `--chat-model`（本次 `qwen2.5:7b`） |
| 规划/执行超时 | 默认 | `ZHENTU_PLANNER_TIMEOUT_S=300`、`ZHENTU_NEWAPI_TIMEOUT_S=300` |
| 任务截止 | 60s | `--deadline-s`（本次 600s，契约上限） |
| E3 断言 | 桩回显文本按序拼接 | `completed` + 步骤数 ≥2 + `sources` 与结果数一致 + 答案非空 |
| E6 等待 | 20s | 300s（取消在批次边界生效） |
| 结果 JSON | `stub=true` | `stub=false` + `planner_model`/`chat_model`/`deadline_s` |

### 重测结果

真实拓扑：阵图（进程内临时实例）→ `http://127.0.0.1:3000/v1`（NewAPI `v1.0.0-rc.48`）→ Ollama `qwen2.5:7b`。

| 次数 | 结果 | 耗时 | 关键数据 |
|---|---|---|---|
| 1（脚本首版） | 7/7 | 100.3s | 主任务 2 步、`sources=2` |
| 2 | 6/7 | 126.6s | **E3 超时**：`_wait_via` 固定 60s，主任务仍在 running |
| 3（修复等待后） | 7/7 | 172.8s | 2 步、`sources=2`、answer 327 字符 |
| 4 | 7/7 | 162.4s | 2 步、`sources=2`、answer 152 字符 |
| 5（最终版本） | 7/7 | 185.8s | `planner_source=planner`、2 步、answer 568 字符 |

> 第 2 次失败是**测试工具缺陷**，不是产品缺陷：真实 7B 规划 + 两步执行超过脚本写死的 60s 等待窗口。
> 修复后连续 3 次（第 3–5 次）均为 7/7。

各用例与桩模式一致：E1 `202`+urls、E2 事件链完整（`planned→2×started/result→aggregate→completed`）、
E4 幂等 `duplicate=true`、E5 越权 `403 task_forbidden`、E7 无能力 `no_route`。

E6 取消这次观察到更完整的一次：规划给出 **4 步**，取消在首批尚未启动时到达，
事件流为 `task/planned → step/canceled ×4 → task/canceled`；与契约「未启动步骤写 canceled、在途步骤不中断」一致。

E2 现在会把 `task/planned` 载荷一起写进结果 JSON，本次 `{"steps": 2, "planner_source": "planner"}`，
后续排查「为什么没拆步」不用再翻代码。

### 这次重测的边界（仍需记住）

1. **注册节点仍是假条目**：模型声明为 `qwen2.5:7b`，但执行走的是
   `阵图 → NewAPI → Ollama`，**没有**打到准圣/散仙的 `/v1/chat/completions` 端点。
   因此本次验证的是「网关链路」，不是「节点端点链路」。
2. NewAPI 的 `qwen2.5:7b` 渠道是**手工在后台加的**，仓库里还没有渠道初始化脚本；
   渠道/权重/心跳的代码化属于第二步，自动化部署模板属于第五步。
3. `sources` 与结果数一致（本次 2/2），第八节提到的「执行器不返回 source 时来源缺失」仍未在
   真实节点路径上验证。

### 复现

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/daojian/orchestration_e2e.py \
    --gateway-base-url http://127.0.0.1:3000/v1 \
    --planner-model qwen2.5:7b --chat-model qwen2.5:7b \
    --deadline-s 600 --json scripts/daojian/out/e2e-real.json --strict
```

### 对第三步验收的影响

「复杂请求自动拆多子任务」在**真实网关 + 7B** 下已稳定成立（本次连续 3 次拆出 2 步并完成聚合）。
第三步剩余工作只剩「把注册节点换成真实的准圣/散仙并验证节点端点路径」，可在第二步网关联调时一并完成。
