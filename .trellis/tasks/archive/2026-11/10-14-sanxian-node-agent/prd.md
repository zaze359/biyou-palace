# PRD · 散仙安卓节点智能体（smallphone 改造 KMP + llama.cpp）

> 父任务：`10-13-biyou-v1-blueprint`（BiYouPalace V1.0 主规划）· 第一步子任务
> 协议基准：`docs/node-agent-contract.md` v1.0
> 上游资产：`/Users/zaze/Documents/smallphone`（勘察结论见 `research/smallphone-assessment.md`）

## 一、目标与价值

把手机变成一个**独立可用的自治双模单元**：在本地用 llama.cpp 跑轻量模型（Embedding / OCR / 短上下文对话），对外暴露与准圣完全一致的 OpenAI 兼容服务，同时 App 仍保留原有的本地聊天能力。

核心价值是**复用已有资产**——旧项目 smallphone 已具备 KMP 骨架、llama.cpp JNI 集成、模型下载与聊天 UI，省掉从零做安卓端侧推理的大量工作。

## 二、范围与要求

须实现 `docs/node-agent-contract.md` v1.0 的全部端点与切面（与准圣同一份契约）：

- `GET /v1/health`：`node`（`role=sanxian`、`runtime=llama.cpp`、`arch=arm64`）；**散仙专属字段必须真实填充**——`battery_pct`、`temperature_c`、`thermal_state`（`nominal`/`warn`/`hot`）；`resources` 取 CPU/内存；`models[].capabilities` 声明所承接能力（embedding / ocr / chat 短上下文）；`limits.max_concurrency` 建议 1~2。
- `GET /v1/models`
- `POST /v1/chat/completions`：**必须支持 SSE 流式**（桥接 `AndroidLlmEngine` 的流式 `Flow<String>`）。
- `POST /v1/embeddings`：散仙为主要承接方；无能力时返回 `501`。
- 通用切面：`X-Node-Id` / `X-Node-Role=sanxian` / `Contract-Version: 1.0` 响应头；OpenAI 风格错误；可选 bearer 鉴权；**客户端断连即中止生成**（直接影响发热与耗电，必须做）。
- **本地模式**：保留 Compose 聊天 UI 作为本地入口，且它通过 `localhost` 调用同一套端点——不存在第二套本地推理路径。

### smallphone 改造要点
1. **移除 iOS 目标**：删 `iosX64/iosArm64/iosSimulatorArm64` 及其 sourceSet、xcframework linkerOpts 与 `iosMain` 源码（BiYouPalace 只需安卓）。
2. **保留聊天 UI** 作为本地自用模式。
3. **新增 Ktor HTTP 服务**暴露上述四个端点；桥接既有 `LlmEngine` / `AndroidLlmEngine`。
4. 前台服务保活 + 通知（避免系统杀后台），可选开机自启。

## 三、验收标准

1. 四个端点行为与契约 v1.0 **逐条一致**（字段、状态码、错误格式）。
2. `/v1/chat/completions` SSE 流式可逐 chunk 输出并以 `data: [DONE]` 结束。
3. `/v1/health` 返回**真实**电量、温度与 `thermal_state`；`role=sanxian`、`runtime=llama.cpp`、`contract_version=1.0`。
4. 不具备的能力返回 `501` 且 `code=capability_not_supported`。
5. 响应头 `X-Node-Id`、`X-Node-Role`、`Contract-Version` 齐备。
6. **App 本地聊天仍可用**，且确认它走的是 localhost 同一套端点（非另一套本地推理路径）。
7. 请求中途断开：服务端确认中止生成、资源释放。
8. App 退到后台后服务仍可被访问（前台服务保活生效）。

## 四、非目标（第一步不做）

- 不含节点注册到碧游宫 / 拉取子任务（第二步）。
- 不含 Prometheus 指标格式（第四步灵脉 scrape `/v1/health`）。
- 不含 iOS 支持（已明确剥离）。
- 不含集群级重试 / 降级（第三步阵图）。

## 五、依赖与顺序

**建议顺序：先完成准圣子任务**（`10-14-quasheng-node-agent`）作为契约参照样板，再开工本任务，以避免契约理解偏差导致的返工。

## 六、风险

- llama.cpp NDK/JNI 构建链较重，对 JDK 17 / Android SDK 35 / NDK 27 版本敏感。
- 手机内存与发热：低并发模型仅 1~2 并发；高温时必须由 `health` 反映 `degraded`/`hot` 供上层降权。
- 安卓后台限制：前台服务 + 通知是必需的保活手段，仍需真机验证各厂商策略。
- 剥离 iOS 后需确认 shared 模块仍能编译（cinterop / sourceSet 依赖清理干净）。
