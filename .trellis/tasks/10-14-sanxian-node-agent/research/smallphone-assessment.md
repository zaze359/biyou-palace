# Research · 旧项目 smallphone 勘察结论

> 勘察对象：`/Users/zaze/Documents/smallphone`
> 用途：作为散仙节点智能体的复用基座，判断哪些可复用、哪些必须改造

## 它是什么

端侧离线大模型的**安卓 App**，采用 **CMP（Compose Multiplatform）+ KMP** 架构。README 原文：

> 推理引擎选用 llama.cpp 原生集成（通过 JNI / cinterop 接入，而非 Termux 等沙箱方案）。

也就是说，**smallphone 本身就是为绕开 Termux 而生的**——这与「用 KMP 替代 Termux」的目标天然一致。

## 关键事实

| 项 | 结论 |
|---|---|
| 推理运行时 | **llama.cpp（JNI → `libllama_jni.so`），非 Ollama** |
| 模块结构 | `composeApp`（CMP UI / 安卓入口）→ 依赖 `shared`（KMP commonMain）+ `nativelib`（NDK/CMake） |
| 目标平台 | `androidTarget` + `iosX64/iosArm64/iosSimulatorArm64`。**无 JVM / 桌面 target** |
| iOS 状态 | Task #1 未启用真实接入，仅为占位 actual；需 Xcode |
| 产品形态 | **聊天 App**（`ChatScreen` + `ChatViewModel` MVI 雏形），**不是节点智能体** |
| 已完成的抽象 | `LlmEngine` 接口（expect/actual）、`LlmEngineProvider` 进程级单例、`LlmConfig`、`ChatSession`、`ChatMessage`、`LlmError`、`ModelDownload` |
| 原生层 | `nativelib/src/main/cpp/` 下 vendor 了**整棵 llama.cpp C++ 树**（数百文件）+ `jni_bridge.cpp` |
| 工具链要求 | JDK 17、Android SDK 35 / Build-Tools 35.0.0、NDK 27.x、Gradle 8.10 |

## 对 BiYouPalace 的复用判断

**可复用**
- llama.cpp 的 JNI 桥与 `AndroidLlmEngine`：省掉安卓端侧推理的最大一块工作量。
- `LlmEngine` 统一接口 + `LlmEngineProvider` 单例：正是「共享抽象」的现成范例。
- `ModelDownload` / 模型管理：散仙轻量模型的获取可直接沿用。
- Compose 聊天 UI：恰好就是「自治双模」里的**本地模式**。

**必须改造**
1. **移除 iOS 目标**（BiYouPalace 只需安卓）：删 sourceSet、linkerOpts、`iosMain` 源码。
2. **新增 HTTP 服务层**（Ktor）暴露节点智能体统一契约的四个端点——smallphone 目前是纯本地 UI 直连 engine，**完全不对外提供 HTTP 服务**。
3. **剥离「聊天 App」心智**：聊天降级为本地模式入口，主职责变为「对外提供 OpenAI 兼容推理服务」。
4. **补健康/资源采集**：电量、温度、热状态目前没有，但如上所述属散仙必填字段。

## 重要纠偏

早期曾设想「KMP 一套代码共享 PC + 手机」，**此前提不成立**：
- smallphone **无 JVM/桌面 target**，编译不出 PC 产物；
- 准圣（PC）侧是 Ollama 原生服务端，**不是 Kotlin 程序**。

因此 KMP 在 BiYouPalace 中实际**只覆盖安卓侧**。准圣/散仙的统一不应靠代码共享，而应靠**协议级契约**（`docs/node-agent-contract.md`）——即「用协议共享取代代码共享」。
