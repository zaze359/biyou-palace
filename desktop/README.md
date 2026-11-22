# 碧游宫桌面客户端 · S1 骨架

macOS / Windows 桌面客户端的第一层：**Electron 品牌壳 + dsh 运行时接入**。
默认单机自闭环，不广播、不监听联阵；用户主动「开阵」后才进入联阵模式（S5）。

> 任务：`.trellis/tasks/11-21-desktop-client`（设计见同目录 `design.md` / `implement.md`）
> 命名一律按 `docs/naming.md`：阵眼、仙徒、观星台、功德殿、藏经阁、香火。

## 一、版本锁定

| 组件 | 版本 | 落地方式 |
|---|---|---|
| Node | **24.22.3** | `desktop/.node-version` + `.nvmrc`；`npm run node:bootstrap` 下载到 `desktop/.runtime/node` |
| Electron | **44.7.0** | `devDependencies.electron` 精确版本 |
| dsh | **0.2.2-rc.5** | `runtime/home/profiles/biyou/package.json` 精确版本 |

三处必须一致，`npm run check` 会交叉校验 `package.json` / `.node-version` /
`electron/versions.js` / profile 清单，任何一处漂移都会直接报错。

为什么还要单独下载 Node：dsh 的 `engines` 要求 `>=24.9.0`，而 Electron 44.7.0 内置的
Node 是 24.21.0——能用，但不是锁定版本。所以约定：

1. 有 `.runtime/node` → 用它（**唯一真正锁定版本的路径**）；
2. 没有 → 依次回退 Electron 内置 Node、PATH 上的 node，要求 `>=24.9.0`；
3. 回退时状态里会标注 `exact=no`，`dsh.log` 第一行也会记下来。

## 二、目录

```text
desktop/
├── start.sh / start.command  一键启动（Finder 可双击）
├── electron/
│   ├── main.js            主进程：单实例、窗口、视图布局、IPC、退出清理
│   ├── preload.cjs        渲染层唯一权力边界（sandbox + contextIsolation）
│   ├── paths.js           userData 目录布局
│   ├── versions.js        版本锁定单一事实来源
│   └── dsh/
│       ├── resolve-node.js  选 Node（锁定 → Electron → PATH）
│       ├── provision.js     仓库 profile → userData（node_modules 用软链）
│       └── server.js        dsh 子进程生命周期 + URL 解析 + 探活 + 进程组清理
├── ui/shell/              品牌壳：顶栏、状态、启动/失败/就绪三态
├── packages/biyou-plugin/ 碧游宫 dsh 客户端插件（品牌 / 状态浮层）
├── packages/biyou-local-models/ 本地模型插件（运行时 / GGUF / llama-server / provider）
├── runtime/home/profiles/biyou/   dsh profile 模板（bundles + 补丁层）
├── assets/
│   ├── icon.png             应用图标（写实青萍剑 logo；npm run icon 重生成）
│   └── brand/
│       ├── icon-photo.png   图标事实源：写实水墨青萍剑（剑主体、青莲背景）
│       ├── icon.svg         几何副版图标源（npm run icon -- --geo）
│       └── mark.svg         单色几何标记（dsh 侧栏 24px 位用）
└── scripts/               版本自检、Node 下载、无头冒烟、图标生成、插件链接
```

### 品牌 Logo 接入

正式 Logo 是**写实水墨青萍剑**（`docs/brand/logo-master.png`，剑为绝对主体、青莲为背景注脚），
图标事实源为 `assets/brand/icon-photo.png`；设计推导与规范见 `docs/brand/`。

| 落点 | 用什么 | 为什么 |
|---|---|---|
| 应用图标（Dock / 窗口 / 关于面板） | `assets/icon.png`，源自写实 logo | 大尺寸，写实笔触可辨 |
| 壳顶栏品牌位 | `assets/icon.png`（28px 方图） | 放大显示，`object-fit: cover` 收敛留白 |
| 关于面板 | `nativeImage` 载 `assets/icon.png` | 同上 |
| dsh 侧栏品牌位（`sidebar.brand.mark`） | `brand/mark.svg` 几何单色版 | 24px + `currentColor` 场景，写实图会糊成一团 |

重生成图标：

```bash
npm run icon              # 写实 logo（默认）
npm run icon -- --geo     # 几何副版（回溯旧样式）
```

注意：`render-icon.main.cjs` 支持 PNG（写实）与 SVG（几何）两种源；改事实源后跑 `npm run icon`，
不要手工覆盖 `assets/icon.png`。

## 三、快速开始

### 一键启动（推荐）

```bash
bash desktop/start.sh                 # 自检 → 起壳（缺依赖会提示安装）
bash desktop/start.sh --setup         # 首次：装 Electron + dsh 依赖 + 锁定 Node，再起壳
```

macOS 也可以直接在 Finder 里双击 `desktop/start.command`。

从仓库根统一入口进来等价：

```bash
bash start.sh desktop                 # 等同 bash desktop/start.sh
bash start.sh desktop --smoke-dsh     # 参数原样透传
```

常用开关：

| 开关 | 作用 |
|---|---|
| `--setup` | 缺什么装什么（Electron / dsh 依赖 / 锁定 Node） |
| `--no-sandbox` | 受限环境（容器、CI、无签名沙箱）追加 `--no-sandbox` |
| `--smoke` / `--smoke-quit` | 壳冒烟：截图报告 / 验证关窗清理 |
| `--smoke-dsh` | 只验 dsh 接入，不需要图形界面 |
| `--dsh` | 只起 dsh（终端），不开壳 |

### 手动分步

```bash
cd desktop
npm install                 # Electron 44.7.0
npm run dsh:install         # dsh 运行时依赖（@deepseek-ai/dsh 0.2.2-rc.5）
npm run node:bootstrap      # 可选但推荐：下载锁定 Node 24.22.3
npm run dev                 # 起壳，自动拉起 dsh web profile
```

没有图形环境时验证接入链路：

```bash
npm run smoke:dsh           # 起 → 解析 URL → HTTP 探活 → 停 → 查残留
npm run smoke               # Electron 壳冒烟，截图写到 <userData>/smoke/
```

## 四、和 dsh 的集成方式

**子进程 + 打印的 URL，不 import。**

```text
Electron 主进程
  └─ spawn <node> <profile>/node_modules/@deepseek-ai/dsh/lib/bin.js
        --profile biyou --host 127.0.0.1 --port 0 --no-open
     ↓ stdout: "dsh web: http://127.0.0.1:52341/?token=..."
  ├─ HTTP 探活（带 token 的那条 URL）
  └─ WebContentsView.loadURL(该 URL)   ← dsh UI 在独立渲染进程里
```

理由：

- dsh 要求 Node >=24.9，Electron 内置 Node 版本不受我们控制，隔离进程最稳；
- dsh 会自己起 shell / 工具子进程，`detached: true` 单独进程组，退出时按组发
  `SIGTERM`（5 秒后 `SIGKILL`），避免残留；
- dsh 崩了只影响它自己，壳还能显示失败原因 + 重试。

品牌壳（本进程）与 dsh UI（WebContentsView）是两块独立渲染：壳只负责顶栏与状态，
不 iframe、不往 dsh 页面注入脚本，避免被上游 DOM 变化带崩。

**dsh 只做单 Agent 运行时/UI**：profile 只装 `dsh-base` + `dsh-web-app`，不启用
`dsh-agent-team` 这类集群级编排——跨 Agent 调度属于阵图/联阵，不属于桌面端。

### 碧游宫插件（`packages/biyou-plugin`）

品牌、观星台、功德殿、状态浮层不做成"第二个壳"，而是做成 **dsh 客户端插件**，
注册进上游已有的 slot。这样会话、工作区、设置仍归上游所有，我们只贡献自己那几格。

> 2026-11-22 范围调整：单机首版直接使用 dsh 原生 Agent，**观星台/功德殿面板后置**；
> 当前插件只承载品牌与状态。任务外壳/账本同理后置，见任务 `11-21-desktop-client`。

```text
packages/biyou-plugin/
├── package.json   exports["./client"] + dsh.client{platform:"web", inject:[...]}
└── lib/
    ├── index.js   node half：apply() 为空，只为了让插件成为正常 Cordis row
    └── client.js  browser half：window.__ModuleLoader__.load({id, factory}) 包着的
                   CJS factory，末尾 exports.apply / exports.inject
```

用的 slot（rc.5 实测存在）：

| slot | 当前用途 |
|---|---|
| `shell.overlay` | 右下角「碧游宫 · 就绪 / 连接中 / 已断开」状态 pill（跟随 dsh 连接状态） |
| `sidebar.brand.mark` / `brand.name` | 已接（D2）：青萍剑标记 + 「碧游宫」名称 |
| `main`（keyed）+ `sidebar.panellist` | 预留：观星台 / 功德殿（后置，单机首版不实现） |

产物格式是 dsh 自有的同步加载格式，**不是标准 ESM**：`window.__ModuleLoader__.load`
拿到的 factory 里 `require("react")` 由宿主提供，所以 lib 下的文件是手写的、可直接运行，
不需要打包器。改完 `lib/` 里的文件重启即可，`rev` 由内容哈希自动变。

挂载方式与上游登记 `ui-open-in-app` 完全一致：

1. `scripts/link-plugin.mjs` 把包链进 profile 的 `node_modules`（幂等、离线，只建符号链接，
   不碰 1.2 GB 依赖树）；`start.sh` 每次启动前会跑一次。
2. `runtime/home/profiles/biyou/cordis.patch.yml` 里插一行
   `- insert: [{id: ui-biyou, name: '@biyou/dsh-plugin'}]`。

```bash
npm run plugin:link      # 手动链接（start.sh 已自动做）
npm run plugin:check     # 只校验链接是否指向正确
```

验收口径：`npm run smoke` 的 `report.json` 里 `dshView.biyouOk` 应为 `true`，且
`dshView.biyou` 至少包含 `{ inBoot: true, pill: true, state: "connected",
brandMarkCount: 1, brandName: "碧游宫" }`。`inBoot` 证明进了 `window.__DSH_BOOT__`
（browser roster），`pill`/`brandMarkCount` 证明 occupant 真的渲染上屏，
`brandName` 证明侧栏产品名已被替换。`dshView.localModelsOk=true` 证明本地模型插件的
browser half 也进了 roster（设置页具体渲染仍需人工看一次）。

### 本地模型插件（`packages/biyou-local-models`）

设置页入口：**设置 → 插件 → 本地模型**（slot `settings.plugins.tab`，id
`biyou.local-models`）。Host half 与 Browser half 装在同一个 Cordis row 里：

```text
Host half（lib/index.js）
  ├─ RuntimeManager   固定清单 → 下载 → SHA-256 → 解压 → 激活
  ├─ ModelManager     精选目录 / GGUF 导入 / 分片完整性 / registry
  ├─ Supervisor       一个 llama-server router（preset + 按需加载 + 空闲卸载 + 退避重启）
  ├─ LocalProvider    ctx.llm.registerAdapter(['biyou-local'], adapter)
  └─ Bridge           /api/biyou-local/{status,action}（同源 + 白名单 + 256KB 上限）

Browser half（lib/client.js）
  └─ Runtime / Models / Status 三区，2s 轮询，不直接碰 Node/文件系统
```

- 运行时固定在 `lib/runtime/manifest.json`：llama.cpp `b12620`，macOS x64/arm64
  （Metal）与 Windows x64（CPU/Vulkan/CUDA）；不抓 latest，不支持组合直接拒绝。
- 模型固定在 `lib/runtime/catalog.json`：Qwen3 8B / 4B / 1.7B 的 Q4_K_M，记录
  revision、sha256、上下文、内存估算与许可证；下载默认走 Hugging Face，可在
  `cordis.patch.yml` 的插件 config 里配 `mirror`。
- 服务用 router 模式：`--models-preset` 描述已安装模型，模型第一次请求时加载，
  空闲 15 分钟 `/models/unload`；崩溃按 1/5/15/60 秒退避；退出/热重载清理进程组。
- API key 随机生成在 `<DSH_HOME>/local-models/state/.api_key`（0600），只通过
  `--api-key-file` 传给 router，不写日志、不回前端。
- 数据目录：`<userData>/dsh/local-models/`（runtimes / models / cache / state / logs）。
- 导入本地 `.gguf` 只建符号链接（Windows 无权限时退硬链接），不复制原文件；
  文件头读取失败仍可入库，但标注 unverified。

```bash
cd desktop
node packages/biyou-local-models/test/unit/run.mjs
node packages/biyou-local-models/test/integration/run.mjs
# 或：npm run test:local-models
```

单元测试覆盖清单、平台选择、下载/续传/校验、GGUF 头、分片完整性、SSE 解析与去敏；
集成测试用假 runtime + 假 llama-server 跑通导入 → 启动 → 对话/工具调用 → 空闲卸载
→ 崩溃重启 → 退出清理。真实 llama-server 已在 macOS x64 + Qwen2.5 0.5B 上手工验证；
Windows 仍需真机跑一次。

## 五、数据与配置

一切可写数据都在 userData（macOS：`~/Library/Application Support/biyou-desktop`），
**不写仓库目录**：

```text
<userData>/
├── config.json              窗口位置尺寸（S1 仅此一项）
├── dsh/                     DSH_HOME
│   └── profiles/biyou/      profile 运行时副本；node_modules 软链回仓库模板
├── logs/desktop.log         壳日志
├── logs/dsh.log             dsh stdout/stderr（4 MB 轮转）
└── smoke/                   npm run smoke 的截图与报告
```

仓库里的 `runtime/home/profiles/biyou` 只是模板：启动时把 `package.json` /
`cordis.yml` / `cordis.patch.yml` / `pnpm-workspace.yaml` 同步到 userData，
`node_modules` 用符号链接指回模板，避免每次启动复制上百 MB。

环境变量（调试用）：

| 变量 | 作用 |
|---|---|
| `BIYOU_NODE` | 指定跑 dsh 的 node 可执行文件 |
| `BIYOU_USER_DATA` | 覆盖 userData 目录（干净环境验证、CI 隔离） |
| `BIYOU_NO_SANDBOX` | `=1` 时给 Electron 追加 `--no-sandbox`（受限环境专用） |
| `DSH_HOME` | 由主进程设置，不要在外部改 |

## 六、脚本

| 命令 | 说明 |
|---|---|
| `bash start.sh` | 一键启动（同 `npm run dev`，带环境自检与补装） |
| `npm run dev` | 自检 → 起壳（开发模式，非打包） |
| `npm start` | 起壳，同样先自检 |
| `npm run check` | 版本交叉校验 + 依赖检查 |
| `npm run dsh:install` | 安装 / 更新 profile 依赖 |
| `npm run node:bootstrap` | 下载锁定 Node 到 `.runtime/node` |
| `npm run dsh:web` | 只起 dsh（终端里），用于判断「壳的问题还是 dsh 的问题」 |
| `npm run smoke:dsh` | dsh 接入无头冒烟（CI 可跑） |
| `npm run smoke` | Electron 壳冒烟：截图 + 报告写到 `<userData>/smoke/` |
| `npm run smoke:quit` | 验证「关窗 → 清理 dsh 子进程 → 无残留」 |
| `npm run icon` | 重新生成图标占位 |

启动器 `scripts/dev.mjs` 会剔除 `ELECTRON_RUN_AS_NODE` 与 `NODE_OPTIONS` 再拉起 Electron：

- 前者会让 Electron 退化成纯 Node，报 `module 'electron' does not provide an export named 'BrowserWindow'`；
- 后者里的 `--require` 垫片（沙箱、APM）会污染主进程，也会通过环境继承搞坏 dsh 的原子写。

受限环境（容器 / CI / 无签名沙箱，Chromium sandbox 起不来）设 `BIYOU_NO_SANDBOX=1` 即可追加 `--no-sandbox`。

## 七、S1 验收对照

- [x] `desktop/` 骨架：package.json、主进程、preload、渲染入口
- [x] dsh / Electron / Node 三处版本锁定 + `npm run check` 交叉校验
- [x] 开发模式接入 dsh：本地 profile 子进程 + URL 探活 + WebContentsView 承载 UI
- [x] 单实例锁、窗口生命周期、退出清理（进程组）
- [x] 品牌壳：应用名、图标占位、首页入口、运行时状态
- [x] 冒烟可复现：`smoke:dsh` 全绿、`smoke` 出截图/报告、`smoke:quit` 无残留进程
- [ ] 打包为 `.app` / Windows 安装包 → S8
- [ ] 观星台 / 功德殿 / 开阵页 → S3 / S5 / S6

## 八、已知限制

- 未做签名/公证、自动更新（V2.0 非目标）。
- `cordis.patch.yml` 只放碧游宫自己的两行（品牌插件、本地模型插件）；模型入口、品牌人设、Agent Teams 禁用项按需追加。
- dsh UI 首屏是「请选择模型」：本地模型插件安装并加载模型后会出现在选择器；未安装时仍需云端凭据。
- 主题只有 `BIYOU_THEME` 环境变量这一个开关，且需重启生效；设置页里的主题切换留到 S6。
- 冷启动 5~10 秒（dsh 要加载几百个 ESM 插件）；`dsh.log` 有完整启动日志。
- 首次启动 dsh 需要模型凭据；`~/.dsh` 的既有登录不会自动复用（用独立 DSH_HOME）。
- Windows 的 Node 自动下载未实现，开发时回退 Electron 内置 Node 即可。
- 运行时依赖约 1.2 GB（dsh 的 web bundle 全家桶），S8 打包时需要裁剪。

## 九、视觉规范（以 dsh 为准）

壳是 dsh 的同色系延伸，**不引入第二套色板**。以下取值是在 Electron 里起 dsh 后
用 `getComputedStyle` 实测得到的，不是猜的。

**默认浅色**（`nativeTheme.themeSource = 'light'`），深色走
`@media (prefers-color-scheme: dark)`。切主题用环境变量，不用改代码：

```bash
BIYOU_THEME=dark  ./start.sh     # light | dark | system，缺省 light
BIYOU_THEME=system ./start.sh    # 跟随系统
```

浅色（默认）：

| token | 值 | 用途 |
|---|---|---|
| `--bg-base` | `#ffffff` | 窗口底、顶栏底 |
| `--bg-layer-1` / `--bg-layer-2` | `#f9fafb` / `#f1f3f5` | 侧栏底 / 分组底（与 dsh 侧栏同值） |
| `--bg-raised` | `#ffffff` | toast、抬升面 |
| `--line` / `--line-strong` | `rgba(0,0,0,.08)` / `.12` | 分隔线、控件描边 |
| `--text` / `--text-2` / `--text-3` | `#0f1115` / `#61666b` / `#8a9099` | 主 / 次 / 三级文字 |
| `--hover` / `--active` | `rgba(0,0,0,.04)` / `.06` | 行 hover / 选中 pill |
| `--accent` / `--accent-hover` | `#3964fe` / `#4176e6` | 主按钮、品牌块、就绪指示 |
| `--warning` / `--danger` | `#f7ad31` / `#f25a5a` | 启动中 / 失败 |

深色覆盖（同变量名，只换值）：

| token | 浅色 | 深色 |
|---|---|---|
| `--bg-base` | `#ffffff` | `#151517` |
| `--bg-layer-1` / `--bg-layer-2` | `#f9fafb` / `#f1f3f5` | `#1b1b1c` / `#2b2d31` |
| `--bg-raised` | `#ffffff` | `#43454a` |
| `--line` / `--line-strong` | `rgba(0,0,0,.08)` / `.12` | `rgba(255,255,255,.10)` / `.16` |
| `--text` / `--text-2` / `--text-3` | `#0f1115` / `#61666b` / `#8a9099` | `#f9fafb` / `#cfd3d6` / `#adb2b8` |
| `--hover` / `--active` | `rgba(0,0,0,.04)` / `.06` | `rgba(255,255,255,.06)` / `.10` |

控件规格对齐 dsh：高度 28（图标按钮）/ 32（行）/ 34~38（主按钮），圆角 8（小控件）
/ 12（行与卡片），字号 12（caption）/ 13（辅助）/ 14（正文与导航）/ 17（面板标题），
字体栈与 dsh 完全一致（`-apple-system, system-ui, "Segoe UI", "PingFang SC", …`）。
顶栏取 52px，与 dsh 工具栏节奏衔接。

一致性的关键前提：壳定了 `themeSource`，dsh UI 的「跟随系统」就会落到同一套，
不会出现白壳配深色 dsh。改主题后要复核这一点，别只看壳自己。

复核方式：改完样式跑 `npm run smoke`，把 `shell.png` 与 `dsh.png` 并排看——
两块的底色、描边、圆角必须能无缝拼在一起；`report.json` 里
`dshView.colorScheme.dark` 要和当前主题一致（浅色应为 `false`、`bg` 为 `rgb(255, 255, 255)`）。
dsh 自带的侧栏品牌行属于 dsh UI 本身，S6 用壳内入口弱化，不做 DOM 注入。
