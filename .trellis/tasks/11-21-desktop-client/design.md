# 设计 · 碧游宫桌面客户端

## 目标

交付 macOS / Windows 桌面客户端：默认单机自闭环，用户主动开阵后担任阵眼；复用 dsh 的会话与 Agent 能力，但不让 dsh 取代碧游宫的集群级编排。

## 关键决策

1. **壳与运行时**：基于 dsh 的 Electron 壳；固定 dsh `0.2.2-rc.5`、Electron `44.7.0`、Node `24.22.3`。
2. **本地控制面用 Node/TypeScript**：桌面端不打包 Python sidecar；本地任务板、联阵服务、模型适配都在 Electron 主进程/本地服务中实现。Python `zhentu` 仅作为可选联阵后端。
3. **本地模型走 dsh 插件，内置 llama.cpp**：由 `@biyou/dsh-local-models` 自管运行时与模型（回环 `127.0.0.1:18434`），以 `biyou-local` provider 注册进 dsh；不依赖外部 Ollama，也不把它塞进安装包。外部 Ollama 仅作可选 provider，不是 V2.0-A 前提。**V2.0-A 已按此交付**（2026-11-22，见 `11-22-local-models-plugin`）。
4. **dsh 只做单 Agent 运行时/UI**：禁用 dsh 的集群级 Agent Teams 编排；跨 Agent 调度属于阵图/联阵，不属于桌面客户端。
5. **联阵默认关闭**：启动为单机，不广播、不监听；用户点「开阵」后才启动 LAN 服务并成为阵主。
6. **数据本地优先**：配置、任务、产物、日志写入 `app.getPath("userData")`，不写仓库目录。
7. **归属边界**：机制属于 dsh，代码属于碧游宫；功能要进 dsh 界面/能力上下文才写插件，只管进程与联阵则留客户端本体。详见文末「归属与交互边界」。

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

1. ~~V2.0-A 是否接受 Ollama 作为本机前置依赖；若要求严格自闭环，需切 V2.0-B 内嵌 llama.cpp。~~ → 已决：V2.0-A 直接交付内置 llama.cpp 插件，不依赖外部 Ollama（2026-11-22）。
2. 本地控制面是否直接复用 dsh 的会话存储，还是独立 SQLite；当前倾向独立。
3. ~~观星台是独立页面，还是 dsh UI 的一个面板；当前倾向独立页面 + 壳内入口。~~ → 已决：做成 dsh UI 的 `main` 面板（keyed slot），壳内不再渲染自有业务页。见下节「呈现路线」。

---

## 呈现路线：走 dsh 客户端插件（2026-11-21 决策）

> 本节取代「架构」一节里「渲染进程：dsh UI + 碧游宫壳 + 观星台/开阵页」的表述。
> 自建壳渲染进程（`desktop/ui/shell`）从"业务承载者"降级为"dsh 起不来时的降级页"。

### 决策

观星台、功德殿、品牌标识、运行状态浮层，一律做成 **dsh 客户端插件的 browser half**，注册进上游已有的 slot；
Electron 只保留窗口、托盘、进程生命周期、本地控制面与密钥。理由四条：

1. **位置已经开好了**。rc.5 实测（见下表）上游为「品牌、全局面板、浮层、底栏」都留了公开 slot，不需要重写壳，也不需要 patch 上游。
2. **不复制 feature 状态**。会话、工作区、设置仍归上游插件所有，我们只贡献自己那块，避免状态双写。
3. ~~**品牌是官方留的替换位**。`sidebar.brand.mark` / `sidebar.brand.name` 就是给部署方换 logo 与名字用的，比自建壳更省。~~ → **2026-11-22 改决**：侧栏品牌位不占用，保留 dsh/DeepSeek 默认外观；碧游宫品牌由 Electron 壳顶栏承载。其余三条理由不变。
4. **升级只需跟 contract**。slot 与 service 是公开契约，dsh 升版时改动集中在 `inject` 列表与 d.ts 校验，不用整壳重做。

### rc.5 已核实的能力（`node_modules/@deepseek-ai/`，340 个包）

注册位置（`@deepseek-ai/dsh-client-ui-layout`、`dsh-client-ui-sidebar` 的 `contract/slots.d.ts`）：

| slot | kind | 用途 |
|---|---|---|
| `sidebar.brand.mark` / `sidebar.brand.name` | single | 官方替换位；**本项目不占用**，保留 dsh 默认 |
| `sidebar.panellist` | list | 全局面板图标；**list id 即对应 main panel 的 key** |
| `sidebar.footer.action` | list | 设置旁的脚部动作 |
| `main` | keyed | 中央面板；保留 key `conversation`，其余 key 不绑会话 |
| `shell.overlay` | list | 全帧浮层，**本身 click-through**，条目自行 opt-in 指针事件 |
| `shell.bottom` | single | 三栏下方通栏 |
| `shell.leading` | single | 顶栏左侧窗口区 |

共享服务（`ctx.layout`，`dsh-client-ui-layout/lib/types/client/service.d.ts`）：
`selectPanel(MainPanelId | null)` / `toggleSidebar()` / `openRightbar(track, fullscreen)` / `closeRightbar()` /
`beginNavigation(): AbortSignal` / `panelInfo: HostObservable<PanelInfo>`。

插件写法（真实样例 `dsh-client-ui-open-in-app`，`src/` 随包发布可直接抄）：

```ts
export const inject = ['slots', 'locale', 'layout', 'sessions']
export function apply(ctx: ClientContext): void {
  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay', id: 'biyou.status', locale: NS,
    inject: () => ({ hooks: status.hooks, refresh: status.refresh }),
  }, BiyouStatusPill))
}
```

包形态（`package.json`）：`exports["./client"]` 指向 browser half，
`dsh.client = { inject: [...], platform: 'web' }`；node half 的 `apply() {}` 只为进 Loader。
挂载到 profile：依赖进 profile `package.json`，再在 `cordis.patch.yml` 插一行
`- insert: [{ id: ui-biyou, name: '@biyou/dsh-plugin', config: {} }]`。

### 目标架构

```text
Electron 主进程      窗口 / 托盘 / 单实例 / dsh 子进程 / 本地控制面 / 密钥
      │  (Host service，不经 preload IPC 直达插件)
dsh Host ──── biyou 插件 node half（service：state / taskboard / formation）
      │
dsh Web UI ──── biyou 插件 browser half（slot occupant）
                 ├ sidebar.brand.mark/name   不占用（保留 dsh 默认品牌）
                 ├ sidebar.panellist + main  观星台 / 功德殿
                 └ shell.overlay             运行状态 pill（开阵中 / 模型未就绪）
      │
desktop/ui/shell     仅三态降级页：dsh 起不来 / 启动中 / 崩溃后重试
```

纪律：**插件只拥有 chrome 与几何**，观星台是 `main` 面板而不是"另一个壳"；会话与设置不复制到插件。

### 阶段计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| D1 | `desktop/packages/biyou-plugin` 骨架：TS + ESM 构建、node/browser 双 half、profile 挂载 | 冒烟断言插件进 Loader 且出现在 `window.__DSH_BOOT__` roster |
| D2 | 状态：`shell.overlay` 状态 pill 接通现有 dsh status。侧栏品牌不替换（2026-11-22 改决） | pill 能反映 ready/failed；`brandMarkCount=0` 且 `brandName=null`（dsh 默认品牌未被占用） |
| D3 | 观星台：`main` keyed 面板 + `sidebar.panellist` 入口，第一版只读 | 点击侧栏能进面板、能回 conversation；不崩不白屏 |
| D4 | 功德殿：任务板面板，读写经 Host service 到本地控制面 | 创建/推进任务落盘 userData，刷新后仍在 |
| D5 | 自建壳退场：`ui/shell` 只留降级页，README 与视觉规范改写 | 正常路径不再出现自建壳顶栏 |

### 风险与回退

- **slot 契约随版本变动**：锁 rc.5；升级单独立项，用 d.ts 编译期校验 + `npm run check` 兜底。
- **视觉割裂**：插件 UI 复用 `@deepseek-ai/dsh-client-ui-primitives` 与 theme token，不引入第二套色板。
- **构建链变复杂**：先用 tsup 出 ESM，`platform: 'web'`；打包塞 asar 的事留到 S8。
- **回退**：D1–D4 任一卡住，`ui/shell` 仍在，不影响 S1 已交付能力；D5 是唯一不可逆步骤。

### 待定（需你拍板）

1. 插件放 `desktop/packages/biyou-plugin/`，还是独立仓库（便于以后给第三方复用）。
2. ~~观星台第一版范围：只读展示，还是含「开阵」按钮（涉及本地控制面提前落地）。~~ → 已决：观星台后置。
3. ~~品牌替换程度：只换 mark + name，还是连主色也换（会与 dsh 视觉规范分叉）。~~ → 已决：侧栏品牌整体不替换，保留 dsh 默认；碧游宫品牌只在 Electron 壳顶栏。

---

## 单机首版精简为纯 dsh（2026-11-22 决策）

**结论**：V2.0-A 单机不实现任务外壳、本地任务板/因果簿、香火账本、观星台与功德殿面板。

理由：

- dsh 已是完整单 Agent 运行时，具备会话持久化、agent loop、工具与后台 job；
- 当前 6 条验收（可启动、本地闭环、默认不广播、联阵可用、重启不丢、无残留）不依赖任务外壳；
- 单机单用户场景下，任务状态机、租约、预算硬停与审批属于增量治理能力，不是闭环前提。

保留：

| 保留项 | 说明 |
|---|---|
| dsh 原生执行 | 会话、工具、模型调用全部归 dsh |
| Electron 壳 | 窗口、单实例、dsh 子进程、配置与退出清理 |
| 品牌与状态 | 品牌在 Electron 壳顶栏；dsh 侧栏 `sidebar.brand.*` 不占用；状态走 `shell.overlay` |
| 联阵 | 开阵/寻阵/入阵按 `lan-formation-contract.md` 用 Node 实现 |
| `ui/shell` | 仅三态降级页 |

后置（触发条件满足时再引入）：

| 后置项 | 重新引入的触发条件 |
|---|---|
| 任务外壳（ticket/派差/租约/重派） | 一个目标需跨多个 dsh session 持续推进，或需要任务级恢复 |
| 因果簿/香火账本 | 需要预算硬停、成本归因或跨重启对账 |
| 观星台 | 需要高风险动作定夺与审计 |
| 功德殿 | 需要任务交接、验收与功德入账 |
| `/agent/v1/*` 写操作 | 上述面板需要写入时 |

影响：

- S3/S4 不再是 V2.0-A 交付项；
- S2 由“自研 model-adapter”改为“配置 dsh 的 Ollama provider + 健康提示”；
- D3/D4 后置，D2 收尾后可直接进入 S5/S7/S8；
- `docs/agent-contract.md` 与 `zhentu` 任务板/账本保留，作为联阵或后续任务治理的契约与参考实现，不在单机首版接线。

---

## 归属与交互边界（2026-11-22 澄清，已确认）

本节是后续拆功能的统一判断依据，优先级高于上文「架构」一节里把本地控制面画进 Electron 的早期表述。

### 角色

| | dsh | 碧游宫客户端 |
|---|---|---|
| 身份 | 运行时 + Cordis 插件框架（Agent / 会话 / 模型 / Web UI） | 产品壳 / 宿主（Electron 窗口、托盘、进程生命周期、联阵、主题、品牌） |
| 代码 | 上游包，锁 `0.2.2-rc.5` | `desktop/electron/`、`desktop/ui/shell/`、`desktop/packages/` |
| 关系 | 被托管 | 启动 dsh、配置 profile、装载碧游宫插件；用 `WebContentsView` 承载 dsh Web UI，不重写上游 UI |

### 插件归属

- **机制属于 dsh**：`apply(ctx)` / `inject` / `ctx.slots` / `ctx.llm`、`window.__ModuleLoader__`、profile 的 `cordis.patch.yml`、`exports["./client"]` → `window.__DSH_BOOT__`。
- **代码与内容属于碧游宫**：`desktop/packages/@biyou/*`，服务碧游宫品牌与业务。
- 精确表述：既不是 Electron 插件，也不是 dsh 官方插件，而是**碧游宫基于 dsh 契约编写、挂进自己 profile 的 dsh 插件**。

### 交互方向：控制面双向、状态面单向、业务面不通

```text
客户端 ──①控制下行──▶ dsh        spawn / restart / 信号；profile / DSH_HOME / 主题预设
客户端 ◀──②状态上行── dsh        stdout "dsh web:" 解析 + exit + 客户端主动 fetch 探活
客户端 ──③UI 承载───▶ dsh UI     WebContentsView.loadURL，容器→内容，不是 API 调用
客户端 ✕──④业务调用── dsh        无通道：客户端不调 dsh Agent/模型 API，dsh 不调联阵/窗口
```

关键事实：

- ① 见 `electron/dsh/server.js` 的 `start/stop/restart/killNow` 与 `electron/main.js` 的 `provisionProfile`、`nativeTheme.themeSource`。
- ② `dsh:status` 返回的是客户端自己维护的 `DshServer.status`（由 stdout 正则与 `child.on('exit')` 驱动），**不是 dsh 进程的回调**；dsh 不知道 Electron 存在。
- ③ `electron/main.js` 的 `attachServer` 在 READY 时 `loadURL`。
- ④ `desktop/packages/` 内无 `electron` / `ipcRenderer` / 联阵引用；本地模型插件的 `llama-server` 自管在 `127.0.0.1:18434`，不经客户端。

**刻意的设计取舍**：dsh 可独立运行、崩溃隔离、不产生第二套需双向同步的状态；代价是客户端与插件业务不通，而这正好逼着业务能力走 dsh 契约。

### 功能归属判断标准

> 要进 dsh 的界面或能力上下文 → dsh 插件（browser half + 必要的 host half）；
> 只是开应用 / 管进程 / 管联阵 → 客户端本体。

### 已知 seam 与当前双轨

- **联阵 seam**：`FormationHost` 在 Electron 主进程，dsh 插件不感知阵况。若未来要让观星台显示联阵状态，须新开通道（Host half 暴露 service，或客户端推 snapshot）；当前无通道是设计使然，不是缺陷。
- **双轨现状**：品牌 mark/name、状态 pill、本地模型设置页已走 dsh slot（真插件）；自建壳 `ui/shell/index.html` 的观星台 / 功德殿 / 藏经阁导航仍走 Electron IPC（非插件）。D5 撤场时按上述标准逐项判定：搬进 slot，或明确划为客户端本体功能、不再以插件名义存在。
