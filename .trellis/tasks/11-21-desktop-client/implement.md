# 实施计划 · 碧游宫桌面客户端

## 阶段

### S1 · Electron 骨架与 dsh 接入

- 新增 `desktop/`：`package.json`、Electron 主进程、preload、渲染入口。
- 固定 dsh / Electron / Node 版本；开发模式从仓库源码启动。
- 单实例锁、窗口生命周期、退出清理骨架。
- 品牌壳：应用名、图标占位、首页入口。

### S2 · 本地模型适配

- 探测 Ollama；显示未安装/未运行/就绪。
- 模型列表与 chat 流式调用；错误映射。
- 设置页持久化模型与地址；密钥只留主进程。

### S3 · 本地控制面与存储

- `userData` 目录布局：`config.json`、`tasks.db`、`artifacts/`、`logs/`。
- SQLite 任务板最小表：tasks、assignments、reports、usage。
- 任务状态机与租约；紧凑任务视图。

### S4 · 单机闭环

- 目标提交 → 本地阵眼拆解 → 本地仙徒执行 → 回禀 → 产物。
- 先串行，后有限并发；预算上限与暂停。
- 重启恢复未完成任务。

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

- `npm run dev` 启动桌面端，单机完成一次模型对话。
- 单机任务闭环：提交 → 执行 → 产物 → 重启恢复。
- 开阵后 Android/验收脚本可发现并入阵；踢人/散阵回单机。
- 退出后无残留 Node/Ollama 子进程。
- `python3 scripts/lan_formation_acceptance.py` 仍全绿（协议回归）。

## 风险

- dsh 版本升级导致壳与 UI 接口漂移：固定版本，升级单独立项。
- Electron 打包体积大：接受；本地运行时后置。
- 本地控制面与 dsh 会话边界不清：先独立 SQLite，避免耦合。
- Ollama 依赖不满足“严格自闭环”：V2.0-A 接受，V2.0-B 再内嵌 llama.cpp。

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

### 下一步

S2 本地模型适配（Ollama 探活/流式/密钥留主进程），在 `cordis.patch.yml` 增模型入口。

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
