# 实施 · 本地模型插件（dsh-local-models）

## 前置

- 父任务 `11-21-desktop-client` 的 D2 已完成：插件链接、`settings.plugins.tab` 可注册。
- dsh `0.2.2-rc.5`，Node 24.22.3，Electron 44.7.0。
- 先确认本机可用模型与工具调用能力，再写目录。

## 阶段

### S1 · 插件骨架

- 新建 `desktop/packages/biyou-local-models/`，`package.json` 声明 Host/Browser 两半。
- Host `index.js`：空 apply + `inject`，先只打日志。
- Browser `client.js`：注册 `settings.plugins.tab`，渲染占位页。
- `scripts/link-plugin.mjs` 支持多包链接；`cordis.patch.yml` 插入 `ui-biyou-local-models`。
- 验收：dsh 启动后设置页出现「本地模型」占位。

### S2 · 运行时管理

- `platform.js`：平台/架构/后端选择。
- `manifest.json`：macOS x64、Windows x64 各一条固定构建。
- `downloader.js`：Range、partials、暂停/恢复/取消、SHA-256。
- `runtime.js`：安装、校验、解压、激活、发现。
- 验收：干净目录安装出可执行的 `llama-server`；校验失败不激活；不支持平台明确拒绝。

### S3 · 模型目录与下载

- `catalog.json`：首版 2–3 个工具调用验证过的 GGUF。
- `gguf.js`：头部解析。
- `models.js`：下载计划、分片完整性、导入本地文件、删除。
- `state.js`：原子写 `registry.json` / `downloads.json`。
- 验收：分片缺一不可服务；暂停后可续传；导入不复制原文件。

### S4 · Supervisor

- `supervisor.js`：端口、key、spawn、真实生成就绪、退避重启、空闲卸载、退出清理、孤儿接管。
- `server.json` 持久化；日志写 `logs/llama-server.log`。
- 验收：真实启动一个模型；杀进程后恢复；退出无残留。

### S5 · Provider 接入

- `provider.js`：`ctx.llm.registerAdapter(['biyou-local'], adapter)`。
- messages/tools 转换、SSE 解析、usage、错误映射。
- 模型按需加载；空闲时不误杀在途请求。
- 验收：模型出现在选择器；一次对话 + 一次工具调用成功。

### S6 · 设置页与 Bridge

- `bridge.js`：`/api/biyou-local/status`、`/action`，同源校验 + 白名单。
- Browser 页面：Runtime / Models / Status 三区；进度轮询；错误与日志展示。
- 验收：安装、下载、启动、使用、停止全部可从页面完成。

### S7 · 加固与验证

- 错误路径：无网络、磁盘不足、端口冲突、GPU 不支持、缺分片、key 文件损坏。
- 单元测试 + 假服务集成测试。
- macOS x64 与 Windows x64 各跑一次真实模型。
- 回归 `npm run smoke`；退出后无 llama-server。
- 更新桌面端 README 与任务进度。

## 文件清单（预计）

```text
desktop/packages/biyou-local-models/
├── package.json
├── lib/index.js
├── lib/client.js
├── lib/runtime/{manifest,catalog}.json
├── lib/runtime/{platform,downloader,runtime,models,gguf,supervisor,provider,bridge,state}.js
└── test/
```

## 验证命令

```bash
cd desktop
npm run check
npm run smoke
node packages/biyou-local-models/test/unit/run.mjs
node packages/biyou-local-models/test/integration/run.mjs
```

手工：

1. 清空 `userData/local-models/`。
2. 启动客户端 → 设置 → 插件 → 本地模型。
3. 安装运行时 → 下载模型 → 使用。
4. 断网后对话 + 工具调用。
5. 杀掉 llama-server → 观察恢复。
6. 退出客户端 → `ps aux | grep llama-server`。

## 风险

| 风险 | 处理 |
|---|---|
| llama.cpp 构建与 macOS x64/Windows 后端不匹配 | 清单只放实测通过的 target；不支持即拒绝 |
| 模型下载体积大、网络不稳 | partials + Range + 暂停/恢复；默认镜像可配 |
| 就绪判定误判 | 真实生成探针，不信 `/health` 200 |
| 退出残留 | Cordis effect 统一停服；启动时接管孤儿 |
| provider 流式/工具调用不兼容 | 用真实模型做集成测试，错误带模型 id |
| 插件升级破坏 dsh 0.2.2-rc.5 | 固定版本，升级单独立项 |
| 第三方模型许可证 | 目录记录 license，UI 展示来源 |

## 进度

- [x] S1 插件骨架 · `@biyou/dsh-local-models` 两半、link-plugin 多包、profile 行、boot 载荷可见
- [x] S2 运行时管理 · b12620 清单（macOS x64/arm64、Win x64 CPU/Vulkan/CUDA）、下载/校验/解压/激活；macOS 真实下载安装通过
- [x] S3 模型目录与下载 · Qwen3 8B/4B/1.7B Q4_K_M、GGUF 头、分片完整性、导入链接、registry、preset；下载器用本地 Range 服务验证
- [x] S4 Supervisor · router 模式、preset、按需加载、真实生成探针、空闲卸载、退避重启、退出清理、孤儿接管；假服务与真实 llama-server 均通过
- [x] S5 Provider 接入 · `biyou-local`、消息/工具转换、SSE text/reasoning/tool/usage；真实 Qwen2.5 0.5B 文本与工具调用通过
- [x] S6 设置页与 Bridge · `/api/biyou-local/{status,action}` 同源+白名单+256KB；页面进入 boot 载荷；`model.use` 经 bridge 跑通真实服务
- [x] S7 加固与验证 · 单元 12/12、集成 1/1、`smoke:dsh` 通过、真实链路手工通过；Windows 真机与页面截图待补

复核（2026-11-22）：重新跑单测 12/12、集成 1/1、`npm run check`、`npm run smoke:dsh`、`npm run smoke`（`ok=true`，`localModelsOk=true`）均通过；复核时修复设置页「启动服务 / 停止服务」只返回状态、未真正启停的问题。

## 完成定义

- [x] macOS x64 可用路径（b12620 Metal，真实安装+真实模型验证）
- [ ] Windows x64 真机路径（CPU/Vulkan/CUDA target 已入清单，未真机跑）
- [x] 首启无运行时/模型 → 插件内安装运行时/导入模型 → 真实对话 + 工具调用
- [x] 退出无残留（端口探活 000；`smoke:dsh` 进程组无残留）
- [x] 文档记录清单、目录、状态文件与错误码（`packages/biyou-local-models/README.md`）

## 已验证记录（2026-11-22）

- 单元：`node packages/biyou-local-models/test/unit/run.mjs` → 12/12。
- 集成：`node packages/biyou-local-models/test/integration/run.mjs` → 1/1（假 runtime/假 router）。
- 真实：macOS x64 + llama.cpp b12620 + Qwen2.5 0.5B（Ollama blob 转 `.gguf` 链接）
  跑通 `/models/load`、真实生成、tool_calls、空闲卸载、停止清理。
- bridge：dsh profile 启动后 `GET /api/biyou-local/status` 200；`runtime.install`、
  `model.import`、`model.use` 经 bridge 完成，服务进入 ready。
- provider：`/api/llm/listProviders` 返回 `biyou-local`（碧游宫 · 本地模型）。
- 退出清理：用桌面端 `DshServer.stop()` 走真实 SIGTERM 流程，router 在 1 秒内退出，
  `18434` 无监听；插件 cleanup 改为可等待的 async disposer。
- `BIYOU_NO_SANDBOX=1 BIYOU_USER_DATA=... npm run smoke` → `ok=true`、`biyouOk=true`、`localModelsOk=true`；设置页图形内容未做自动化点选。
- 未做：Windows 真机；设置页截图人工验收。
