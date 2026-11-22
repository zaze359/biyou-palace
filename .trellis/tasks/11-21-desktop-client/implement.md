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

- 开阵表单：阵名、开放/闭阵、阵符、容量。
- 成员列表、踢人、散阵。
- 单机/联阵状态条：默认单机，开阵后显示阵眼。
- ~~观星台：任务、气机、香火用量、待定夺。~~ → 已后置（2026-11-22 单机首版精简），S6 实际交付见下方进度节，不含此项。

### S7 · 健壮性与退出

- 子进程清理；异常退出恢复；日志脱敏。
- 空模型、模型未加载、本地模型运行时/服务不可达、端口占用等错误路径（外部 Ollama 为可选 provider，不作为前提）。
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

### D2 · 已完成（2026-11-22，当日改决侧栏品牌）

- 状态 pill 接 `ctx.connection.state`（`useSyncExternalStore`），显示「就绪 / 连接中 / 已断开」；
  `data-state` 同步连接状态。**这是 D2 现在唯一保留的 browser half UI**。
- 侧栏品牌：初版曾占用 `sidebar.brand.mark` / `brand.name`（青萍剑 + 碧游宫）；
  同日按截图评审改决——**碧游宫插件不再占用这两处**。组件与注册已从 `client.js` 移除。
- 壳顶栏品牌位：同样按截图评审，**顶栏左上角也不显示碧游宫 logo / 名称 / 版本号，留空占位**
  （`index.html` 的空 `.brand` + `shell.css` 的 `flex: 0 0 136px`，`shell.js` 去掉版本号写入）。
  碧游宫身份只保留在 Dock 图标与状态 pill 文案，不再占据任何品牌位。
- 侧栏外观随后明确为 **deepseek 官方发行版**：`dsh-web-app` 自带 `ui-brand-official`，
  profile 补丁层显式写 `disabled: false`，防止 panel 残留把官方品牌关掉后 fallback 成
  「DSH 本地构建」。官方 mark / wordmark 仍由上游包提供，不改上游 bundle。
- 冒烟断言随之调整：`inBoot`、pill 存在、`state === "connected"`、
  `brandMarkCount === 0`、`brandName === null`（碧游宫未占用），并正向检查
  `brandOfficialInBoot`、`officialMark`、`officialWordmark`、`localBuildFallback === false`
  （官方字标确实上屏）。`dshView.biyouOk` 为总判定。
- Dock / 菜单栏应用名：macOS 取 app bundle 的 `Info.plist` 名，非运行时 `app.setName()`。
  开发态直跑 `node_modules/electron/dist/Electron.app`（bundle 名 Electron），新增幂等脚本
  `scripts/patch-electron-name.mjs` 由 `dev.mjs` 启动时调用，把 `CFBundleName` /
  `CFBundleDisplayName` 改成「碧游宫」并留 `.biyou-bak`；打包产物由 electron-builder 按
  `productName` 生成，不受影响。验证：`lsappinfo info -only name 碧游宫` 返回
  `LSDisplayName=碧游宫`，截图菜单栏左侧显示「碧游宫」。

### S5 · 已完成（2026-11-22）

落地位置：`desktop/electron/lan-host/`，由 Electron 主进程持有，默认 standalone 不监听。

| 文件 | 作用 |
|---|---|
| `index.js` | `FormationHost`：开阵/散阵、4022 起选端口、状态快照、退出清理 |
| `room.js` | 房间状态机：成员/令牌/租约/封禁/限速/事件 |
| `http.js` | `/lan/v1` HTTP/JSON + SSE；阵主接口仅回环 + host token |
| `discovery.js` | UDP `4021` 公告 + `discover` 单播响应 |
| `util.js` | Argon2id 阵符、令牌、私有地址判断、广播地址 |

- 支持开放阵/闭阵；闭阵阵符 4–64 字符，只存 Argon2id 哈希（替代 Python 参考的 PBKDF2）。
- 校验顺序、错误码、心跳 10s/租约 30s、踢人封禁、散阵广播均按契约。
- 主进程新增 `lan:status/open/disband/kick` IPC 与 `biyou.lan` preload 白名单。
- `npm run smoke` 新增 `report.lan`：默认 standalone → 开阵 → HTTP 入阵 → UDP 发现 → 散阵 → 端口关闭。

实测：

- `npm run test:lan` 9/9 通过（开放/闭阵、限速、踢人封禁、SSE、UDP、租约、端口顺延）。
- `python3 scripts/lan_formation_acceptance.py` 3/3 通过（协议回归）。
- `BIYOU_USER_DATA=/tmp/biyou-s5-smoke npm run smoke`：`ok=true`，
  `lan={defaultState:"standalone", opened:{state:"hosting",health:true,roomId:true,joined:true,discovered:true}, afterState:"standalone", httpClosed:true}`。

已知边界：mDNS 仍为 P1；租约过期只标 offline 并保留容量，任务回收待编排。

### S6 · 已完成（2026-11-22）

- 壳顶栏新增「联阵」入口与 `单机 / 阵眼 · N 人` 状态条；dsh 连接状态改为只显示「就绪/启动中/已断开」。
- 联阵页占用主区：开阵表单（阵名/开放阵/闭阵/阵符/容量）、阵眼信息卡、成员列表（平台/门派/在线状态）、
  行内移出、刷新、散阵（二次确认）。
- 主进程新增 `shell:dsh-visible`，切到联阵页时隐藏 dsh 视图，回「对话」恢复；开阵后后台继续运行。
- `npm run smoke` 新增 `report.shell`：校验入口/表单/状态条、dsh 视图让位与开阵/散阵后的页面状态，
  输出 `smoke/lan.png` 与 `smoke/lan-hosting.png`。

实测：

- `BIYOU_USER_DATA=/tmp/biyou-s6-smoke3 npm run smoke`：`ok=true`，
  `shell={nav:true,panel:true,form:true,chipText:"单机",ipcState:"standalone",dshHidden:true,lanVisible:true,uiOpened:"hosting",hostingVisible:true,chipHosting:"阵眼 · 0 人",uiStopped:true,dshShown:true,ok:true}`。
- 截图确认：表单与阵眼信息卡顶部对齐、字段完整、按钮无截断；已修正 `.hint` 在左对齐面板里被 `margin:auto` 推到中间的问题。

### 下一步

S7：健壮性与退出（异常恢复、错误路径、日志脱敏、无残留进程）。

### S7 · 健壮性与退出 · 已完成（2026-11-22）

- **日志脱敏**：新增 `electron/dsh/redact.js`；DshServer 的 `#pushTail` / `#writeLog` 与
  主进程 `log()` 统一打码 URL `token` / `api_key` / `password` / `Bearer` 值。冒烟实测
  `dsh.log` 只留 `?token=***`，无明文凭据；`report.json` 与 `SMOKE` stdout 里的
  `status.url` 也打码（加载视图仍用原始值，不受影响）。
- **崩溃自动恢复**：DshServer 加 `autoRecover`（默认 false，主进程显式开）、
  `maxRecoverAttempts=2`、`recoverDelayMs=1200`、`probeTimeoutMs=20000`（原先探活写死 20s，
  现在可配置）。只有 **READY 后**意外退出才重拉；STARTING 阶段失败不重试，避免坏 profile
  死循环。恢复期间状态为 `FAILED + recovering`，壳显示「正在恢复」并隐藏旧视图，
  重拉 READY 后重新 loadURL。
- **单元测试**：`electron/dsh/test/s7.test.mjs`（`npm run test:s7`）4/4 通过——脱敏、
  READY 后崩溃受限重拉、启动期失败不恢复、主动 stop 不恢复。用假 dsh CLI（真 HTTP 服务）
  模拟，无需真实运行时。
- **回归**：`npm run smoke:dsh` 8/8、`npm run smoke` ok=true（biyouOk / localModelsOk /
  shell / lan 全绿），无残留进程。

### S8 · 打包交接（2026-11-22）

- **打包工具**：引入 `electron-builder@26`，配置见 `desktop/electron-builder.yml`。
  app.asar 只装主进程 / preload / ui/shell / assets / `@noble/hashes`；
  `runtime/`（1.4 GB dsh 依赖）与 `.runtime/`（锁定 Node 24.22.3）走 `extraResources`
  放 asar 外——asar 内跑不了子进程 Node，也加载不了原生模块。
- **路径适配**：`main.js` 用 `app.isPackaged` 切到 `process.resourcesPath/runtime` 与
  `node-runtime`；`resolveNode` 加可选 `pinnedNodeRoot` 参数（开发走 `desktop/.runtime`）。
  userData 里的 `node_modules` 软链在打包后指向 Resources，不再指仓库。
- **构建入口**：`scripts/build.mjs`（`npm run build -- --mac|--win|--linux`）= 版本自检 →
  校验 runtime/Node 就位 → 注入 commit/构建时间 → electron-builder → 写
  `dist/build-manifest.json`（版本 + commit + 时间 + 每个产物 SHA-256）。
  打包前剔除 `NODE_OPTIONS` / `ELECTRON_RUN_AS_NODE`，避免污染原子写。
- **范围**：mac `.dmg`/`.zip`、win NSIS `.exe`、linux AppImage，全部未签名（`identity: null`）。
  CI 矩阵、校验和一致性、Android APK 归 `11-21-client-packaging`。

实测（2026-11-22，macOS x64）：

- `npm run build -- --mac` 成功产出 `dist/mac/碧游宫.app`（2.0 GB）与
  `碧游宫-0.1.0-mac.zip`（463 MB），`dist/build-manifest.json` 记录版本 / commit /
  构建时间 / 两个产物 SHA-256。
- 资源布局核对通过：`Resources/runtime/.../dsh/lib/bin.js`、`Resources/node-runtime/node/bin/node`、
  `Resources/icon.png` 均就位；asar 内含 `/electron/main.js`、`/ui/shell/index.html`。
- **dmg 目标已移除**：首次构建时 dmg 阶段的 `ditto` 在受限沙箱下报
  `Operation not permitted`（挂载卷拷贝被拒），`.app` + `.zip` 已满足未签名分发；
  dmg 由 client-packaging 在有权限 CI 上按需开启。
- **未验证项**：打包后 GUI 启动冒烟——本沙箱下 Electron 报 `codesign_util` 与
  `Node.js environment variables are disabled`，app 进程无法稳定进入主流程，
  需在真实桌面环境人工验收（双击 .app → 观察 dsh 就绪 / 插件上屏）。
- 构建产物 `dist/` 已在 desktop/.gitignore 排除，不会误入提交。

**打包后验收补记（2026-11-23）**：重打包并实际启动 `.app` 后，发现并修复一个此前
开发态冒烟覆盖不到的缺口：

- **bug**：`@biyou/dsh-plugin` / `@biyou/dsh-local-models` 在打包 profile 里是相对
  symlink（`../../../../../../packages/biyou-*`），而 `electron-builder.yml` 的 `files`
  不含 `packages/`。放 `files` 里会被塞进 `app.asar`，但 dsh 是独立 Node 进程、不走
  asar，symlink 解析到 `Resources/packages/*` 时空无一物 → dsh 启动报
  `ERR_MODULE_NOT_FOUND: Cannot find package '@biyou/dsh-plugin'`，2 个插件未激活。
- **修复**：把两个插件目录从 `files` 移到 `extraResources`，落到 `Resources/packages/*`，
  与 symlink 目标对齐。重打包验证：symlink 解析成功、dsh 日志无 `ERR_MODULE_NOT_FOUND`
  或 `did not activate`、dsh 正常 READY。
- **Dock 应用名验证**：打包 app 的 bundle 正确（`CFBundleName`/`DisplayName`=碧游宫、
  `CFBundleIdentifier`=`cn.biyou.desktop`）。用 `open -n` 以 bundle 方式启动时
  `lsappinfo` 显示 `bundleID="cn.biyou.desktop"`、`LSDisplayName=碧游宫`——Dock 悬停
  标签正确（开发态直跑裸二进制拿不到 bundle 身份，是固有差异，见 patch 脚本注释）。
- **仍未验证**：打包 GUI 完整渲染——本沙箱 `sandbox initialization failed: Operation
  not permitted` 导致 GPU 进程反复崩溃退出（SIGTRAP），与 S8 记录的限制一致；真机验收
  仍需 client-packaging 完成。
- **清单缺陷（待改）**：`build.mjs` 对 `.app` 目录产物用「路径字符串」算 SHA-256，
  内容变了哈希不变（多次打包 `.app` 哈希均为 `28870c07…`）。需改为真实内容哈希或
  标注为目录清单哈希，交 client-packaging。
- **Dock 图标圆角/尺寸修正（2026-11-23）**：真机反馈 Dock 图标是直角方图、比系统图标
  大一圈。根因是 `assets/icon.png` 为 1024×1024 满幅 RGB 无 alpha，`app.dock.setIcon()`
  对不透明方图不加圆角。修法：改 `render-icon.main.cjs`，渲染时把源图放进透明画布、
  加圆角遮罩（radius 22%）并四周内缩（inset 10%），输出 RGBA；事实源
  `assets/brand/icon-photo.png` 不动。重生成 `icon.png` 并重打包，从 `icon.icns` 导出
  预览确认圆角+透明+留白正确，冒烟全绿。

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
