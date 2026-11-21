# Implement · 碧游宫客户端 V1.0

> 对应 PRD / Design：`.trellis/tasks/11-21-biyou-client/{prd.md,design.md}`
> 执行前置：本文件评审通过、`task.py start` 成功。
> 原则：先跑通本机 macOS，再补 Windows 构建；不修改服务端行为。

---

## 零、前置事实（已完成）

- `@deepseek-ai/dsh@0.2.2-rc.5` 已发布，`@deepseek-ai/dsh-web-app` 随依赖提供 Web GUI。
- 本机 spike 通过：

  ```bash
  DSH_HOME=/tmp/biyou-dsh-home \
    ./node_modules/.bin/dsh --profile web --no-open --port 3099
  # stdout: dsh web: http://127.0.0.1:3099/?token=...
  # 未带 token 访问 / → 401
  ```

- 结论：V1 不需要 fork dsh Desktop，Electron 只需启动 CLI 并加载 token URL。
- dsh 要求 Node `>=24.9.0`；V1 打包 Node 24 LTS。

---

## 一、阶段与检查点

### S1 · Electron 骨架与运行时

**目标**：应用能启动内置 dsh，并显示 dsh Web GUI。

- [ ] 创建 `client/package.json`、`tsconfig.json`、`.gitignore`。
- [ ] 依赖：`electron`、`electron-builder`、`typescript`、`yaml`、`@types/node`。
- [ ] 实现 `electron/runtime.ts`：
  - [ ] 解析 `<resources>/runtime/node/bin/node` 与 `<resources>/runtime/dsh/lib/bin.js`（开发模式回退到本机 `node` / `npx dsh`）。
  - [ ] 选空闲端口（39000 起）。
  - [ ] `spawn` 时 `detached: true`，注入 `DSH_HOME`、密钥环境变量。
  - [ ] 解析 stdout `^dsh web:\s+(https?://\S+)`，30 秒超时。
  - [ ] 写 `runtime.json`（pid/port/url），退出时清理。
  - [ ] 实现 POSIX 进程组 kill 与 Windows `taskkill /T /F`。
- [ ] 实现 `electron/main.ts`：单实例锁、创建窗口、`loadURL`、退出清理。
- [ ] 本机验证：
  - [ ] `npm run dev` 打开窗口，页面可交互。
  - [ ] 关闭应用后 `pgrep -fl dsh` 为空，端口释放。

**检查点 G1**：连续启动/退出 5 次无孤儿进程、无端口冲突。

---

### S2 · 引导页、配置与密钥

**目标**：用户可配置网关/本地模式，配置写入 dsh profile。

- [ ] `renderer/setup.html`：三步表单（模式 → 地址/密钥 → 模型）。
- [ ] `electron/probe.ts`：
  - [ ] `GET {baseURL}/models`，Bearer 鉴权，10 秒超时。
  - [ ] 错误映射：401/403、404、超时、非 JSON、空列表。
  - [ ] 本地模式先 `GET /v1/health`，检查 `status`。
- [ ] `electron/config.ts`：
  - [ ] 确保 `DSH_HOME/profiles/web` 存在。
  - [ ] 读/合并/写 `cordis.patch.yml`（保留其他行与 provider）。
  - [ ] 原子写 + 解析自检 + 失败回滚。
  - [ ] provider 只写 `apiKeyEnv`，不写明文。
- [ ] `electron/secrets.ts`：
  - [ ] `safeStorage` 加密保存；不可用时降级为内存并提示。
  - [ ] 日志脱敏工具（`sk-`、`Bearer`、`token=`）。
- [ ] 本机验证：
  - [ ] 网关模式：NewAPI `127.0.0.1:3000` + 现有 key，模型列表非空，进入对话。
  - [ ] 本地模式：准圣 `127.0.0.1:4010`，`/v1/health=ready`，可对话。
  - [ ] 错误 key → 明确提示，不写入配置。
  - [ ] 重启应用保留配置，YAML 中无明文 key。

**检查点 G2**：两种模式各完成一次真实流式对话；`grep -R "sk-" <userData>` 无命中。

---

### S3 · 品牌壳、菜单与日志

**目标**：像一个独立产品，而不是 dsh 的调试窗口。

- [ ] `productName: 碧游宫`、`appId: com.biyou.palace.client`。
- [ ] 图标：`resources/icon.icns`、`icon.ico`（可用简单占位图，后续替换）。
- [ ] 窗口标题「碧游宫」；macOS 关闭按钮行为与单实例。
- [ ] 菜单：
  - [ ] 关于碧游宫（版本、dsh、Electron、Node）。
  - [ ] 打开日志目录。
  - [ ] 打开配置目录。
  - [ ] 重新配置（回到引导页）。
  - [ ] 退出。
- [ ] 日志：dsh stdout/stderr 落 `<userData>/logs/dsh.log`，按大小轮转（如 5 MB × 3）。
- [ ] 安全：`contextIsolation`、`sandbox`、禁用 `nodeIntegration`；限制导航与新窗口。
- [ ] 本机验证：
  - [ ] 菜单项均可用。
  - [ ] 日志中无 token / key。
  - [ ] 外部链接不在应用内打开。

**检查点 G3**：关于页版本正确；日志脱敏通过。

---

### S4 · 打包 macOS

**目标**：产出可双击运行的未签名 `.app` / `.dmg`。

- [ ] `scripts/prepare-runtime.mjs`：
  - [ ] 安装 `@deepseek-ai/dsh@<pinned>` 到 `build/runtime/dsh`（`--omit=dev`）。
  - [ ] 下载 Node 24 LTS 到 `build/runtime/node`（支持 `NODE_MIRROR`）。
  - [ ] 校验 `node -v`、dsh 版本，写 `runtime.json`。
- [ ] `electron-builder.config.cjs`：
  - [ ] `extraResources: build/runtime → runtime`。
  - [ ] mac target `dmg` + `zip`，arm64 / x64。
  - [ ] `asar: true`，runtime 不放 asar。
- [ ] `package.json` scripts：
  - [ ] `dev`、`build`、`prepare:runtime`、`dist:mac:arm64`、`dist:mac:x64`、`dist:win:x64`。
- [ ] 本机验证：
  - [ ] `npm run dist:mac:arm64:dir` 产出 `.app`。
  - [ ] 双击启动，完成一次对话。
  - [ ] `open` 启动后退出，无残留。
  - [ ] `npm run dist:mac:arm64` 产出 `.dmg`。

**检查点 G4**：在未安装 Node 的路径/用户环境下，`.app` 仍可启动（用 `env -i` 或改名 shell 验证）。

---

### S5 · Windows 构建、CI 与文档

**目标**：三平台可复现，别人能照着构建。

- [ ] `client/README.md`：
  - [ ] 开发、构建、安装、未签名提示。
  - [ ] 网关/本地模式说明与端口。
  - [ ] 已知限制（UI 品牌、模型元数据、跨机网关）。
- [ ] GitHub Actions：
  - [ ] `macos-14` arm64、`macos-13` x64、`windows-latest` x64。
  - [ ] 缓存 npm；`npm ci`；`prepare:runtime`；打包；上传 artifact。
- [ ] Windows 构建脚本：
  - [ ] NSIS x64；确认 `taskkill /T /F` 清理。
  - [ ] 记录 SmartScreen 提示。
- [ ] 根部 `README.md` 增加「碧游宫客户端」入口。
- [ ] 验收记录写入 `docs/reports/biyou-client-2026-11-21.md`（或实际日期）。

**检查点 G5**：CI 三平台产物可下载；Windows 产物在 Windows 上可安装并对话。

---

## 二、文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `client/package.json` | 新增 | Electron / dsh 版本、脚本 |
| `client/tsconfig.json` | 新增 | TS 配置 |
| `client/electron/main.ts` | 新增 | 生命周期、窗口 |
| `client/electron/runtime.ts` | 新增 | dsh 启停、URL 解析 |
| `client/electron/config.ts` | 新增 | YAML 合并 |
| `client/electron/secrets.ts` | 新增 | safeStorage |
| `client/electron/probe.ts` | 新增 | 模型/健康探测 |
| `client/electron/menu.ts` | 新增 | 菜单 |
| `client/electron/preload.ts` | 新增 | 最小 IPC |
| `client/renderer/setup.html` | 新增 | 引导页 |
| `client/scripts/prepare-runtime.mjs` | 新增 | 运行时准备 |
| `client/electron-builder.config.cjs` | 新增 | 打包 |
| `client/resources/icon.icns` / `icon.ico` | 新增 | 图标 |
| `client/README.md` | 新增 | 使用与构建 |
| `.github/workflows/client.yml` | 新增 | 三平台 CI |
| `README.md` | 修改 | 添加客户端入口 |
| `.gitignore` | 修改 | 忽略 `client/build`、`dist`、`node_modules` |

---

## 三、验证命令

### 3.1 客户端

```bash
cd client
npm ci
npm run typecheck
npm test
npm run prepare:runtime
npm run dist:mac:arm64:dir
open "dist/mac-arm64/碧游宫.app"
```

### 3.2 网关

```bash
curl -s -H "Authorization: Bearer $NEWAPI_KEY" \
  http://127.0.0.1:3000/v1/models | python3 -m json.tool
```

### 3.3 本地准圣

```bash
curl -s http://127.0.0.1:4010/v1/health | python3 -m json.tool
```

### 3.4 进程清理

```bash
pgrep -fl "dsh --profile web" || true
lsof -nP -iTCP:39000-39100 -sTCP:LISTEN || true
```

### 3.5 仓库回归

```bash
bash scripts/verify.sh
.venv/bin/python -m pytest zhentu -q
```

---

## 四、评审门

| 门 | 时机 | 通过条件 |
|---|---|---|
| G0 | 本文件评审 | 用户确认方案 B、品牌范围、网关默认地址 |
| G1 | S1 完成 | dsh 启停稳定，无孤儿 |
| G2 | S2 完成 | 两种模式真实对话；密钥不落明文 |
| G3 | S3 完成 | 品牌壳与诊断入口可用 |
| G4 | S4 完成 | macOS `.app` / `.dmg` 可启动 |
| G5 | S5 完成 | CI 三平台产物可下载；文档可复现 |

---

## 五、回滚点

| 失败点 | 回滚动作 |
|---|---|
| dsh 运行时无法在打包 Node 下启动 | 改用 Electron `ELECTRON_RUN_AS_NODE` 或调整 Node 版本；保留开发模式 |
| YAML 注入 provider 不生效 | 回退到「首次启动后由用户在 dsh 设置页添加 provider」；引导页只做连接测试 |
| macOS 打包失败 | 先交付 `npm run dev` 与脚本；打包另开任务 |
| Windows 构建不可用 | V1 先发 macOS；Windows 标记为 CI 待验证 |
| 品牌注入导致 UI 崩溃 | 撤销注入，仅保留窗口标题与图标 |

---

## 六、明确不做

- 不修改 `nodes/`、`zhentu/`、`deploy/`、`monitoring/` 的运行逻辑。
- 不把 Ollama / 准圣服务打进客户端。
- 不实现自动更新、签名、公证。
- 不在本任务中调整 NewAPI 的网络绑定。

---

## 七、执行顺序摘要

```text
S0 spike（已完成）
  → S1 Electron + dsh 启停
  → S2 引导 + 配置 + 密钥
  → S3 品牌 + 菜单 + 日志
  → S4 macOS 打包
  → S5 Windows/CI + 文档
  → 回归 + 报告 + archive
```

---

## 八、细化执行清单（评审补充）

### 8.1 固定版本与目录

- [ ] `client/package.json` 写入：

  ```json
  {
    "biyou": {
      "dshVersion": "0.2.2-rc.5",
      "nodeVersion": "24.22.3"
    },
    "devDependencies": {
      "electron": "44.7.0",
      "electron-builder": "26.17.0",
      "typescript": "5.9.3",
      "@types/node": "^24.0.0"
    },
    "dependencies": {
      "yaml": "^2.8.0"
    }
  }
  ```

- [ ] 运行时目录固定为：

  ```text
  client/build/runtime/
  ├── node/bin/node
  ├── dsh/node_modules/@deepseek-ai/dsh/lib/bin.js
  └── runtime.json
  ```

- [ ] `.gitignore` 增加：`client/node_modules/`、`client/build/`、`client/dist/`、`client/dist-electron/`。

### 8.2 S1 运行时实现细节

- [ ] `electron/runtime.ts` 导出纯函数，便于单测：
  - `parseDshUrl(line: string): string | null`
  - `pickFreePort(start = 39000): Promise<number>`
  - `buildDshArgs(port: number): string[]`
  - `buildDshEnv(secrets, dshHome): NodeJS.ProcessEnv`
- [ ] spawn 参数：

  ```ts
  spawn(nodePath, [dshBin, '--profile', 'web', '--no-open', '--port', String(port)], {
    cwd: runtimeRoot,
    env,
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  ```

- [ ] stdout/stderr 同时写日志文件；解析前先脱敏。
- [ ] `runtime.json` 启动前读取并清理残留。
- [ ] `BIYOU_SMOKE=1` 模式：不创建主窗口，只打印 `SMOKE_OK <url>` 后退出。
- [ ] 单测覆盖 `parseDshUrl` / `pickFreePort` / 脱敏。

### 8.3 S2 配置实现细节

- [ ] `client-config.json` 与 `secrets.json` 按 design 第十七节实现。
- [ ] 引导页「高级设置」默认展开收起，字段：
  - 上下文窗口（默认 32768）
  - 最大输出（默认 4096）
  - 是否支持图片（默认关）
- [ ] `probe.ts` 错误码统一：

  | code | 触发 |
  |---|---|
  | `unauthorized` | 401/403 |
  | `not_found` | 404 |
  | `timeout` | AbortController 10s |
  | `invalid_json` | 响应非 JSON |
  | `empty_models` | `data` 为空 |
  | `unreachable` | 连接失败 |

- [ ] 写入 YAML 后执行：

  ```bash
  <runtime>/node <runtime>/dsh/lib/bin.js --profile web --dump-config
  ```

  - 退出码非 0 或输出不含 provider id → 回滚备份，进入 `failed`。
- [ ] YAML 合并保留其他顶层行与 `llm-pi-ai` 的其他配置键，只改 `providers[providerId]`。
- [ ] 密钥只在 spawn 时注入，测试连接时由主进程持有，不经渲染进程回传。

### 8.4 S3 品牌与日志细节

- [ ] 日志文件 `dsh.log` 轮转：5 MB × 3，写入前统一 `redact()`。
- [ ] 菜单结构：

  ```text
  碧游宫：关于碧游宫 / 重新配置 / 退出
  文件：打开日志目录 / 打开配置目录
  编辑：标准复制粘贴
  视图：重新加载 / 开发者工具（仅开发模式）
  帮助：部署文档 / 问题反馈
  ```

- [ ] `will-navigate` 允许 `127.0.0.1` 与 `localhost`；其他 URL `shell.openExternal`。
- [ ] `setWindowOpenHandler` 返回 `{ action: 'deny' }`。

### 8.5 S4 打包细节

- [ ] `prepare-runtime.mjs` 下载 Node 后校验 `SHASUMS256.txt`。
- [ ] `runtime.json` 记录：

  ```json
  {
    "dshVersion": "0.2.2-rc.5",
    "nodeVersion": "24.22.3",
    "platform": "darwin",
    "arch": "arm64",
    "builtAt": "2026-11-21T..."
  }
  ```

- [ ] `electron-builder.config.cjs` 增加：

  ```js
  directories: { output: 'dist' },
  extraMetadata: { main: 'dist-electron/main.js' },
  ```

- [ ] macOS 构建前设置 `CSC_IDENTITY_AUTO_DISCOVERY=false`。
- [ ] 首次打开 `.app` 若被 Gatekeeper 拦截，README 给出右键打开与 `xattr` 方案。

### 8.6 S5 CI 细节

- [ ] `.github/workflows/client.yml`：

  ```yaml
  name: client
  on:
    workflow_dispatch:
    push:
      paths: ['client/**', '.github/workflows/client.yml']
  jobs:
    build:
      strategy:
        matrix:
          include:
            - os: macos-14
              platform: darwin
              arch: arm64
              script: dist:mac:arm64
            - os: macos-13
              platform: darwin
              arch: x64
              script: dist:mac:x64
            - os: windows-latest
              platform: win32
              arch: x64
              script: dist:win:x64
      runs-on: ${{ matrix.os }}
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-node@v4
          with: { node-version: 24 }
        - run: npm ci
          working-directory: client
        - run: npm run prepare:runtime -- --platform ${{ matrix.platform }} --arch ${{ matrix.arch }}
          working-directory: client
        - run: npm run ${{ matrix.script }}
          working-directory: client
        - uses: actions/upload-artifact@v4
          with:
            name: biyou-client-${{ matrix.os }}
            path: client/dist/**
  ```

- [ ] Windows job 设置 `CSC_IDENTITY_AUTO_DISCOVERY=false`。
- [ ] CI 产物只保留 dmg/zip/exe，不提交到仓库。

### 8.7 跨机网关（可选落地）

- [ ] `deploy/newapi/docker-compose.yml` 的 ports 改为：

  ```yaml
  - "${NEWAPI_BIND:-127.0.0.1}:${NEWAPI_PORT:-3000}:3000"
  ```

- [ ] 根 `docker-compose.yml` 如有 new-api 端口声明，同步使用 `NEWAPI_BIND`。
- [ ] `docs/deployment.md` 增加「局域网访问」：
  - 默认仅本机；
  - 设置 `NEWAPI_BIND=0.0.0.0` + 防火墙；
  - 强密码与独立令牌；
  - 不暴露公网。
- [ ] 本任务只加可选变量与文档，不改默认值。

### 8.8 细化验收矩阵

| 编号 | 场景 | 操作 | 期望 |
|---|---|---|---|
| A1 | 首次启动 | 删除 userData 后启动 | 出现引导页 |
| A2 | 网关成功 | 填 `127.0.0.1:3000/v1` + key | 模型列表非空，进入主界面 |
| A3 | 网关 401 | 填错误 key | 提示令牌无效，不写配置 |
| A4 | 网关不可达 | 填 `http://10.0.0.99:3000/v1` | 提示检查地址/防火墙 |
| A5 | 本地 ready | 准圣 `4010` | 进入主界面并可对话 |
| A6 | 本地 down | 停止准圣后测试 | 提示服务未启动 |
| A7 | 配置校验 | 人为写坏 YAML | 回滚备份并报错 |
| A8 | 退出清理 | 启动后退出 | 无 dsh 进程、端口释放 |
| A9 | 崩溃恢复 | kill dsh 子进程 | 显示错误页，可重启 |
| A10 | 密钥安全 | 搜索 userData | 无 `sk-` 明文 |
| A11 | 打包启动 | 双击 `.app` | 正常启动并可对话 |
| A12 | 未装 Node | 临时移走系统 Node | 仍可启动（使用内置 Node） |

### 8.9 实施顺序（更细）

1. 脚手架 + `runtime.ts` 纯函数 + 单测。
2. Electron 主进程 + `BIYOU_SMOKE` 启动 dsh。
3. `config.ts` + `--dump-config` 校验。
4. `probe.ts` + 引导页 + IPC。
5. 密钥与日志脱敏。
6. 菜单、关于页、导航限制。
7. `prepare-runtime.mjs` + macOS 打包。
8. Windows 脚本 + CI。
9. 文档、验收记录、回归、archive。
