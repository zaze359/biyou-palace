# 碧游宫 V2 · 本地模型插件（dsh-local-models）

> 父任务：`11-21-desktop-client`
> 定位：独立 dsh 插件，为桌面客户端提供「下载运行时 → 下载模型 → 本地推理 → 注册为 dsh provider」的完整闭环。

## Goal

客户端首次启动时没有本地运行时和模型；用户可在插件设置页安装 llama.cpp 运行时、下载或导入 GGUF，启动托管的 `llama-server`，并把本地模型注册为 dsh 的一等模型 provider。模型下载完成后可完全离线对话与调用工具。

## 背景与约束

- 单机执行层是 dsh；本地模型只需成为 dsh 的 LLM provider，不重写 agent loop。
- 现有第三方插件无法同时满足：macOS x64、Windows x64、托管运行时、模型下载、provider 注册。
- 运行时和模型都不进安装包，按需下载；这是 Hermes Agent 和现有 dsh 插件验证过的形态。
- 参考实现：Hermes Agent 的 `local_runtime`，以及 `dsh-open-model` 的 `ctx.llm.registerAdapter()` / `ctx.webServer.register()` 接法。

## Scope

- 新插件包 `@biyou/dsh-local-models`，包含 Host half 与 Browser half，可独立启停。
- 运行时安装：按平台/架构选择 llama.cpp 构建，下载、SHA-256 校验、解压、激活。
- 模型目录：首版 2–3 个通过工具调用验证的 GGUF 模型。
- 模型下载：支持 Hugging Face / 镜像、分片 GGUF、暂停/续传/取消、完整性检查。
- 模型导入：支持选择本地 `.gguf` 文件（链接或复制）。
- 服务托管：启动/停止 `llama-server`，稳定回环端口、API key、真实生成就绪、崩溃重启、空闲卸载、退出清理、孤儿接管。
- Provider 注册：`ctx.llm.registerAdapter()` 注册 `biyou-local`，模型出现在 dsh 模型选择器。
- 设置页：`设置 → 插件 → 本地模型`，含 Runtime / Models / Status 三个区。
- 外部服务兼容：探测已有 `llama-server` / Ollama，允许不装内置运行时也能用。

## Requirements

1. **独立插件**：包名 `@biyou/dsh-local-models`，`main` 为 Host half，`exports["./client"]` 为 Browser half，`dsh.client.platform = "web"`。
2. **运行时清单**：仓库内维护 `runtime/manifest.json`，固定 llama.cpp 版本、平台/架构、下载 URL、SHA-256、解压后二进制相对路径；不运行时抓取 latest。
3. **平台覆盖**：首版支持 macOS x64（Metal 或 CPU）与 Windows x64（CPU / Vulkan / CUDA）；不支持的组合在安装前明确拒绝。
4. **运行时安装**：下载到 `cache/partials`，支持 Range 续传；校验失败不得激活；解压与激活分阶段；安装过程可暂停、恢复、取消。
5. **模型目录**：`runtime/catalog.json` 至少包含模型 id、显示名、repo、量化档位、文件列表与大小、revision、训练上下文、许可证、工具调用标记。
6. **硬件适配**：下载前估算内存/显存占用，标记「可全量上 GPU / 回退系统内存 / 不可运行」；低于 4-bit 不提供；推荐模型需预留至少 64K 上下文。
7. **模型下载**：分片 GGUF 必须全部分片齐全才可服务；未完成文件不发布；支持断点续传与校验。
8. **模型导入**：已有 `.gguf` 可链接进模型库，不强制复制；导入后读取头部元数据（架构、量化、层数、训练上下文）。
9. **服务托管**：一个插件实例只托管一个 `llama-server` router；启动端口默认使用稳定非 8080 端口，冲突时回退随机端口并写入状态文件。
10. **鉴权**：`llama-server` 启用随机 API key，key 持久化在用户数据目录，权限 0600；不写入日志、不进入前端。
11. **就绪判定**：健康检查 200 不等于就绪；必须完成一次真实生成（固定短提示与期望输出）后才标记 ready。
12. **生命周期**：崩溃按 1/5/15/60 秒退避重启；空闲 15 分钟卸载模型；退出/热重载清理所有本插件启动的子进程；异常退出后的孤儿进程下次启动可接管或替换。
13. **Provider 注册**：Host half 通过 `ctx.llm.registerAdapter(['biyou-local'], adapter)` 注册；适配器把 dsh 请求转换为 OpenAI chat/completions，支持文本、reasoning、tool calls、usage、finish 与 SSE 流。
14. **默认模型**：用户点击「使用」后，将所选模型设为新会话默认；不改变现有云端 provider，可一键切回。
15. **设置页**：Browser half 注册 `settings.plugins.tab`，id `biyou.local-models`；页面通过同源 JSON bridge 调用 Host，不直接访问 Node/文件系统。
16. **Bridge 安全**：仅接受同源浏览器请求；方法白名单；请求体大小上限；不暴露任意服务调用。
17. **持久化**：运行时、模型、状态、日志全部位于 `userData/local-models/`；写入使用临时文件 + rename；不写仓库目录。
18. **错误提示**：无网络、磁盘不足、校验失败、GPU 不支持、端口冲突、模型缺分片、服务启动失败均给出可操作提示。
19. **兼容性**：探测本机已有 `llama-server`（`/props` 指纹）与 Ollama（`/api/tags`）；内置服务优先，外部服务可选。
20. **测试**：单元测试覆盖清单解析、校验、分片完整性、状态机、端口/key 持久化；集成测试覆盖安装→下载→启动→对话→退出清理。

## Acceptance Criteria

- [ ] 干净环境、未安装 Ollama/llama-server，可在插件内安装运行时并下载一个模型。（运行时安装已真机验证；目录模型下载暂由单测覆盖，尚未真机下完 5GB 模型）
- [x] 下载完成后断开网络，仍可完成一次本地对话和一次工具调用。
- [x] 运行时或模型下载支持暂停、恢复、取消；校验失败被拒绝且不激活。
- [x] 分片 GGUF 缺任一分片时不出现在可服务模型列表。
- [x] 本地模型出现在 dsh 模型选择器，选中后正常流式输出与工具调用。
- [x] 杀掉 `llama-server` 后，插件能按退避重启或提示用户恢复。
- [x] 空闲超时后模型被卸载，下一条消息可自动重载。
- [x] 退出客户端后无 `llama-server` / dsh 残留进程。（macOS x64 已验证；Windows 待真机）
- [ ] macOS x64 与 Windows x64 至少各有一个可用的运行时构建路径。（macOS x64 已真机；Windows CPU/Vulkan 安装包 URL、SHA-256、二进制名已核对，未在 Windows 真机执行）
- [ ] 设置页可查看运行时、模型、下载进度、服务状态与最近日志。（browser half 已进 boot；图形验收待补）
- [x] 现有桌面端 `npm run smoke` 仍为 `ok=true`。
- [x] 日志与前端不出现 API key、模型路径中的敏感信息。

## Dependencies

- `11-21-desktop-client`（D2 插件骨架与 profile 链接方式）
- dsh `0.2.2-rc.5`（`ctx.llm`、`ctx.webServer`、`ctx.settings`）
- llama.cpp 官方发布构建与 GGUF 模型
- `research/hermes-and-dsh-plugins.md`

## Out of Scope

- Linux、Android、iOS。
- 模型训练、微调、量化转换。
- 多 GPU、集群推理。
- 全量 Hugging Face 搜索与模型市场（首版只做精选目录）。
- 运行时/模型自动更新（首版固定版本，手动更新）。
- 在安装包内预置模型。

## Open Decisions

1. 首版模型目录具体型号与量化档位。
2. macOS x64 优先 Metal 还是 CPU 构建；是否同时支持 arm64。
3. 是否额外注册 `model_*` 工具给 agent（建议首版只做 UI + provider）。
4. 默认空闲卸载时长是否采用 15 分钟。
5. 外部 Ollama 是仅探测提示，还是也注册为 provider 路由。
