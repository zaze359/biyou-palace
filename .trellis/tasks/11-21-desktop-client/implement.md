# 实施计划 · 碧游宫桌面客户端

## 阶段

### S1 · Electron 骨架与 dsh 接入

- 新增 `desktop/`：`package.json`、Electron 主进程、preload、渲染入口。
- 固定 dsh / Electron / Node 版本；开发模式从仓库源码启动。
- 单实例锁、窗口生命周期、退出清理骨架。
- 品牌壳：应用名、图标占位、首页入口。

### S2 · 本地模型（已拆为独立插件）

- 由子任务 `11-22-local-models-plugin` 交付：内置 llama.cpp 运行时、GGUF 下载/导入、`llama-server` 托管、`biyou-local` provider 与设置页。
- 桌面端只负责插件链接与 boot 载荷；不再实现 Ollama 探测/模型适配。

### S3 · 本地控制面与存储（后置）

- 单机首版不实现任务外壳、SQLite 任务板与预算账本；`zhentu` 侧实现保留，等触发条件满足再接。

### S4 · 单机闭环（后置）

- 单机首版直接用 dsh 原生 Agent 完成目标；不实现目标拆解、派差、回禀、重启恢复。

### S5 · 联阵服务

- 按 `docs/lan-formation-contract.md` 用 Node 实现 UDP 发现、HTTP join/heartbeat/leave/events、踢人/散阵。
- 开放阵/闭阵；免阵主确认；限流；阵主令牌仅回环。
- 复用 `scripts/lan_formation_acceptance.py` 的场景做集成验证。

### S6 · UI

- 开阵弹窗：阵名、开放/闭阵、阵符、容量。
- 成员列表、踢人、散阵。
- 观星台：任务、气机、香火用量、待定夺。
- 单机/联阵状态条：默认单机，开阵后显示阵眼。

### S7 · 健壮性与退出

- 子进程清理；异常退出恢复；日志脱敏。
- 空模型、模型未加载、Ollama 不可达、端口占用等错误路径。
- 无残留进程验收。

### S8 · 打包交接

- 产出 macOS 未签名 `.app`；Windows 构建脚本。
- 交给 `11-21-client-packaging` 做 CI 与安装包。

## 文件计划

```text
desktop/
├── package.json
├── electron/
│   ├── main.ts
│   ├── preload.ts
│   ├── local-control/
│   ├── model-adapter/
│   ├── lan-host/
│   └── storage/
├── ui/
│   ├── shell/
│   ├── observatory/
│   └── open-formation/
└── tests/
```

## 验证

- `npm run dev` 启动桌面端，单机完成一次模型对话（本地模型插件）。
- 单机任务闭环（提交 → 执行 → 产物 → 重启恢复）后置，不作为 V2.0-A 验收。
- 开阵后 Android/验收脚本可发现并入阵；踢人/散阵回单机。
- 退出后无残留 Node/Ollama 子进程。
- `python3 scripts/lan_formation_acceptance.py` 仍全绿（协议回归）。

## 风险

- dsh 版本升级导致壳与 UI 接口漂移：固定版本，升级单独立项。
- Electron 打包体积大：接受；本地运行时后置。
- 本地控制面与 dsh 会话边界：单机首版不引入本地控制面；后置时再按契约分层。
- 单机首版直接用 dsh 原生 Agent；本地模型由 `11-22-local-models-plugin` 提供，不再依赖 Ollama 作为主路径。

## 完成定义

- macOS 未签名客户端可启动并完成单机闭环。
- 默认不广播；开阵后按契约工作。
- 任务、产物、配置重启不丢。
- 退出无残留进程。
- 文档与联阵契约一致，未引入第二套编排中心。

## 进度

### S1 · 已完成（2026-11-21）

- `desktop/` 落地：`electron/main.js`（单实例、窗口、视图布局、IPC、退出清理）、
  `preload.cjs`（白名单桥）、`ui/shell/*`（品牌壳三态）、`electron/dsh/*`
  （选 Node / 同步 profile / 子进程生命周期）。
- 版本锁定：Node `24.22.3`（`.node-version` + `.nvmrc` + `.runtime/node`）、
  Electron `44.7.0`、dsh `0.2.2-rc.5`；`npm run check` 交叉校验四处清单。
- 接入 dsh 开发模式：仓库内 profile 模板 → `userData/dsh/profiles/biyou`
  （node_modules 软链）→ 子进程 `dsh --profile biyou --host 127.0.0.1 --port 0 --no-open`
  → 解析 `dsh web: <url>` → HTTP 探活 → `WebContentsView` 载入。
- 数据落 `userData`（macOS `~/Library/Application Support/biyou-desktop`），不写仓库。
- 验收实测：`smoke:dsh` 8/8 通过（303→200、无残留进程组）；`smoke` 出截图与
  `colorScheme.dark=true`、视图边界 `{y:56,1180×744}`；`smoke:quit` 关窗后 1.6 秒内
  dsh 已停、无残留 Electron/dsh 进程。
- 踩坑记录（已修）：
  - 外部 `NODE_OPTIONS` 的 `--require` 垫片会破坏 dsh 原子写 → 子进程显式剔除。
  - 外部 `ELECTRON_RUN_AS_NODE=1` 会让 Electron 退化为 Node → 启动器剔除。
  - dsh 默认「跟随系统」主题在深色壳下割裂 → 主进程 `nativeTheme.themeSource = 'dark'`。

### D1 · 已完成（2026-11-21）

- 插件以 `@biyou/dsh-plugin` 进入 `window.__DSH_BOOT__`，`shell.overlay` 渲染状态 pill。
- 插件经 `link-plugin.mjs` 链入 profile，`cordis.patch.yml` 注册 `ui-biyou`。

### D2 · 已完成（2026-11-22）

- `sidebar.brand.mark` 替换为青萍剑标记，`sidebar.brand.name` 替换为「碧游宫」；
  两者带 `data-biyou` 标记，供冒烟断言。
- 状态 pill 接 `ctx.connection.state`（`useSyncExternalStore`），显示「就绪 / 连接中 / 已断开」；
  `data-state` 同步连接状态。
- 冒烟新增断言：`inBoot`、pill 存在、`state === "connected"`、品牌标记存在、品牌名为「碧游宫」；
  `dshView.biyouOk` 为总判定。
- 实测：`npm run smoke` 返回 `ok=true`，`biyou={"inBoot":true,"pill":true,"text":"碧游宫 · 就绪",
  "state":"connected","brandMarkCount":1,"brandName":"碧游宫"}`。

### 下一步

S2：确认并配置 dsh 的 Ollama provider，补模型健康提示；不自研模型客户端。

## 路线调整 · 呈现层改走 dsh 客户端插件（2026-11-21）

结论已写入 `design.md`「呈现路线」一节，这里只记执行要点与已核实的依据：

- **已核实**：rc.5 的 `node_modules/@deepseek-ai/`（340 包）里，`dsh-client-ui-layout` 提供
  `main`(keyed) / `shell.overlay`(list) / `shell.leading` / `shell.bottom` 四个根 slot 与
  `ctx.layout` 服务（`selectPanel`/`toggleSidebar`/`openRightbar`）；`dsh-client-ui-sidebar`
  提供 `sidebar.panellist`(list)、`sidebar.brand.mark`/`brand.name`、`sidebar.footer.action`。
- **可抄的样例**：`dsh-client-ui-open-in-app`（`src/` 随包发布）——`inject` 数组 + `apply(ctx)` +
  `ctx.slots.inject(name, () => ctx.slots.register({name, id, locale, inject}, Component))`。
- **挂载方式**：插件包声明 `exports["./client"]` 与 `dsh.client = {inject, platform:'web'}`；
  profile 加依赖，再在 `cordis.patch.yml` 插 `- insert: [{id, name, config}]`。与 dsh-web-app
  登记 `ui-open-in-app` 的方式完全一致。
- **不变的部分**：Electron 主进程职责（窗口/托盘/单实例/进程/本地控制面/密钥）与 S1 已交付能力
  全部保留；`ui/shell` 降级为"dsh 起不来时的降级页"，到 D5 才动。
- **先做 D1**：`desktop/packages/biyou-plugin` 骨架 + profile 挂载 + 冒烟断言进 roster，
  再做品牌（D2）与观星台（D3）。

### D1 已完成（2026-11-21）

落地物：

| 文件 | 作用 |
|---|---|
| `desktop/packages/biyou-plugin/package.json` | `exports["./client"]` + `dsh.client{platform:"web", inject:[renderer, layout]}` |
| `.../lib/index.js` | node half，`apply()` 为空：只为成为正常 Cordis row |
| `.../lib/client.js` | browser half，手写 `window.__ModuleLoader__.load({id, factory})` |
| `desktop/scripts/link-plugin.mjs` | 幂等软链进 profile `node_modules`，支持 `--check` |
| `runtime/home/profiles/biyou/cordis.patch.yml` | 插一行 `- insert: [{id: ui-biyou, name: '@biyou/dsh-plugin'}]` |
| `electron/main.js` | 新增 `probeBiyouPlugin()`，冒烟报告带 `dshView.biyou` |

实测证据（`BIYOU_USER_DATA=/tmp/biyou-d1b npm run smoke`）：

```json
"dshView": { "loaded": true, "blank": false,
  "biyou": { "inBoot": true, "pill": true, "text": "碧游宫" } }
```

`inBoot` 来自 `JSON.stringify(window.__DSH_BOOT__)` 含 `@biyou/dsh-plugin`，
即 browser roster 真的收了这个包；`pill` 来自 `[data-biyou="status"]` 命中，
证明 occupant 渲染上屏。截图确认 pill 在右下角、不遮挡内容。

踩坑记录：

- **产物不是标准 ESM**。客户端 bundle 形如
  `window.__ModuleLoader__.load({id, factory: require => {...}})`，factory 内
  `require("react")` 由宿主提供，末尾 `exports.apply/inject`。官方没有可用的公开打包器，
  所以 `lib/` 下的文件是手写的、可直接运行，**不需要构建步骤**。
- **combo URL 形如 `plugins/??pkg/client.js&rev=<combo rev>`**，逐个文件猜路径会命中 SPA
  兜底（返回 index.html 而非 404），别把它当成功。
- **`shell.overlay` 的条目默认落在画面左上角**，会压住侧栏品牌行；必须自己
  `position: absolute` 定位，并保持 `pointer-events: none`（该层本身 click-through）。
- **`smoke:dsh` 在 `~/Library/Application Support` 下必失败**：沙箱不允许 unlink，
  `atomic-write` 拿不到 `.credentials.yaml.lock` → `EPERM`，进程 exit 1。用
  `BIYOU_USER_DATA=/tmp/...` 跑即 8/8 全绿。与本插件无关（去掉插件行同样失败）。
- **别拿仓库 profile 直接跑 dsh**：HMR 会把 `cordis.yml` 改写成英文注释，还会就地写
  `.credentials.yaml`（含 browser-session 密钥）与 `storages/`。已在 `.gitignore` 挡住，
  但仍应优先用 `/tmp` userData。

## 路线调整 · 单机首版精简为纯 dsh（2026-11-22）

**决策**：单机直接使用 dsh 原生 Agent，不再自建任务外壳、任务板/因果簿、香火账本与观星台/功德殿面板。

范围变化：

| 阶段 | 原计划 | 现在 |
|---|---|---|
| S2 | 自研 `model-adapter` 调 Ollama | 改为配置 dsh 的 Ollama provider + 健康提示；不写模型客户端 |
| S3 | 本地控制面 + SQLite 任务板 | 移出 V2.0-A；保留 `zhentu` 实现作为后续/联阵参考 |
| S4 | 单机任务闭环 | 由 dsh 原生会话承担；不再实现派差/回禀/租约 |
| D3/D4 | 观星台、功德殿面板 | 后置；D2 收尾后直接进 S5/S7/S8 |
| S5/S7/S8 | 联阵、健壮性、打包 | 保持 |

保留：Electron 壳、dsh 子进程、品牌/状态插件、开阵入口、`ui/shell` 降级页。

重新引入任务外壳的触发条件见 `design.md`「单机首版精简为纯 dsh」。

### 下一步（按精简后范围）

1. D2 收尾：冒烟补品牌替换断言，状态 pill 接 dsh 运行状态。
2. S2：确认 dsh 的 Ollama provider 配置方式与模型目录展示。
3. S5：按 `lan-formation-contract.md` 用 Node 实现开阵/寻阵/入阵。
4. S7/S8：进程清理、错误路径、macOS 未签名 `.app` 与 Windows 构建脚本。
