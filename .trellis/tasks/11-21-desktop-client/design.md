# 设计 · 碧游宫桌面客户端

## 目标

交付 macOS / Windows 桌面客户端：默认单机自闭环，用户主动开阵后担任阵眼；复用 dsh 的会话与 Agent 能力，但不让 dsh 取代碧游宫的集群级编排。

## 关键决策

1. **壳与运行时**：基于 dsh 的 Electron 壳；固定 dsh `0.2.2-rc.5`、Electron `44.7.0`、Node `24.22.3`。
2. **本地控制面用 Node/TypeScript**：桌面端不打包 Python sidecar；本地任务板、联阵服务、模型适配都在 Electron 主进程/本地服务中实现。Python `zhentu` 仅作为可选联阵后端。
3. **本地模型 V2.0-A 接 Ollama**：检测 `127.0.0.1:11434`，通过 OpenAI 兼容端点调用；不把 Ollama 塞进安装包。严格自闭环的 llama.cpp 内嵌列为 V2.0-B。
4. **dsh 只做单 Agent 运行时/UI**：禁用 dsh 的集群级 Agent Teams 编排；跨 Agent 调度属于阵图/联阵，不属于桌面客户端。
5. **联阵默认关闭**：启动为单机，不广播、不监听；用户点「开阵」后才启动 LAN 服务并成为阵主。
6. **数据本地优先**：配置、任务、产物、日志写入 `app.getPath("userData")`，不写仓库目录。

## 架构

```text
┌─────────────────────────────────────────────┐
│ Electron 主进程                              │
│  ├─ 本地控制面（任务板/租约/预算）           │
│  ├─ 联阵服务（UDP 发现 + HTTP/SSE）          │
│  ├─ 模型适配（Ollama OpenAI 兼容）           │
│  ├─ 存储（SQLite + 产物目录）                │
│  └─ dsh 子进程/运行时                        │
├─────────────────────────────────────────────┤
│ preload（受控 IPC）                          │
├─────────────────────────────────────────────┤
│ 渲染进程：dsh UI + 碧游宫壳 + 观星台/开阵页  │
└─────────────────────────────────────────────┘
```

## 组件

| 组件 | 职责 |
|---|---|
| `electron/main` | 窗口、单实例锁、生命周期、子进程清理 |
| `local-control` | 本地阵眼、任务状态、派差、回禀、预算 |
| `model-adapter` | Ollama 探活、模型列表、chat/embeddings |
| `lan-host` | 开阵/散阵、UDP 发现、入阵、心跳、踢人、SSE |
| `storage` | SQLite 任务板/账本、藏经阁目录、配置 |
| `ui` | dsh UI、碧游宫壳、观星台、开阵弹窗 |
| `secrets` | 模型密钥、阵主令牌；只留主进程/系统密钥链 |

## 运行模式

### 单机模式（默认）

- 本地控制面运行，但不广播。
- 本地模型可用时完成「目标 → 任务 → 执行 → 产物」。
- 无 Docker、无 NewAPI、无阵图、无灵脉依赖。

### 联阵模式（用户主动开阵）

- 开阵二选一：开放阵/闭阵。
- 启动 UDP 发现与 HTTP 会话；遵循 `docs/lan-formation-contract.md`。
- 入阵无阵主确认；阵主可踢人；散阵后各端回单机。
- 联阵任务可派给外门仙徒；本地任务板记录派差与回禀。

## 本地控制面

- 任务状态：pending / in_progress / completed / deleted。
- 最小字段：ticket_id、title、goal、tier、risk、owner、blocked_by、revision、lease_expires_at。
- V2.0 先做单机串行/有限并发，不做分布式复制。
- 任务视图紧凑化，不把完整任务板塞进模型上下文。
- 预算：max_workers、max_input_tokens、max_output_tokens、max_cost、max_wall_time；超限暂停待定夺。

## 模型适配

- 探活：`GET http://127.0.0.1:11434/api/version` 或 `/v1/models`。
- 推理：`POST /v1/chat/completions`，流式 SSE。
- 模型列表：`GET /v1/models`；能力声明缺失时按 `chat` 处理。
- 不把模型密钥写入渲染进程；不把调用方 Authorization 透传给 Ollama。

## 联阵实现约束

- 不在桌面端复用 Python 验证服务；按契约用 Node 实现。
- 开放阵/闭阵均无阵主确认；校验通过即入阵。
- 阵主接口只接受回环 + `X-Biyou-Host-Token`。
- 散阵/退出必须停止 UDP 公告、关闭 SSE、撤销 token。

## 安全

- `contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`。
- 渲染进程只通过 preload 白名单 IPC 访问主进程能力。
- 密钥只存主进程/系统密钥链；日志脱敏。
- 开阵默认关闭；开放阵显示风险提示。
- 退出时清理 dsh/模型/联阵子进程，避免残留。

## 非目标

- 不做移动端 App。
- 不做云账号/公网大厅。
- V2.0 不做签名、公证、自动更新。
- 不让 dsh Agent Teams 承担集群编排。

## 开放问题

1. V2.0-A 是否接受 Ollama 作为本机前置依赖；若要求严格自闭环，需切 V2.0-B 内嵌 llama.cpp。
2. 本地控制面是否直接复用 dsh 的会话存储，还是独立 SQLite；当前倾向独立。
3. 观星台是独立页面，还是 dsh UI 的一个面板；当前倾向独立页面 + 壳内入口。
