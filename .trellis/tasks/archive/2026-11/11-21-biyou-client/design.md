# Design · 碧游宫客户端 V1.0

> 对应 PRD：`.trellis/tasks/11-21-biyou-client/prd.md`
> 结论先行：**不 fork dsh Desktop，采用「已发布 dsh 运行时 + Electron 品牌壳」**。
> 已验证：`@deepseek-ai/dsh@0.2.2-rc.5` 可在本机启动 Web 运行时并打印带 token 的 URL。

---

## 一、方案选择

| 方案 | 组成 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A. fork dsh Desktop | 复制/子模块化 `deepseek-harness`，改品牌与打包 | 完整桌面体验 | 源码约 643 MB、pnpm workspace、私有包 `dsh-desktop` 未发布、上游同步重 | ❌ V1 不用 |
| B. dsh 运行时 + Electron 壳 | npm 依赖 `@deepseek-ai/dsh`，Electron 启动 `dsh --profile web`，加载其 Web GUI | 复用 dsh 全部 Agent/会话/模型能力；依赖少；可独立打包 | 缺少 dsh Desktop 原生能力；UI 内部仍是 dsh 文案 | ✅ **采用** |
| C. 自研轻客户端 | 自己写聊天 UI，直连 `/v1` | 最小、完全可控 | 放弃 dsh 的 Agent/工具/会话能力，与用户偏好不符 | ❌ 不采用 |

选择 B 的依据：

1. `@deepseek-ai/dsh` 与 `@deepseek-ai/dsh-web-app` **已发布到 npm**，可直接固定版本安装。
2. `@deepseek-ai/dsh-desktop` 是 `private` 包，依赖整个 monorepo 的 workspace 包；fork 成本远高于 V1 价值。
3. 本机 spike 已确认：`dsh --profile web --no-open --port 3099` 可启动，stdout 输出
   `dsh web: http://127.0.0.1:3099/?token=...`，未带 token 访问根路径返回 `401`，认证模型可用。
4. 碧游宫客户端 V1 的核心是「稳定接入网关 + 对话」，不是桌面原生集成；B 足够覆盖 P0。

---

## 二、总体架构

```text
┌──────────────────────────── 碧游宫客户端（Electron） ────────────────────────────┐
│                                                                                  │
│  Electron Main                                                                   │
│    ├─ 首次引导窗口（本地 HTML）                                                    │
│    │    ├─ 选择：网关模式 / 本地模式                                               │
│    │    ├─ 填写 baseURL + API Key                                                 │
│    │    ├─ 测试 GET /v1/models                                                    │
│    │    └─ 写入 dsh provider 配置 + 加密保存密钥                                    │
│    ├─ Runtime Manager                                                             │
│    │    ├─ 解析内置 Node 24 与 dsh CLI                                             │
│    │    ├─ 选空闲端口，spawn `dsh --profile web --no-open --port <p>`              │
│    │    ├─ 解析 stdout 的 `dsh web: <token-url>`                                   │
│    │    └─ 退出时杀整个进程树                                                       │
│    ├─ 主窗口 BrowserWindow                                                        │
│    │    └─ loadURL(token-url) → dsh Web GUI                                       │
│    └─ 本地数据                                                                     │
│         ├─ DSH_HOME = <userData>/dsh                                              │
│         ├─ 配置 = <userData>/dsh/profiles/web/cordis.patch.yml                     │
│         ├─ 密钥 = <userData>/secrets.json（safeStorage 加密）                       │
│         └─ 日志 = <userData>/logs/                                                │
└──────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ OpenAI Chat Completions / SSE
                                    ▼
                 ┌──────────────────────────────────────┐
                 │ 碧游宫网关 NewAPI  /v1                │
                 │ 或 本机准圣节点     /v1                │
                 └──────────────────────────────────────┘
```

### 2.1 进程模型

- **Electron 主进程**：唯一控制面，负责窗口、配置、密钥、子进程生命周期。
- **dsh 运行时子进程**：独立 Node 进程，监听 `127.0.0.1:<free-port>`，只服务本机。
- **渲染进程**：加载 dsh Web GUI，阻断外部导航；不开启 Node 集成。

### 2.2 为什么单独带 Node

dsh 要求 `node >= 24.9.0`。不依赖 Electron 内置 Node 版本，避免 Electron 升级时 Node 版本不匹配：

- 打包时下载官方 Node 24 LTS（mac arm64/x64、win x64）；
- 运行时用该 Node 执行 `dsh/lib/bin.js`；
- `node -v` 在构建阶段校验，不满足直接失败。

本机 spike 使用 Node `v26.5.0` 通过；V1 固定 Node 24 LTS，避免过新版本。

---

## 三、目录与模块

```text
client/
├── package.json
├── tsconfig.json
├── electron/
│   ├── main.ts                 # app 生命周期、窗口、单实例、菜单
│   ├── runtime.ts              # 定位 Node/dsh、spawn、解析 token URL、kill tree
│   ├── config.ts               # DSH_HOME、cordis.patch.yml 读写与原子替换
│   ├── secrets.ts              # safeStorage 加解密、密钥生命周期
│   ├── onboarding.ts           # 引导流程与 IPC
│   ├── probe.ts                # /v1/models、/v1/health 探测与错误映射
│   ├── menu.ts                 # 碧游宫菜单、打开日志/配置/关于
│   └── preload.ts              # contextBridge 暴露最小 API
├── renderer/
│   └── setup.html              # 引导页（无框架，纯 HTML/CSS/TS）
├── scripts/
│   └── prepare-runtime.mjs     # 安装 dsh 生产依赖 + 下载 Node
├── build/                      # 构建中间产物（gitignore）
├── electron-builder.config.cjs
├── resources/
│   ├── icon.icns
│   └── icon.ico
└── README.md
```

### 3.1 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `runtime.ts` | 启动/停止 dsh、解析 URL、日志落盘 | 不读写业务配置 |
| `config.ts` | 生成/合并 provider YAML | 不保存明文密钥 |
| `secrets.ts` | `safeStorage` 加密密钥 | 不把密钥写入 YAML/日志 |
| `probe.ts` | 调 `/v1/models`、`/v1/health` | 不代理推理请求 |
| `onboarding.ts` | 引导状态机与 IPC | 不直接操作 dsh 子进程 |
| `main.ts` | 组装、窗口、菜单、退出 | 不解析模型列表 |

---

## 四、dsh 运行时契约

### 4.1 启动命令

```bash
<runtime>/node/bin/node <runtime>/dsh/lib/bin.js \
  --profile web --no-open --port <free-port>
```

环境变量：

| 变量 | 值 | 说明 |
|---|---|---|
| `DSH_HOME` | `<userData>/dsh` | 隔离用户数据，避免和本机 dsh 冲突 |
| `BIYOU_GATEWAY_KEY` | 解密后的网关 Key | 仅进程环境，不落盘 |
| `BIYOU_LOCAL_KEY` | 解密后的本地 Key / `local` | 仅进程环境 |
| `NO_COLOR` | `1` | 日志稳定 |
| `NODE_ENV` | `production` | 打包模式 |
| `DSH_CLIENT_VERSION` | 客户端版本 | 版本诊断 |

### 4.2 就绪判定

1. 监听 stdout/stderr；
2. 用正则匹配 `^dsh web:\s+(https?://\S+)`；
3. 命中后立即 `loadURL`；
4. 30 秒未命中则失败，弹窗并提供日志路径；
5. 不采用「端口可连接即成功」，因为 dsh 可能仍在初始化。

### 4.3 退出与清理

- POSIX：`spawn(..., { detached: true })`，退出时 `process.kill(-pid, 'SIGTERM')`，5 秒后 `SIGKILL`。
- Windows：`taskkill /pid <pid> /T /F`。
- 监听 `before-quit`、`window-all-closed`、`process.on('exit')` 三个入口，保证清理幂等。
- 启动前清理上一次遗留的 `runtime.json`（记录 pid/port），若 pid 仍存活则先杀。

### 4.4 端口策略

- 默认从 `39000` 起找空闲端口（避开 dsh 默认 3080、准圣 4010、网关 3000、阵图 4020、灵脉 9090/3001）；
- 绑定 `127.0.0.1`；
- 端口写入 `runtime.json`，仅用于诊断和残留清理。

---

## 五、Provider 配置

### 5.1 配置位置

```text
<DSH_HOME>/profiles/web/cordis.patch.yml
```

### 5.2 网关模式

```yaml
- id: llm-pi-ai
  config:
    providers:
      biyou-gateway:
        displayName: 碧游宫网关
        apiKeyEnv: BIYOU_GATEWAY_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:3000/v1
        models:
          - id: qwen2.5:7b
            name: qwen2.5:7b
            contextWindow: 32768
            maxTokens: 4096
```

### 5.3 本地模式

```yaml
- id: llm-pi-ai
  config:
    providers:
      biyou-local:
        displayName: 本机准圣
        apiKeyEnv: BIYOU_LOCAL_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:4010/v1
        models:
          - id: qwen2.5:7b
            name: qwen2.5:7b
            contextWindow: 32768
            maxTokens: 4096
```

### 5.4 合并规则

1. 读取现有 YAML；解析失败则备份为 `cordis.patch.yml.bak-<timestamp>` 并重新生成最小文件。
2. 查找 `id: llm-pi-ai` 行；存在则只合并/替换 `providers` 下目标 provider，保留其他 provider 与其他行。
3. 不存在则追加一行。
4. 原子写：同目录临时文件 + `rename`。
5. 写入后用 `yaml` 解析自检；失败则回滚备份。
6. **不写入 `apiKey` / 明文密钥**，只写 `apiKeyEnv`。

### 5.5 模型元数据

`/v1/models` 只返回 id/owned_by 等字段，不含上下文窗口。V1 策略：

- 默认 `contextWindow: 32768`、`maxTokens: 4096`；
- 引导页允许用户修改这两个值（P1）；
- 在 README 说明：实际值以模型为准，错误设置会导致过早压缩或请求被拒；
- 后续可从节点 `/v1/health` 的 `models[]` 读取能力并自动填充。

### 5.6 密钥存储

- 使用 Electron `safeStorage.encryptString`；
- 存储文件 `<userData>/secrets.json`，内容为 base64 密文 + 非敏感元数据；
- macOS 走 Keychain，Windows 走 DPAPI；
- 若 `safeStorage.isEncryptionAvailable() === false`，引导页明确提示「当前系统无法加密保存，密钥只保留到本次退出」；
- 日志、诊断包、错误弹窗统一脱敏 `sk-...` 与 `Bearer ...`。

---

## 六、首次引导流程

```text
[启动]
  │
  ├─ 读取 secrets + config
  │     ├─ 完整 → 直接启动运行时
  │     └─ 缺失/损坏 → 引导窗口
  │
[引导窗口]
  Step 1 选择模式：网关 / 本地
  Step 2 输入 baseURL、API Key
  Step 3 点击「测试连接」
        ├─ GET {baseURL}/models（Bearer）
        ├─ 200 → 展示模型多选
        ├─ 401/403 → 「令牌无效或无权限」
        ├─ 404 → 「地址不是 OpenAI 兼容入口，应以 /v1 结尾」
        └─ 超时 → 「检查地址、防火墙、服务是否启动」
  Step 4 勾选模型、设置默认模型
  Step 5 写入配置 → 启动 dsh → 关闭引导窗口 → 主窗口
```

### 6.1 本地模式额外检查

- 先 `GET /v1/health`；
- `status=ready` 才允许继续；
- `loading` 提示「模型加载中」；
- `down` 提示「准圣服务或 Ollama 未启动」，附带启动命令：

```bash
bash scripts/run_quasheng.sh
```

---

## 七、品牌与界面

### 7.1 V1 品牌范围

| 位置 | 做法 |
|---|---|
| 应用名 / 包名 | `碧游宫` / `com.biyou.palace.client` |
| 图标 | `resources/icon.icns`、`icon.ico` |
| 主窗口标题 | `碧游宫` |
| 菜单 | 关于碧游宫、打开日志、打开配置、重新配置、退出 |
| 关于页 | 客户端版本、dsh 版本、Electron / Node 版本 |
| dsh Web UI 内部 | **V1 不替换**，保留上游品牌 |

### 7.2 为什么不在 V1 注入 UI 文案

dsh Web UI 是编译产物，DOM 结构不稳定；注入 CSS/JS 替换文案会随版本升级失效。V1 先保证功能与打包，品牌深度留到 V1.1（可选：顶部自定义标题栏 + WebContentsView）。

---

## 八、打包与分发

### 8.1 运行时准备

`scripts/prepare-runtime.mjs`：

1. 读取 `client/package.json` 的 `dshVersion` 与 `nodeVersion`；
2. 在 `build/runtime/dsh` 执行：

   ```bash
   npm install --omit=dev --no-audit --no-fund @deepseek-ai/dsh@<version>
   ```

3. 下载 Node 官方二进制到 `build/runtime/node`：
   - `darwin-arm64`、`darwin-x64`、`win-x64`；
   - 支持 `NODE_MIRROR` 环境变量；
4. 校验 `node -v` 与 dsh 版本；
5. 写入 `build/runtime/runtime.json`（版本、平台、时间）。

### 8.2 electron-builder

```js
module.exports = {
  appId: 'com.biyou.palace.client',
  productName: '碧游宫',
  asar: true,
  files: ['dist-electron/**', 'renderer/**', 'package.json'],
  extraResources: [
    { from: 'build/runtime', to: 'runtime' },
  ],
  mac: {
    target: ['dmg', 'zip'],
    category: 'public.app-category.productivity',
    icon: 'resources/icon.icns',
  },
  win: {
    target: [{ target: 'nsis', arch: ['x64'] }],
    icon: 'resources/icon.ico',
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
  },
};
```

### 8.3 产物

| 平台 | 目标 | 命令 | V1 状态 |
|---|---|---|---|
| macOS arm64 | dmg + zip | `npm run dist:mac:arm64` | 本机验证 |
| macOS x64 | dmg + zip | `npm run dist:mac:x64` | CI 验证 |
| Windows x64 | NSIS exe | `npm run dist:win:x64` | Windows CI 验证 |

未签名产物：

- macOS 首次打开需右键「打开」，或 `xattr -dr com.apple.quarantine`；
- Windows 会出现 SmartScreen 提示；
- 在 README 与安装引导中明确说明。

### 8.4 CI

GitHub Actions 矩阵：

```yaml
strategy:
  matrix:
    include:
      - os: macos-14      # arm64
        script: dist:mac:arm64
      - os: macos-13      # x64
        script: dist:mac:x64
      - os: windows-latest
        script: dist:win:x64
```

步骤：checkout → setup-node 24 → `npm ci` → `npm run prepare:runtime` → `npm run <script>` → 上传 artifact。

---

## 九、安全设计

1. **仅回环**：dsh 只绑定 `127.0.0.1`，不传 `--host`。
2. **Token URL**：只加载 dsh 打印的带 token URL；不把 token 写入日志。
3. **渲染隔离**：`contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`。
4. **导航限制**：`will-navigate` 只允许 `127.0.0.1:<port>`；`setWindowOpenHandler` 一律 `deny`，外部链接走 `shell.openExternal`。
5. **密钥最小暴露**：只在 spawn 时注入环境变量；不通过 IPC 回传密钥到渲染进程。
6. **日志脱敏**：统一过滤 `sk-[A-Za-z0-9_-]+`、`Bearer\s+\S+`、`token=[^&\s]+`。
7. **遥测**：dsh Web profile 不启用桌面 product analytics；客户端不新增任何上报。验收时抓包确认无外联。

---

## 十、错误处理

| 场景 | 表现 | 处理 |
|---|---|---|
| 端口被占用 | 启动失败 | 自动换下一个空闲端口 |
| dsh 启动超时 | 主窗口不打开 | 弹窗 + 日志路径 + 「重试 / 重新配置 / 退出」 |
| dsh 崩溃 | 窗口空白 | 捕获 exit，弹窗，保留日志，允许重启运行时 |
| 网关 401/403 | 模型列表为空 | 引导页提示令牌问题，不进入主界面 |
| 网关 404 | 模型列表为空 | 提示 baseURL 应以 `/v1` 结尾 |
| 模型列表为空 | 无法选模型 | 允许手动输入模型 ID（P1） |
| 本地节点 `loading` | 无法对话 | 提示等待模型加载，可重试 |
| 配置 YAML 损坏 | 启动异常 | 备份后重建最小配置，并提示用户 |

---

## 十一、兼容性与许可证

- dsh：MIT；分发时附带 `LICENSE` 与版本信息。
- Electron：MIT。
- Node：MIT 类许可证；附带 Node 的 `LICENSE`。
- 客户端代码：沿用仓库现有许可证策略。
- 版本固定：`dshVersion` 写入 `package.json`，升级需单独任务与回归。

---

## 十二、验证策略

### 12.1 本机可验证

- dsh 运行时启动与 token URL 解析；
- 引导页 → 配置写入 → 主窗口加载；
- 网关模式：`http://127.0.0.1:3000/v1` + 现有 NewAPI key；
- 本地模式：`http://127.0.0.1:4010/v1` + 现有准圣；
- 退出清理：连续 10 次启停，检查进程与端口；
- macOS arm64 未签名 `.app` / `.dmg`。

### 12.2 需要 CI / 其他机器

- macOS x64 产物；
- Windows x64 产物与 SmartScreen 行为；
- 跨机访问中枢机网关（需调整 NewAPI 绑定地址）。

### 12.3 回归

- `bash scripts/verify.sh`
- `.venv/bin/python -m pytest zhentu -q`
- 不启停现有服务，不修改 `~/biyou`。

---

## 十三、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| dsh CLI 参数/输出格式变化 | 无法解析 token URL | 固定版本；启动 smoke test；解析失败时回退到端口 + 日志诊断 |
| dsh Web UI 依赖外部 CDN | 内网加载失败 | 验证离线加载；必要时在 Electron 层阻断外链并记录 |
| YAML patch 语义变化 | provider 不生效 | 启动后检查模型列表；失败则回退到 dsh 设置页手动配置 |
| Node 二进制下载受限 | 打包失败 | `NODE_MIRROR` + CI 缓存；提供手动放置目录 |
| dsh 需要原生模块 | 启动失败 | `npm install` 在目标平台执行；`extraResources` 不放 asar |
| 网关只绑 127.0.0.1 | 跨机不可用 | README 说明；必要时另开部署任务调整绑定 |
| 未签名产物被拦截 | 用户无法安装 | 文档说明右键打开 / 允许运行；V1.1 做签名 |

---

## 十四、分阶段落地

| 阶段 | 内容 | 验证 |
|---|---|---|
| S0 | 运行时 spike（已完成） | dsh 启动、token URL、401 行为 |
| S1 | Electron 骨架 + runtime.ts | 本机启动 dsh 并加载主窗口 |
| S2 | 引导页 + 配置/密钥 | 网关与本地两种模式可保存 |
| S3 | 品牌壳 + 菜单 + 日志 | 应用名、图标、诊断入口 |
| S4 | 打包 + macOS 产物 | `.app` / `.dmg` 可启动 |
| S5 | CI + Windows 脚本 + 文档 | 三平台产物可复现 |

---

## 十五、运行时状态机（细化）

### 15.1 状态定义

| 状态 | 含义 | 用户可见 |
|---|---|---|
| `idle` | 应用已启动，未配置或未启动运行时 | 引导页 |
| `preparing` | 校验配置、写 YAML、`--dump-config` | 启动页「正在准备」 |
| `starting` | 已 spawn dsh，等待 token URL | 启动页「正在启动碧游宫」 |
| `ready` | 主窗口已加载 dsh Web GUI | 主界面 |
| `stopping` | 正在结束进程树 | 启动页「正在退出」 |
| `failed` | 启动/运行失败 | 错误页 + 重试/重新配置/打开日志 |

### 15.2 状态迁移

```text
idle ──保存配置──▶ preparing
preparing ──校验通过──▶ starting
preparing ──校验失败──▶ failed
starting ──解析到 token URL──▶ ready
starting ──30s 超时/进程退出──▶ failed
ready ──dsh 崩溃──▶ failed
ready ──退出应用──▶ stopping ──▶ idle
failed ──重试──▶ preparing
failed ──重新配置──▶ idle
```

### 15.3 关键约束

- 同一时刻只允许一个 dsh 子进程。
- `preparing` 阶段先跑 `--dump-config`，失败不 spawn，避免把 YAML 错误带到运行时。
- `failed` 状态必须保留最后 200 行日志与退出码，供诊断。
- 单实例锁：第二个实例只聚焦已有窗口，不启动新运行时。

---

## 十六、主进程 ↔ 渲染进程 IPC 契约

### 16.1 通道

| 通道 | 方向 | 入参 | 出参 |
|---|---|---|---|
| `biyou:get-state` | invoke | 无 | `ClientState` |
| `biyou:test-connection` | invoke | `{mode, baseUrl, apiKey}` | `{ok, models?, health?, error?}` |
| `biyou:save-config` | invoke | `{mode, baseUrl, apiKey, models, defaultModel, contextWindow, maxTokens}` | `{ok, error?}` |
| `biyou:restart-runtime` | invoke | 无 | `{ok, error?}` |
| `biyou:open-logs` | invoke | 无 | `{ok}` |
| `biyou:open-config` | invoke | 无 | `{ok}` |
| `biyou:open-external` | invoke | `{url}` | `{ok}` |
| `biyou:state-changed` | send | `ClientState` | 无 |

### 16.2 `ClientState`

```ts
type ClientState = {
  state: 'idle' | 'preparing' | 'starting' | 'ready' | 'stopping' | 'failed'
  mode?: 'gateway' | 'local'
  baseUrl?: string
  models: Array<{ id: string; name: string }>
  defaultModel?: string
  error?: { code: string; message: string; detail?: string }
  versions: { app: string; dsh: string; electron: string; node: string }
}
```

### 16.3 安全规则

- `preload.ts` 只暴露上述方法，不暴露 `ipcRenderer`。
- 主进程校验 `event.senderFrame.url` 必须是本地引导页或 dsh 的 `127.0.0.1:<port>`。
- `apiKey` 只从渲染进程传入主进程，永不反向回传；`get-state` 不含密钥。
- `open-external` 只允许 `http:` / `https:`，拒绝 `file:`、`javascript:`、自定义协议。

---

## 十七、本地数据与配置 Schema

### 17.1 `client-config.json`

```json
{
  "version": 1,
  "mode": "gateway",
  "baseUrl": "http://127.0.0.1:3000/v1",
  "providerId": "biyou-gateway",
  "models": [
    { "id": "qwen2.5:7b", "name": "qwen2.5:7b", "contextWindow": 32768, "maxTokens": 4096 }
  ],
  "defaultModel": "qwen2.5:7b",
  "dshVersion": "0.2.2-rc.5"
}
```

- 不含任何密钥。
- `mode` 为 `local` 时 `providerId=biyou-local`，`baseUrl` 默认 `http://127.0.0.1:4010/v1`。
- 文件权限 `0600`。

### 17.2 `secrets.json`

```json
{
  "version": 1,
  "gateway": { "cipher": "base64...", "updatedAt": "2026-11-21T..." },
  "local": { "cipher": "base64...", "updatedAt": "2026-11-21T..." }
}
```

- 只存 `safeStorage.encryptString()` 的结果。
- 解密失败视为未配置，引导用户重新输入，不崩溃。
- 文件权限 `0600`。

### 17.3 `runtime.json`

```json
{
  "pid": 12345,
  "port": 39001,
  "url": "http://127.0.0.1:39001/",
  "startedAt": "2026-11-21T..."
}
```

- 启动前读取；若 `pid` 存活且命令行包含本应用 runtime 路径，则先杀。
- 正常退出删除；异常退出保留，供下次清理。

### 17.4 目录权限

```text
<userData>/                 0700
<userData>/dsh/             0700
<userData>/logs/            0700
<userData>/client-config.json 0600
<userData>/secrets.json     0600
```

---

## 十八、界面细化

### 18.1 首次引导

```text
┌──────────────────────────────────────────────┐
│  碧游宫                                       │
│  不问根骨，万仙入阵                            │
│                                              │
│  [ 碧游宫网关 ]        [ 本机准圣节点 ]        │
│   统一入口、按模型路由    直连 127.0.0.1:4010   │
│                                              │
│  地址  [ http://127.0.0.1:3000/v1        ]   │
│  令牌  [ ••••••••••••••••••••••••••••    ]   │
│                                              │
│  ▸ 高级设置                                   │
│    上下文窗口 [ 32768 ]  最大输出 [ 4096 ]    │
│                                              │
│           [ 测试连接 ]   [ 下一步 ]           │
└──────────────────────────────────────────────┘
```

测试成功后：

```text
┌──────────────────────────────────────────────┐
│  选择模型                                     │
│  ☑ qwen2.5:7b         上下文 32768           │
│  ☐ nomic-embed-text   仅向量化               │
│                                              │
│  默认模型 [ qwen2.5:7b ▾ ]                   │
│                                              │
│           [ 返回 ]   [ 进入碧游宫 ]           │
└──────────────────────────────────────────────┘
```

### 18.2 启动页

```text
┌──────────────────────────────────────────────┐
│                碧 游 宫                       │
│           正在启动推演阵图…                    │
│           [====------] 初始化运行时            │
│                                              │
│   首次启动可能需要 5–10 秒                     │
└──────────────────────────────────────────────┘
```

### 18.3 错误页

```text
┌──────────────────────────────────────────────┐
│  无法启动碧游宫                               │
│                                              │
│  原因：网关不可达 / 令牌无效 / 运行时超时       │
│  详情：GET http://.../v1/models → 401         │
│                                              │
│  [ 重试 ]  [ 重新配置 ]  [ 打开日志 ]  [ 退出 ] │
└──────────────────────────────────────────────┘
```

### 18.4 主窗口

- 直接加载 dsh Web GUI；
- 标题「碧游宫」；
- 菜单栏：碧游宫 / 编辑 / 视图 / 窗口 / 帮助；
- 不额外包裹顶部栏，避免挤压 dsh 布局；
- 连接状态只在错误时通过系统弹窗或菜单「连接诊断」呈现。

---

## 十九、跨机访问网关（重要约束）

当前 `deploy/newapi/docker-compose.yml` 将 NewAPI 绑定在 `127.0.0.1:3000`，因此：

| 客户端位置 | 可用性 | 说明 |
|---|---|---|
| 与网关同一台机器 | ✅ 默认可用 | `http://127.0.0.1:3000/v1` |
| 局域网其他机器 | ❌ 默认不可用 | 需把监听地址改为 `0.0.0.0` 或指定内网 IP |

### 19.1 V1 处理方式

1. 客户端不修改服务端；引导页只校验用户填写的 URL。
2. 当 URL 主机不是 `127.0.0.1` / `localhost` 且连接失败时，提示：
   - 检查中枢机防火墙；
   - 确认 NewAPI 是否只绑定了回环地址；
   - 参考 `docs/deployment.md` 的「局域网访问」小节。
3. 新增可选环境变量（默认行为不变）：

   ```yaml
   ports:
     - "${NEWAPI_BIND:-127.0.0.1}:${NEWAPI_PORT:-3000}:3000"
   ```

   用户在中枢机 `.env` 设置 `NEWAPI_BIND=0.0.0.0` 后，才能被局域网客户端访问。

### 19.2 安全要求

- 仅在可信内网使用 `0.0.0.0`；
- 必须使用强管理员密码与独立客户端令牌；
- 不建议直接把 NewAPI 管理端口暴露到公网；
- 如有公网需求，必须前置反向代理、TLS 与访问控制（V1.1）。

> 该改动是**可选配置 + 文档**，不改变默认安全姿态；是否在本任务落地由评审决定（建议落地，否则客户端跨机不可用）。

---

## 二十、打包细化

### 20.1 固定版本

| 组件 | 版本 | 说明 |
|---|---|---|
| dsh | `0.2.2-rc.5` | npm `@deepseek-ai/dsh` |
| Node | `v24.22.3` | 当前 24 LTS |
| Electron | `44.7.0` | 与 dsh Desktop 同代 |
| electron-builder | `26.17.0` | 打包 |
| TypeScript | `5.9.x` | 客户端编译 |

### 20.2 运行时目录

```text
client/build/runtime/
├── node/
│   └── bin/node                 # Windows: node.exe
├── dsh/
│   ├── node_modules/@deepseek-ai/dsh/lib/bin.js
│   └── node_modules/...         # dsh 全部生产依赖
└── runtime.json
```

### 20.3 `prepare-runtime.mjs` 步骤

1. 读取 `package.json` 的 `biyou.dshVersion`、`biyou.nodeVersion`。
2. `rm -rf build/runtime`。
3. `npm install --omit=dev --no-audit --no-fund --prefix build/runtime/dsh @deepseek-ai/dsh@<dshVersion>`。
4. 下载 Node：
   - `https://nodejs.org/dist/v24.22.3/node-v24.22.3-darwin-arm64.tar.gz`
   - `https://nodejs.org/dist/v24.22.3/node-v24.22.3-darwin-x64.tar.gz`
   - `https://nodejs.org/dist/v24.22.3/node-v24.22.3-win-x64.zip`
   - 同时下载 `SHASUMS256.txt` 并校验 SHA256。
   - `NODE_MIRROR` 可替换前缀（如 `https://npmmirror.com/mirrors/node`）。
5. 解压后只保留 `bin/node`（POSIX）或 `node.exe`（Windows），删除 npm/npx/corepack 等无关文件。
6. 执行 `<node> -v` 与 `<node> dsh/lib/bin.js --version`，断言版本。
7. 写 `runtime.json`：`{dshVersion, nodeVersion, platform, arch, builtAt}`。

### 20.4 打包命令

```bash
# macOS arm64（本机）
npm run prepare:runtime -- --platform darwin --arch arm64
npm run dist:mac:arm64 -- --dir

# macOS x64（x64 机器或 CI）
npm run prepare:runtime -- --platform darwin --arch x64
npm run dist:mac:x64

# Windows x64（Windows 或 CI）
npm run prepare:runtime -- --platform win32 --arch x64
npm run dist:win:x64
```

### 20.5 未签名构建

```bash
export CSC_IDENTITY_AUTO_DISCOVERY=false
export APPLE_ID=
export APPLE_APP_SPECIFIC_PASSWORD=
```

- macOS 产物不公证；
- Windows 不签名；
- README 写清首次打开方式。

---

## 二十一、测试矩阵（细化）

### 21.1 单元测试（Node test runner）

| 模块 | 用例 |
|---|---|
| `runtime.parseUrl` | 标准 token URL、无 token、多行输出、URL 带空格 |
| `runtime.freePort` | 端口占用时递增、范围边界 |
| `config.mergeProvider` | 空文件、已有其他 provider、已有目标 provider、YAML 损坏 |
| `config.redact` | `sk-`、`Bearer`、`token=`、嵌套对象 |
| `probe.mapError` | 401、403、404、超时、非 JSON、空列表 |
| `secrets` | 加密可用/不可用降级（mock safeStorage） |

### 21.2 集成测试（Electron smoke）

新增 `BIYOU_SMOKE=1`：

1. 使用临时 `userData` 与临时配置；
2. 启动运行时；
3. 解析 token URL；
4. 用 `fetch(tokenUrl, { redirect: 'manual' })` 断言非 5xx；
5. 打印 `SMOKE_OK <port>`；
6. 退出并断言无子进程。

```bash
BIYOU_SMOKE=1 npm run smoke
```

### 21.3 配置校验

```bash
<runtime>/node <runtime>/dsh/lib/bin.js --profile web --dump-config
```

- 退出码 0 且输出包含 `biyou-gateway` / `biyou-local` 才算配置生效；
- 失败时不 spawn，直接进入 `failed`。

### 21.4 真实对话

- 网关模式：用现有 NewAPI key，在 dsh UI 发一条消息，确认流式输出；
- 本地模式：准圣 `/v1/health=ready`，发一条消息；
- 记录首 token 时间与错误日志。

### 21.5 回归

- `bash scripts/verify.sh`
- `.venv/bin/python -m pytest zhentu -q`
- 连续 10 次启动/退出，检查 `pgrep` 与端口。

---

## 二十二、已定决策（评审建议）

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | V1 使用 dsh Web GUI + Electron 壳，不 fork dsh Desktop | 官方 Desktop 包未发布；CLI + Web GUI 已实测可用 |
| D2 | V1 只做应用壳品牌，不注入 dsh UI 文案 | 避免随 dsh 升级失效 |
| D3 | 网关地址预填 `http://127.0.0.1:3000/v1`，可编辑 | 同机开箱即用；跨机由用户改 |
| D4 | 跨机访问通过可选 `NEWAPI_BIND` + 文档解决，默认仍绑回环 | 不降低默认安全性 |
| D5 | 上下文窗口/最大输出在引导页「高级设置」可改 | `/v1/models` 不提供该元数据 |
| D6 | Windows V1 不签名，接受 SmartScreen 提示 | 内网分发优先跑通 |
| D7 | 固定 dsh `0.2.2-rc.5`、Node `v24.22.3` | 可复现，升级单独任务 |

---

## 二十三、仍待确认

1. 是否同意 D4：在本任务中给 `deploy/newapi` 增加可选 `NEWAPI_BIND`（默认不变）。
2. 是否同意 D2：V1 不替换 dsh UI 内部文案。
3. 是否需要在 V1 就做「一键切换网关/本地」，还是先重启应用切换。
