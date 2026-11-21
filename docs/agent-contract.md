# 仙徒契约（Agent Contract）v1.0

> **定位**：Super-agent 架构下的**任务面协议**——定义仙箓、师门任务、师门令、派差、回禀与巡阵的数据形状、状态机与校验规则。
> **实现方**：阵眼（Router）、阵图（Registry / Task Board）、碧游宫客户端（功德殿 / 观星台）、内门与外门仙徒。
> **命名基准**：[docs/naming.md](./naming.md) v2.1；代码与协议字段一律使用英文标识符。
> **不包含**：模型推理端点（见 `node-agent-contract.md`）、集群任务编排与道果（见 `orchestration-contract.md`）、联阵发现与准入（见 `lan-formation-contract.md`）。

---

## 一、定位与边界

1. **新能力走独立命名空间**：本契约的全部端点位于 `/agent/v1/*`，不改动既有 `/v1/*`（节点推理）与 `/zhentu/v1/*`（阵图编排与节点注册）。
2. **与编排契约的分工**：
   - `/zhentu/v1/requests` 解决「一次请求怎么被拆成步骤、在哪些节点上执行、如何聚合」；
   - `/agent/v1/tickets` 解决「一件差事谁能接、授权到什么程度、如何回禀与验收」。
   - 师门任务可以**引用**编排任务（`ticket.orchestration_request_id`），但不重复定义 `RequestEnvelope` / `Plan` / `ResultEnvelope`。
3. **与节点注册的分工**：节点对 `/zhentu/v1/nodes/*` 的出站注册继续描述「进程与模型是否在线」；本契约的仙箓描述「道行、神通、法宝与限额」，两者按 `agent_id ↔ node_id` 关联，不合并。
4. **阵眼是唯一派差方**：只有持有当前 leader 身份的阵眼可以创建派差；仙徒只能领命、回禀、请求取消。
5. **师门令不可变**：任何范围扩大都必须新开师门任务与新的师门令。
6. **仅内网**：不接云端 Agent 服务，不对外暴露公网入口。

---

## 二、术语映射

| 叙事名 | 协议对象 / 字段 | 说明 |
|---|---|---|
| 仙箓 | `agent_card` | 身份、根骨、道行、神通、法宝、模型、限额、状态 |
| 玉册 | `agent_card` 集合 | 由阵图维护，支持按道行/神通/法宝筛选 |
| 师门任务 | `ticket` | 一件被授权的差事 |
| 师门令 | `mandate` | 不可变授权：范围、法宝、数据边界、香火额度、有效期 |
| 派差 | `assignment` | 把 ticket 交给某仙徒的一次尝试 |
| 领命 | `assignment.status=accepted` | 仙徒确认接单 |
| 回禀 | `report` | 仙徒向阵眼汇报状态与产物 |
| 巡阵 | `watchdog` | 命灯/租约异常检测与恢复动作 |
| 缘起 | `trigger_id` | 不可变事件标识，用于师门任务幂等 |
| 因果线 | `thread_id` | 事件流归属 |
| 香火额度 | `budget` | `max_workers` / `max_input_tokens` / `max_output_tokens` / `max_cost` / `max_wall_time_s` |

道行、品阶与风险的枚举值一律使用英文，见 4.1–4.3。

---

## 三、通用约定

| 项 | 约定 |
|---|---|
| 基础路径 | `/agent/v1/` |
| 内容类型 | `application/json`；事件流 `text/event-stream` |
| 契约版本 | 请求/响应头 `Contract-Version: 1.0`；对象内 `contract_version` 回显 |
| 鉴权 | `Authorization: Bearer <token>`；阵眼与阵图使用集群令牌，仙徒使用每仙徒令牌 |
| 身份 | 请求头 `X-Agent-Id` 必须与令牌映射一致；body 中的 `agent_id` 不得自述越权 |
| 时间 | RFC 3339 带时区；服务端时钟为判定依据，客户端 `sent_at` 仅用于展示 |
| 未知字段 | 必须忽略；新增字段必须可选 |
| 幂等 | 创建师门任务以 `trigger_id` 为幂等键；派差以 `assignment_id` + `fencing_token` 去重 |
| 错误体 | `{"error": {"code": "...", "message": "...", "type": "agent_error", "param": null}}` |
| 脱敏 | 事件、回禀、日志不记录符令、完整 prompt；大产物用 `artifact_ref` |

错误码为稳定枚举，客户端按 `code` 分支，不解析 `message` 文本。完整清单见第六章。

---

## 四、枚举定义

### 4.1 根骨（`root`）

| 值 | 含义 |
|---|---|
| `inner` | 内门仙徒（PC 端，重推理 / 多法宝） |
| `outer` | 外门仙徒（移动端，轻神通 / 少法宝） |

> `quasheng` / `sanxian` 仅作兼容别名接收，服务端归一为 `inner` / `outer` 后存储。

### 4.2 道行（`cultivation`）与品阶（`tier`）

道行为整数 `1..9`，品阶为整数 `1..8`。

| cultivation | 道行 | 可接最高品阶 |
|---|---|---|
| 1 | 人仙 | 1（黄） |
| 2 | 地仙 | 2（玄） |
| 3 | 天仙 | 3（地） |
| 4 | 玄仙 | 4（天） |
| 5 | 金仙 | 5（宇） |
| 6 | 太乙金仙 | 6（宙） |
| 7 | 大罗金仙 | 7（洪） |
| 8 | 准圣 | 8（荒） |
| 9 | 混元 | 8（荒，含掌门/长老级授权，保留） |

| tier | 品阶 | 最低道行 |
|---|---|---|
| 1 | 黄 | 1 |
| 2 | 玄 | 2 |
| 3 | 地 | 3 |
| 4 | 天 | 4 |
| 5 | 宇 | 5 |
| 6 | 宙 | 6 |
| 7 | 洪 | 7 |
| 8 | 荒 | 8 |

硬约束：`ticket.min_cultivation == ticket.tier`，`agent.cultivation >= ticket.min_cultivation` 才具备接单资格。

> 早期 PRD 曾写作 `cultivation: 0–8`，本契约以命名规范 v2.1 的 `1–9` 为准；`0` 视为非法值，返回 `invalid_cultivation`。

### 4.3 风险（`risk`）

| 值 | 含义 | 授权要求 |
|---|---|---|
| `qingxiu` | 清修：只读，无副作用 | 师门令允许只读法宝 |
| `rushi` | 入世：有限写、可回滚 | 师门令 + 定夺 |
| `pozhen` | 破阵：重启、变更服务 | 师门令 + 护道 + 定夺 |
| `chuandao` | 传道：外发、对外通信 | 师门令 + 定夺 |

风险强度顺序：`qingxiu < rushi < pozhen < chuandao`。师门令的 `scope.risk_max` 必须覆盖任务风险。

### 4.4 状态枚举

师门任务 `ticket.status`：

| 值 | 中文 | 说明 |
|---|---|---|
| `pending` | 待派 | 已创建、尚无派差 |
| `queued` | 候差 | 有合格仙徒但忙碌；按品阶降序 + 截止时间升序排队 |
| `bounty` | 悬赏 | 无合格仙徒；通知功德殿，不降阶派差 |
| `running` | 执行中 | 至少一个派差处于 `accepted` / `running` |
| `reported` | 回禀 | 已收到终态回禀，等待复核 |
| `verifying` | 验收 | 护道复核 / 人工验收中 |
| `completed` | 已完成 | 验收通过，功德可入账 |
| `failed` | 失败 | 不可恢复失败，或全部派差失败 |
| `canceled` | 取消 | 用户/阵眼取消；批次边界语义，不打断在途执行 |

派差 `assignment.status`：

| 值 | 说明 |
|---|---|
| `offered` | 阵眼已发出，等待仙徒领命 |
| `accepted` | 仙徒已领命，尚未开始执行 |
| `running` | 执行中 |
| `reported` | 仙徒已提交终态回禀 |
| `released` | 租约到期或主动释放，任务可重派 |
| `revoked` | 被阵眼撤销（改派/取消） |

回禀 `report.status`（以命名规范为准）：

| 值 | 说明 |
|---|---|
| `accepted` | 已领命，准备开始 |
| `progress` | 进度更新，非终态 |
| `waiting` | 等待外部条件（定夺、依赖、资源） |
| `ready` | 道果就绪，等待验收（旧 PRD 的 `done` 归一为 `ready`） |
| `blocked` | 受阻，需要阵眼/人介入 |
| `failed` | 执行失败 |
| `dropped` | 主动放弃并交还任务 |

> 只有 `ready` / `failed` / `dropped` 是回禀终态；`accepted` / `progress` / `waiting` / `blocked` 不结束派差。

---

## 五、数据对象

字段标注：`R` 必填、`O` 可选。所有对象均含 `contract_version`。

### 5.1 仙箓 `agent_card`

```json
{
  "agent_id": "inner-01",
  "name": "内门·01",
  "root": "inner",
  "cultivation": 3,
  "merit": 120,
  "skills": ["chat", "summarize", "diagnose"],
  "tools": ["read_file", "http_get"],
  "tool_permissions": {
    "read_file": "qingxiu",
    "http_get": "qingxiu"
  },
  "preferred_tools": ["read_file"],
  "models": [
    {
      "model": "qwen2.5:7b-instruct-q4_K_M",
      "capabilities": ["chat"],
      "context_len": 32768
    }
  ],
  "limits": {
    "max_concurrency": 4,
    "max_context_len": 32768,
    "max_cost_per_ticket": 2.0
  },
  "location": { "workspace": "/var/biyou/agents/inner-01", "zone": "lan" },
  "status": "ready",
  "contract_version": "1.0",
  "updated_at": "2026-11-21T10:00:00+08:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `agent_id` | string | R | 全局唯一；与节点注册的 `node_id` 建立映射时为同值 |
| `name` | string | O | 展示名 |
| `root` | `inner` \| `outer` | R | 根骨 |
| `cultivation` | integer `1..9` | R | 道行 |
| `merit` | integer ≥ 0 | O | 功德；只由验收通过入账 |
| `skills` | string[] | R | 神通；canonical 能力名与节点契约一致（`chat`/`embedding`/`vision`/`ocr`/`rerank`），可扩展领域神通 |
| `tools` | string[] | R | 法宝技术名（`read_file`/`http_get`/`shell`/`restart_service`） |
| `tool_permissions` | object | R | 法宝 → 风险档；未声明即不可用 |
| `preferred_tools` | string[] | O | 本命法宝，仅作调度偏好 |
| `models` | array | R | 可用模型与能力 |
| `limits` | object | R | `max_concurrency` / `max_context_len` / `max_cost_per_ticket` |
| `location` | object | O | 洞府工作目录与网络区域，仅展示与隔离校验用 |
| `status` | `ready` \| `busy` \| `degraded` \| `offline` \| `draining` | R | `busy` 仍可接候差队列；`draining` 不再接新单 |
| `updated_at` | string | R | 玉册最后更新时间 |

规则：

1. `tools` 中的每一项必须在 `tool_permissions` 中声明风险档；否则视为非法仙箓，返回 `invalid_agent_card`。
2. `cultivation` 变更即生效；派差读取玉册最新值，不读缓存副本。
3. `limits.max_concurrency` 是并发闸门，达到上限不再接单。

### 5.2 师门任务 `ticket`

```json
{
  "ticket_id": "tkt-01HQ0001",
  "thread_id": "thr-01HQ0001",
  "trigger_id": "trg-01HQ0001",
  "title": "诊断节点温度过高",
  "goal": "定位 inner-01 温度告警根因并给出建议动作",
  "tier": 3,
  "min_cultivation": 3,
  "risk": "qingxiu",
  "required_skills": ["chat", "diagnose"],
  "required_tools": ["read_file"],
  "inputs": {
    "refs": ["artifact://alerts/alert-778"],
    "params": { "node_id": "inner-01" }
  },
  "expected_outputs": ["root_cause", "recommended_actions"],
  "depends_on": [],
  "orchestration_request_id": null,
  "budget": {
    "max_workers": 2,
    "max_input_tokens": 100000,
    "max_output_tokens": 20000,
    "max_cost": 1.5,
    "max_wall_time_s": 600,
    "currency": "CNY"
  },
  "deadline_at": "2026-11-21T12:00:00+08:00",
  "status": "pending",
  "current_assignment_id": null,
  "revision": 1,
  "created_by": "router",
  "created_at": "2026-11-21T10:05:00+08:00",
  "updated_at": "2026-11-21T10:05:00+08:00",
  "contract_version": "1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ticket_id` | string | R | 师门任务唯一 id |
| `thread_id` | string | R | 因果线；同一因果线同时只跑一个观星 |
| `trigger_id` | string | R | 缘起 id；创建幂等键 |
| `title` / `goal` | string | R | 标题与目标 |
| `tier` | integer `1..8` | R | 品阶 |
| `min_cultivation` | integer `1..9` | R | 必须等于 `tier`；不等返回 `invalid_ticket` |
| `risk` | enum | R | 风险四档 |
| `required_skills` | string[] | O | 神通要求，全匹配才可接 |
| `required_tools` | string[] | O | 法宝要求，仙徒须全部持有且权限覆盖风险 |
| `inputs` | object | O | `refs`（大对象引用）与 `params`（内联小参数） |
| `expected_outputs` | string[] | O | 期望产物键名，回禀 `artifacts` 按此命名 |
| `depends_on` | string[] | O | 前置 `ticket_id`，必须无环 |
| `orchestration_request_id` | string \| null | O | 关联 `/zhentu/v1/requests` 的 task_id，不复制其结构 |
| `budget` | object | R | 香火额度，见 5.2.1 |
| `deadline_at` | string | O | 截止时间；缺省由门规决定 |
| `status` | enum | R | 见 4.4 |
| `current_assignment_id` | string \| null | O | 当前有效派差 |
| `revision` | integer ≥ 1 | R | CAS 修订号；每次状态或关键字段变更 +1 |
| `created_by` | string | R | 通常为 `router` |
| `created_at` / `updated_at` | string | R | 时间 |

#### 5.2.1 香火额度 `budget`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `max_workers` | integer ≥ 1 | R | 并行仙徒上限 |
| `max_input_tokens` | integer ≥ 0 | R | 输入 token 上限 |
| `max_output_tokens` | integer ≥ 0 | R | 输出 token 上限 |
| `max_cost` | number ≥ 0 | R | 香火上限 |
| `max_wall_time_s` | integer ≥ 1 | R | 墙钟上限 |
| `currency` | string | O | 外部模型计价货币；本地模型可为 `compute_unit` |

规则：预检按**冷缓存（0% 命中）**估算；运行中达到 80% 发预警事件，达到 100% 暂停派差并通知功德殿/观星台，不静默降级。

### 5.3 师门令 `mandate`

```json
{
  "mandate_id": "mdt-01HQ0001",
  "ticket_id": "tkt-01HQ0001",
  "scope": {
    "tier_max": 3,
    "risk_max": "qingxiu",
    "skills": ["chat", "diagnose"],
    "tools": ["read_file"],
    "data_boundary": {
      "paths": ["/var/biyou/logs"],
      "network": ["lan"],
      "external_send": false
    }
  },
  "budget": {
    "max_workers": 2,
    "max_input_tokens": 100000,
    "max_output_tokens": 20000,
    "max_cost": 1.5,
    "max_wall_time_s": 600,
    "currency": "CNY"
  },
  "approvals": [
    { "kind": "human_decision", "by": "elder:zhanglao-01", "at": "2026-11-21T10:04:00+08:00" }
  ],
  "issued_by": "router",
  "issued_at": "2026-11-21T10:05:00+08:00",
  "expires_at": "2026-11-21T12:00:00+08:00",
  "immutable": true,
  "revoked": false,
  "contract_version": "1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mandate_id` | string | R | 不可变 |
| `ticket_id` | string | R | 归属师门任务，一对一 |
| `scope.tier_max` | integer `1..8` | R | 允许的最高品阶 |
| `scope.risk_max` | enum | R | 允许的最高风险 |
| `scope.skills` | string[] | O | 允许调用的神通白名单；缺省表示不额外限制 |
| `scope.tools` | string[] | R | 允许调用的法宝白名单 |
| `scope.data_boundary` | object | O | `paths` / `network` / `external_send` |
| `budget` | object | R | 与 ticket 一致或更严；不得超过 ticket |
| `approvals` | array | O | 定夺记录；`pozhen` / `chuandao` / 洪荒阶必须有 |
| `issued_by` | string | R | 阵眼 id 或 `elder:<id>` |
| `issued_at` / `expires_at` | string | R | 有效期 |
| `immutable` | boolean | R | 恒为 `true`；写 `false` 返回 `mandate_immutable` |
| `revoked` | boolean | R | 撤销后不可再用；已派差继续按原令执行到边界 |

规则：

1. 扩大范围（提高品阶上限、放宽风险、加法宝/路径）必须新建师门任务与新师门令，`POST` 修改一律 `409 mandate_immutable`。
2. 师门令撤销只阻止新的领命，不回溯撤销已完成的回禀。
3. `risk_max=pozhen|chuandao` 或 `tier_max>=7` 时 `approvals` 不得为空。

### 5.4 派差 `assignment`

```json
{
  "assignment_id": "asg-01HQ0001",
  "ticket_id": "tkt-01HQ0001",
  "agent_id": "inner-01",
  "mandate_id": "mdt-01HQ0001",
  "status": "offered",
  "attempt": 1,
  "lease": {
    "owner_agent_id": "inner-01",
    "fencing_token": 7,
    "expires_at": "2026-11-21T10:20:00+08:00"
  },
  "offered_at": "2026-11-21T10:06:00+08:00",
  "accepted_at": null,
  "finished_at": null,
  "reason": null,
  "contract_version": "1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assignment_id` | string | R | 派差唯一 id |
| `ticket_id` / `agent_id` / `mandate_id` | string | R | 关联对象 |
| `status` | enum | R | 见 4.4 |
| `attempt` | integer ≥ 1 | R | 同一 ticket 的第几次派差，重派递增 |
| `lease.owner_agent_id` | string | R | 租约持有者 |
| `lease.fencing_token` | integer ≥ 1 | R | 单调递增；旧令牌的回禀被拒绝 |
| `lease.expires_at` | string | R | 租约到期；到期未续即视为释放 |
| `reason` | string \| null | O | `released` / `revoked` 的原因码 |
| `contract_version` | string | R | 版本 |

### 5.5 回禀 `report`

```json
{
  "report_id": "rpt-01HQ0001",
  "ticket_id": "tkt-01HQ0001",
  "assignment_id": "asg-01HQ0001",
  "agent_id": "inner-01",
  "seq": 2,
  "status": "ready",
  "message": "根因为散热风扇转速异常，建议清理风道后复测",
  "artifacts": [
    { "key": "root_cause", "artifact_ref": "artifact://tickets/tkt-01HQ0001/root_cause.md", "media_type": "text/markdown" }
  ],
  "usage": {
    "input_tokens": 8200,
    "output_tokens": 1200,
    "cache_hit_tokens": 6000,
    "cache_miss_tokens": 2200,
    "cache_unknown": false,
    "cost": 0.08,
    "currency": "CNY",
    "latency_ms": 14500,
    "steps": 3
  },
  "trace_id": "trace-01HQ0001",
  "at": "2026-11-21T10:12:00+08:00",
  "contract_version": "1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `report_id` | string | R | 回禀唯一 id |
| `ticket_id` / `assignment_id` / `agent_id` | string | R | 归属 |
| `seq` | integer ≥ 1 | R | 同一派差内单调递增，用于乱序校验 |
| `status` | enum | R | 见 4.4 |
| `message` | string | O | 人类可读摘要，不得含符令 |
| `artifacts` | array | R | 可为空数组；每项 `key` / `artifact_ref` / `media_type` |
| `usage` | object | R | 见 5.5.1 |
| `trace_id` | string | R | 跨对象追踪 id |
| `at` | string | R | 仙徒本地时间，仅展示 |

#### 5.5.1 用量 `usage`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `input_tokens` / `output_tokens` | integer ≥ 0 | R | 输入/输出 token |
| `cache_hit_tokens` / `cache_miss_tokens` | integer ≥ 0 | O | 无法获取时置 0 且 `cache_unknown=true` |
| `cache_unknown` | boolean | O | `true` 时不得按命中价估算费用 |
| `cost` | number ≥ 0 | R | 本段香火消耗 |
| `currency` | string | O | 计价单位 |
| `latency_ms` | integer ≥ 0 | O | 端到端耗时 |
| `steps` | integer ≥ 0 | O | 模型调用步数 |

### 5.6 巡阵 `watchdog`

```json
{
  "watchdog_id": "wdg-01HQ0001",
  "agent_id": "inner-01",
  "ticket_id": "tkt-01HQ0001",
  "assignment_id": "asg-01HQ0001",
  "signal": "heartbeat_missing",
  "detected_at": "2026-11-21T10:21:00+08:00",
  "action": "reassign",
  "fencing_token": 8,
  "note": "命灯连续 3 个周期缺失，租约已过期",
  "contract_version": "1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `watchdog_id` | string | R | 事件唯一 id |
| `agent_id` | string | R | 涉事仙徒 |
| `ticket_id` / `assignment_id` | string \| null | O | 关联任务与派差 |
| `signal` | enum | R | `heartbeat_missing` / `lease_expired` / `task_timeout` / `stalled` / `orphan` |
| `detected_at` | string | R | 服务端判定时间 |
| `action` | enum | R | `reassign` / `release` / `cancel` / `notify` |
| `fencing_token` | integer | O | 恢复动作使用的新令牌 |
| `note` | string | O | 可读原因 |

巡阵判定规则：

1. 命灯缺失达到 3 个心跳周期，或租约过期，即判离线。
2. 离线派差标记 `released`，任务回到 `pending` 或 `queued` 并按规则重派；`attempt` 递增，`fencing_token` 递增。
3. 超过 `deadline_at` 未完成的任务，巡阵发 `notify` 并转 `bounty` 或交长老定夺。
4. 取消是批次边界语义：已领命的派差不被打断，未开始的部分不再派发。

---

## 六、状态机

### 6.1 师门任务

```text
            ┌──────────── bounty ◄──────── 无合格仙徒
            │                │
create ─► pending ──派差──► running ──终态回禀──► reported ──► verifying ──► completed
            │  ▲             │                                             └──► failed
            │  └── 有合格者忙碌 ──► queued ──仙徒空闲──► running
            │                  │
            └──────────────────┴──► canceled
```

| 迁跃 | 触发 | 备注 |
|---|---|---|
| `pending → running` | 创建派差并被领命 | 同时写 `current_assignment_id` |
| `pending → queued` | 有合格仙徒但并发已满 | 进入候差队列 |
| `pending → bounty` | 无合格仙徒 | 通知功德殿；不允许降阶硬派 |
| `queued → running` | 仙徒空闲 | 按品阶降序、截止时间升序取队首 |
| `bounty → running` | 新仙箓入册满足条件 | 玉册变更触发重评估 |
| `running → reported` | 收到 `ready` / `failed` / `dropped` 回禀 | |
| `reported → verifying` | 需护道复核或人工验收 | 洪/荒阶、破阵、传道必过 |
| `reported → completed` | 低风险任务自动验收通过 | 门规允许时 |
| `verifying → completed` | 复核通过 | 记功德 |
| `verifying → failed` | 复核否决 | 不记功德 |
| `running → pending` | 派差被释放且仍有重派机会 | `attempt` 递增 |
| 任意非终态 → `canceled` | 用户/阵眼取消 | 批次边界生效 |

非法迁跃返回 `409 ticket_state_invalid`。

### 6.2 派差

```text
offered ──领命──► accepted ──开始──► running ──终态回禀──► reported
   │                  │                 │
   └──超时/撤销────────┴─────────────────┴──► released / revoked
```

| 迁跃 | 触发 |
|---|---|
| `offered → accepted` | 仙徒 `POST /assignments/{id}/accept` |
| `accepted → running` | 首条 `progress` 回禀，或显式开始 |
| `running → reported` | `ready` / `failed` / `dropped` |
| `offered → released` | 领命超时 |
| 任意非终态 → `revoked` | 阵眼撤销 |

### 6.3 回禀顺序

- 同一派差内 `seq` 必须严格递增；重复 `seq` 返回 `409 report_seq_conflict`。
- `status=ready|failed|dropped` 后再收到非终态回禀返回 `409 report_after_terminal`。
- 旧 `fencing_token` 的回禀返回 `409 fencing_token_stale`，不改变任何状态。

---

## 七、派差校验与规则

### 7.1 候选筛选

阵眼按以下顺序过滤，前一步失败直接排除该仙徒：

1. 仙箓存在且 `status ∈ {ready, busy}`，非 `offline` / `draining`。
2. `agent.cultivation >= ticket.min_cultivation`（**不越阶**）。
3. `required_skills ⊆ agent.skills`。
4. `required_tools ⊆ agent.tools`，且每个法宝的 `tool_permissions` 风险档 ≥ `ticket.risk`。
5. 师门令未过期、未撤销，且 `scope` 覆盖 `tier` / `risk` / 所需法宝 / 数据边界。
6. 并发未达 `limits.max_concurrency`。
7. 剩余香火额度足够（按冷缓存估算）。

### 7.2 排序与优先

通过筛选的候选按以下顺序排序：

1. **刚好够用优先**：`agent.cultivation` 升序靠近 `min_cultivation`（高阶不轻用）。
2. 负载率升序（`active / max_concurrency`）。
3. 近期成功率降序。
4. 时延估计升序。
5. 功德/评分降序。

> **高阶可接低阶**（`cultivation > min_cultivation` 合法），但只是资格；排序会优先低阶合格仙徒。

### 7.3 无候选

- 有合格仙徒但忙碌 → `queued`（候差），按 `tier` 降序、`deadline_at` 升序排队。
- **无任何合格仙徒 → `bounty`**，返回或记录 `bounty_required`，并推送功德殿；**禁止静默降阶**。
- V2 不抢占：高阶任务到达时，在途低阶任务继续执行。

### 7.4 并发、CAS 与 fencing

- `POST /tickets/{ticket_id}/assignments` 必须带 `expected_revision`；不匹配返回 `409 revision_conflict`。
- 只有当前 leader 能派差；请求头 `X-Leader-Fencing-Token` 落后于阵图当前令牌时返回 `409 fencing_token_stale`。
- 同一票任何时刻最多一个有效派差（`status ∈ {offered, accepted, running}`）。
- 租约续期由仙徒心跳携带 `fencing_token`；令牌不匹配即拒绝。

---

## 八、端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/agent/v1/agents` | 注册/覆盖仙箓（幂等 upsert） |
| GET | `/agent/v1/agents` | 玉册查询（按 `root`/`cultivation`/`skills`/`tools`/`status` 过滤） |
| GET | `/agent/v1/agents/{agent_id}` | 单个仙箓 |
| POST | `/agent/v1/agents/{agent_id}/heartbeat` | 命灯续租，携带 `fencing_token` |
| DELETE | `/agent/v1/agents/{agent_id}` | 注销仙箓 |
| POST | `/agent/v1/tickets` | 创建师门任务；`X-Idempotency-Key: <trigger_id>` |
| GET | `/agent/v1/tickets` | 列表（按 `status`/`thread_id`/`tier` 过滤） |
| GET | `/agent/v1/tickets/{ticket_id}` | 单个师门任务 |
| POST | `/agent/v1/tickets/{ticket_id}/cancel` | 取消（批次边界；终态幂等） |
| POST | `/agent/v1/tickets/{ticket_id}/mandates` | 签发师门令（只增不改） |
| GET | `/agent/v1/tickets/{ticket_id}/mandates` | 师门令列表 |
| POST | `/agent/v1/tickets/{ticket_id}/assignments` | 创建派差（需 `expected_revision`） |
| GET | `/agent/v1/tickets/{ticket_id}/assignments` | 派差历史 |
| GET | `/agent/v1/assignments/{assignment_id}` | 单个派差 |
| POST | `/agent/v1/assignments/{assignment_id}/accept` | 领命 |
| POST | `/agent/v1/assignments/{assignment_id}/release` | 主动释放 |
| POST | `/agent/v1/assignments/{assignment_id}/reports` | 提交回禀 |
| GET | `/agent/v1/tickets/{ticket_id}/reports` | 回禀列表 |
| GET | `/agent/v1/watchdog/events` | 巡阵事件（SSE 或分页列表） |
| GET | `/agent/v1/events` | 全局事件流（SSE，支持 `Last-Event-ID`） |
| GET | `/agent/v1/health` | 本契约健康检查（免鉴权） |

### 8.1 HTTP 状态约定

| 场景 | 状态码 |
|---|---|
| 创建任务 / 派差 / 回禀 | `201` |
| 重复 `trigger_id` 创建任务 | `200`，返回已有 `ticket_id` |
| 幂等重复提交同一回禀 `seq` | `200`，返回已有 `report_id` |
| 未认证 / 令牌无效 | `401` |
| 非本仙徒操作他人派差 | `403` |
| 任务/仙徒/派差不存在 | `404` |
| 状态机非法迁跃、CAS 冲突、令不可变 | `409` |
| 香火额度超限 | `429` + `Retry-After` |
| 请求体不合约 | `422` |

### 8.2 错误码

| code | HTTP | 触发 |
|---|---|---|
| `agent_unknown` | 404 | 仙箓不存在 |
| `ticket_unknown` | 404 | 师门任务不存在 |
| `assignment_unknown` | 404 | 派差不存在 |
| `mandate_unknown` | 404 | 师门令不存在 |
| `ticket_state_invalid` | 409 | 非法状态迁跃 |
| `report_seq_conflict` | 409 | `seq` 重复或乱序 |
| `report_after_terminal` | 409 | 终态回禀后再报 |
| `revision_conflict` | 409 | CAS `expected_revision` 不匹配 |
| `fencing_token_stale` | 409 | 旧令牌派差/续租/回禀 |
| `mandate_immutable` | 409 | 尝试修改师门令 |
| `mandate_expired` | 409 | 师门令过期或撤销 |
| `tier_exceeded` | 422 | 仙徒道行低于任务最低道行（不越阶） |
| `skill_mismatch` | 422 | 缺少必需神通 |
| `tool_not_permitted` | 422 | 缺少法宝或权限风险档不足 |
| `mandate_violation` | 422 | 越权：风险/法宝/数据边界超出师门令 |
| `bounty_required` | — | 无合格仙徒，任务转悬赏（事件与审计字段，不一定是 HTTP 错误） |
| `budget_exceeded` | 409/429 | 香火额度耗尽，暂停并通知 |
| `invalid_agent_card` | 422 | 仙箓字段非法（法宝未申明权限等） |
| `invalid_cultivation` | 422 | `cultivation` 不在 `1..9` |
| `invalid_ticket` | 422 | `min_cultivation != tier` 等 |
| `not_leader` | 409 | 非当前 leader 尝试派差 |
| `rate_limited` | 429 | 限速 |

---

## 九、事件

`GET /agent/v1/events` 使用与编排契约一致的 SSE 形状：

```json
{
  "event_id": "aevt-01HQ0001",
  "seq": 12,
  "type": "ticket/status_changed",
  "at": "2026-11-21T10:12:05+08:00",
  "data": {
    "ticket_id": "tkt-01HQ0001",
    "from": "running",
    "to": "reported",
    "assignment_id": "asg-01HQ0001",
    "revision": 4
  }
}
```

事件类型（只增不改）：

| type | 说明 |
|---|---|
| `agent/registered` | 仙箓入册或更新 |
| `agent/offline` | 命灯熄灭 |
| `ticket/created` | 师门任务创建 |
| `ticket/status_changed` | 状态迁跃 |
| `ticket/queued` | 进入候差 |
| `ticket/bounty` | 转入悬赏 |
| `assignment/offered` | 派差发出 |
| `assignment/accepted` | 领命 |
| `assignment/released` | 释放/撤销 |
| `report/received` | 收到回禀（含非终态） |
| `budget/warning` | 香火达 80% |
| `budget/exceeded` | 香火达 100%，已暂停 |
| `watchdog/action` | 巡阵恢复动作 |

客户端用 `Last-Event-ID` 断点续传；未知 `type` 必须忽略。

---

## 十、与现有契约的关系

| 契约 | 负责 | 本契约的关系 |
|---|---|---|
| `node-agent-contract.md` | 节点 `/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` 与 `/zhentu/v1/nodes/*` 注册 | 本契约不新增节点入站端点；仙箓 `agent_id` 与节点 `node_id` 建立映射即可 |
| `orchestration-contract.md` | `/zhentu/v1/requests`、`Plan`、`TaskEvent`、`ResultEnvelope` | 师门任务可引用 `orchestration_request_id`；回禀 `artifacts` 可引用编排 `output_ref`；不复制其数据结构 |
| `lan-formation-contract.md` | mDNS/UDP 发现、「开阵/寻阵/入阵」 | 入阵成功后下发仙箓、进入玉册；离阵/阵散触发巡阵 `released` 与重派 |
| `naming.md` | 全部叙事名与协议字段对照 | 本契约的枚举与字段名以命名规范为准 |

不重叠清单：

- 不在本契约定义 `/v1/*` 或 `/zhentu/v1/*` 的任何端点或字段。
- 不在本契约定义模型价格表与计费公式（见成长任务 `11-21-taskboard-cost`）；只引用 `usage` 与 `budget` 形状。
- 不在本契约定义功德殿实现形态；只定义功德入账前置条件（`ticket.status=completed`）。

---

## 十一、版本策略

1. 本契约独立版本号，当前 `1.0`；与 `node-agent-contract.md`（`1.2`）、`orchestration-contract.md`（`1.0`）、`lan-formation-contract.md`（`1.0`）互不联动。
2. 新增可选字段不升 major；删除字段、改变语义、收紧枚举升 major。
3. 枚举只增不改；未知值按 `unknown` 处理，不得崩溃。
4. 破坏性变更必须先在次版本同时接受新旧字段，观察一个版本后再移除。
5. Schema 与 fixtures 必须同步更新：`docs/schemas/agent/v1/agent-contract.schema.json`、`docs/fixtures/agent/objects.v1.json`。

---

## 十二、待确认

以下决策不阻塞本契约落地，但实现前需与父任务对齐（见 `.trellis/tasks/11-21-biyou-superagent/prd.md` Open Decisions）：

1. 仙徒法宝权限在 V2.0 是否默认只读（本契约按「由仙箓声明、师门令限制」处理，不预设默认值）。
2. 洪/荒阶与 `pozhen` / `chuandao` 的护道复核人：高阶仙徒、长老，或两者其一即可。
3. 候差超期后的默认动作：通知功德殿、转悬赏，还是长老定夺中断低阶任务。
4. `bounty` 状态条目的对外通知通道（客户端内提示 / 灵脉告警）。
5. 巡阵判定周期与阈值是否复用节点注册的 `ttl = clamp(3 × interval, floor, 600)`。
6. 宗族级 `tier=8` 是否允许外门仙徒在长老定夺后接取（当前规则允许，需业务确认）。

---

## 十三、参考

- [docs/naming.md](./naming.md) — 命名、道行九境、品阶与风险
- [docs/node-agent-contract.md](./node-agent-contract.md) — 节点推理与注册
- [docs/orchestration-contract.md](./orchestration-contract.md) — 编排、事件、结果
- [docs/lan-formation-contract.md](./lan-formation-contract.md) — 开阵、寻阵、入阵
- `.trellis/tasks/11-21-biyou-superagent/prd.md` — R1–R15
- `.trellis/tasks/11-21-agent-contracts/prd.md` — 本任务验收
- `docs/schemas/agent/v1/agent-contract.schema.json` — JSON Schema
- `docs/fixtures/agent/objects.v1.json` — 示例对象
