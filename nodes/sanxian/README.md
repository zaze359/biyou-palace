# 散仙节点智能体（安卓 App · Kotlin / llama.cpp）

把一台手机变成 **自治双模单元**：本地用 llama.cpp 跑轻量模型，对外暴露与准圣（PC/Ollama）**完全一致**的 OpenAI 兼容服务，同时 App 保留本地聊天能力。

协议基准：[`docs/node-agent-contract.md`](../../docs/node-agent-contract.md) v1.0 ｜ 实施细化：[`../.trellis/tasks/10-14-sanxian-node-agent/implement.md`](../.trellis/tasks/10-14-sanxian-node-agent/implement.md) ｜ 参照样板：准圣（[`../nodes/quasheng`](../nodes/quasheng)）

> **本目录现已自包含**：smallphone 底座（KMP 骨架、llama.cpp JNI、`LlmEngine`、`ModelDownload`、Compose 聊天 UI）已于 2026-10-18 **完整并入本目录**（`shared/`、`composeApp/`、`nativelib/` 即底座本体，`com.biyou.sanxian` 增量源码并存），不再依赖外部 smallphone 仓库，也无运行时 patch 步骤。本目录下的 `com.biyou.sanxian` 包为散仙节点智能体新增源码；构建集成细节与已落地的补丁见 [`NATIVE_INTEGRATION.md`](./NATIVE_INTEGRATION.md)。

## 本目录结构

```
nodes/sanxian/
├── shared/src/commonMain/kotlin/com/biyou/sanxian/   # 跨平台纯逻辑（与准圣逐条对齐）
│   ├── contract/Models.kt        # 契约数据模型（health/models/chat/embeddings/error）
│   └── node/
│       ├── Contract.kt          # 契约身份常量（role/runtime/响应头名）
│       ├── NodeConfig.kt        # 配置 + JSON/Map 装载
│       ├── NodeStatus.kt        # status 四态纯函数（与准圣同源）
│       ├── Capabilities.kt      # （能力路由逻辑内联在 NodeConfig.capabilityFor）
│       ├── Sse.kt               # OpenAI SSE 块拼装（手工，逐字节对齐准圣）
│       ├── ConcurrencyGate.kt   # 并发闸门（满载立即 429）
│       ├── Errors.kt            # OpenAI 风格错误
│       └── EmbeddingEngine.kt   # 向量化后端抽象（散仙主承接方）
├── shared/src/androidMain/kotlin/com/biyou/sanxian/node/
│   ├── NodeHealthCollector.kt    # 真实电量/温度/热状态/内存/CPU（散仙专属必填）
│   └── NodeServer.kt            # Ktor/CIO 服务：4 端点 + 中间件 + 429 + 断连取消
├── composeApp/src/androidMain/kotlin/com/biyou/sanxian/
│   └── NodeForegroundService.kt  # 前台服务保活 + 启动 Ktor + 模型预热
└── composeApp/src/androidMain/assets/sanxian.json     # 示例配置
```

## 已实现 / 待接（相对准圣已验证的契约行为）

| 契约行为 | 状态 | 落点 |
|---|---|---|
| 响应头 X-Node-Id / X-Node-Role=sanxian / Contract-Version | ✅ | `NodeServer` 发送管道 |
| `/v1/health` 全字段（含散仙专属真实电量/温度/thermal_state） | ✅ | `NodeHealthCollector` + `buildHealth` |
| `/v1/models` OpenAI 列表 | ✅ | `buildModels` |
| `/v1/chat/completions` SSE 流式（自产 chunk，`[DONE]` 收尾） | ✅ | `NodeServer.handleChat` 桥 `LlmEngine.generate(): Flow<String>` |
| `/v1/embeddings` 能力驱动（无能力 501） | ✅ 路由 | `NodeServer.handleEmbeddings` |
| embeddings **真实向量化** | ⚠️ 需 JNI 桥 | 见 `NATIVE_INTEGRATION.md`（llama.cpp 已原生支持） |
| 并发闸门 429 + Retry-After | ✅ | `ConcurrencyGate` |
| 可选 bearer 鉴权（覆盖全端点） | ✅ | `NodeServer` 鉴权中间件 |
| 客户端断连即 `stopGeneration` | ✅ | Flow 取消 → `AndroidLlmEngine.finally` |
| 本地聊天 UI 走 localhost 同端点 | ⚠️ 待改 | 见 `NATIVE_INTEGRATION.md` 本地模式一节 |

## 构建与验证（需真机 / CI）

本工程已自包含，无需先派生 smallphone。前置：JDK 17、Android SDK 35 + Build-Tools 35.0.0、NDK 27.x、CMake 3.22.1、adb、ANDROID_HOME。本环境无工具链，无法编 APK，请在本机或 CI 执行：

```bash
export JAVA_HOME=$(brew --prefix openjdk@17)
export ANDROID_HOME=$HOME/Library/Android/sdk

# 一键：补 local.properties → 编译 → 安装 → 拉起节点 → 探活
./build_and_run_sanxian.sh

# 或只编译 APK（不装不启）：
./build_and_run_sanxian.sh --no-install

# 模型需自行 push（默认预热 Qwen3-0.6B-Q8_0.gguf）：
./build_and_run_sanxian.sh --push-model ~/Qwen3-0.6B-Q8_0.gguf
```

装好并拉起后（App 前台服务自动启动 Ktor），手机与电脑同局域网取手机 IP，或用 `adb forward tcp:4011 tcp:4011` 后走 localhost：

```bash
curl http://127.0.0.1:4011/v1/health
curl http://127.0.0.1:4011/v1/models
curl -N -X POST http://127.0.0.1:4011/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<loaded>","messages":[{"role":"user","content":"你好"}],"stream":true}'
# 并发：发起 max_concurrency+1 个请求 → 429 + Retry-After
# 断连：流式中途断开，日志应出现引擎停止生成
```

准圣的 `mock_ollama` 冒烟脚本已验证契约行为，散仙应对标准圣**逐条对照**。
