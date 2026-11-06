# 散仙节点 · 原生 / 构建集成指南

本目录现为**自包含工程**：散仙 底座已于 2026-10-18 完整并入本目录（`shared/`、`composeApp/`、`nativelib/` 即底座本体；`com.biyou.sanxian` 增量并存）。下面 §2–§7 描述的改动**均已永久应用到仓库文件**，此处保留为「变更记录 + 原生集成细节」与 embeddings JNI 落地参考。

> 前置：JDK 17、Android SDK 35 + Build-Tools 35.0.0、NDK 27.x、Gradle 8.10（同 散仙）。

---

## 1. 合并状态（已并入仓库，无需手动派生）

> 2026-10-18 起，散仙 底座已**完整并入 `nodes/sanxian`**（`shared/`、`composeApp/`、`nativelib/` 即底座本体；`com.biyou.sanxian` 增量源码并存，全工程约 34M，含 llama.cpp/ggml 原生源码）。下列 §2–§7 描述的所有改动**均已永久应用到仓库文件**，不再有运行时 patch。本文件保留为「变更记录 + 原生集成细节」，供后续维护与 embeddings JNI 落地参考。

新增源码包名为 `com.biyou.sanxian`（与 散仙 的 `com.biyou.sanxian.llm` 并存，互不冲突）。

---

## 2. Gradle 依赖（Ktor 服务 + 序列化）

`shared/build.gradle.kts` 的 `commonMain.dependencies` 增加：

```kotlin
implementation("io.ktor:ktor-server-core:2.3.12")
implementation("io.ktor:ktor-server-cio:2.3.12")
implementation("io.ktor:ktor-server-status-pages:2.3.12")
implementation("io.ktor:ktor-server-content-negotiation:2.3.12") // 可选；本实现用手工 JSON
implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
```

`composeApp/build.gradle.kts` 的 `androidMain.dependencies` 增加同样的 Ktor `ktor-server-cio`（CIO 引擎在安卓侧运行需要）。版本号按项目既有 `libs.versions.toml` 对齐。

---

## 3. 剥离 iOS（仅安卓）

`shared/build.gradle.kts` 删除：

```kotlin
iosX64(); iosArm64(); iosSimulatorArm64()
// 以及 iosMain sourceSet 与三段 iosArm64 { binaries.all { linkerOpts(...) } }
```

并删除：`shared/src/iosMain/`、`composeApp/src/iosMain/`、`IosLlmEngine.kt`（占位）、`nativelib/ios/`。改后运行 `./gradlew :shared:compileKotlinAndroid` 验证纯安卓编译。

---

## 4. embeddings JNI 桥（散仙主承接方，**真实向量化**）

vendored llama.cpp 已原生支持：`cparams.embeddings` / `cparams.pooling_type` / `llama_encode` / `llama_get_embeddings_seq`（见 `nativelib/.../include/llama.h`）。**本段描述的内容已落地到仓库（Plan A：独立 embedding 句柄）**，保留为变更记录。

### 4.1 拓扑：Plan A（独立 embedding 句柄）

`AndroidLlmEngine` 同时持有两个 llama 上下文：
- `handle`：chat 模型（`loadModel(enableEmbeddings=false)`），走 `llama_decode` + 采样；
- `embeddingHandle`：独立 embedding 模型（`loadModel(enableEmbeddings=true)`），走 `llama_encode` + `llama_get_embeddings_seq`。

两者 KV 互不污染，并发安全。`NodeForegroundService` 仍以 `engine as? EmbeddingEngine` 把同一引擎实例注入 `NodeServer`，无需改动注入点。embedding 模型路径由 `LlmConfig.embeddingModelPath` / `NodeConfig.embeddingModel` / `sanxian.json` 的 `embedding_model` 字段驱动；为空则节点不承接 embedding（`/v1/embeddings` 落 501/503）。

### 4.2 `LlamaNative.kt` / `jni_bridge.cpp`（已实现）

`loadModel` 新增末参 `enableEmbeddings: Boolean`：为真时在原生侧只设置
`cparams.embeddings = true`，**不强制指定 pooling**。`pooling_type` 保持 `UNSPECIFIED`，
由 llama.cpp 采用模型自带元数据（如 Qwen3-Embedding 的 last-token pooling）；仅当元数据
缺失/为 `NONE`（`llama_get_embeddings_seq` 会返回 NULL）时，重建 context 回退到
`LLAMA_POOLING_TYPE_LAST` 并在 logcat 打印 WARN。embedding 句柄的 `nBatch` 取
`EMBEDDING_N_BATCH = 1024`（chat 句柄仍为 512），兼顾手机内存与长文本截断率。

新增原生 `embed(handle, jtext): jfloatArray`：
`tokenizePrompt` → **按 `llama_n_ubatch(ctx)` 截断过长分词结果并打印 WARN** →
`llama_batch_get_one(toks, n)` → `llama_encode(ctx, batch)` →
`llama_get_embeddings_seq(ctx, 0)` → `jfloatArray(n_embd)`；任一环节失败返回长度 0 数组。

> 截断必须发生在原生侧、且基于**实际分词结果**：`llama_encode` 内部有
> `GGML_ASSERT(n_ubatch >= n_tokens)`（`GGML_ASSERT` 永远生效，触发即 abort 整个进程），
> 而字符数与 token 数并非一一对应，在 Kotlin 层限制字符长度不足以规避该断言。

> ⚠️ 历史草稿曾设想单独的 `setEmbeddingsMode` + 单句柄 `as? EmbeddingEngine`；实际落地改为
> 「`loadModel` 带 `enableEmbeddings` 参数 + 双句柄」，以避免 `llama_encode`/`llama_decode`
> 共享 KV 互相污染。

### 4.3 `AndroidLlmEngine.kt` 实现 EmbeddingEngine

```kotlin
class AndroidLlmEngine(...) : LlmEngine, com.biyou.sanxian.node.EmbeddingEngine {
    override fun embed(texts: List<String>): List<List<Float>> {
        if (embeddingHandle == 0L) throw LlmError.NotLoaded()
        return texts.map { raw ->
            val text = if (raw.isBlank()) " " else raw  // 空串以空格兜底，保维度一致
            LlamaNative.embed(embeddingHandle, text).toList()
        }
    }
}
```
（多并发下 embedding 与 chat 各自独立上下文，互不阻塞 KV；代价是设备上多一份 embedding 模型上下文内存，约与 chat 模型等量。）

---

## 5. AndroidManifest（`composeApp/src/androidMain/AndroidManifest.xml`）

在 `<manifest>` 内、`<application>` 外加前台服务权限；在 `<application>` 内加 `<service>`：

```xml
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" /> <!-- API 34+ -->

<service
    android:name="com.biyou.sanxian.NodeForegroundService"
    android:exported="false"
    android:foregroundServiceType="dataSync" />
```
（`INTERNET` 权限 散仙 已有。）

---

## 6. 启动钩子（`SanXianApplication.onCreate`）

```kotlin
override fun onCreate() {
    super.onCreate()
    appContext = this
    LlmEngineProvider.init()
    com.biyou.sanxian.NodeForegroundService.start(this)   // 拉起节点服务
}
```

---

## 7. 本地模式 UI（走 localhost 同端点）

契约要求「不存在第二套本地推理路径」。把 Compose 聊天 UI 的 `ChatViewModel` 从直连 `LlmEngineProvider.engine.generate` 改为经 `http://127.0.0.1:<port>/v1/chat/completions`（与集群消费完全相同的 HTTP 路径）。实现要点：UI 层用 Ktor 客户端或 `HttpURLConnection` 发 SSE 请求，解析 `data:` 行——以此证明本地/集群双模统一。

---

## 8. 已知约束（V1）

- **单句柄引擎**：`LlmEngine` 同一时刻仅一个模型常驻。`preload_models` 只预热首项；同时服务 chat + embedding 需两模型轮换或同一多能力模型，属后续工作。`/v1/health` 的 `models[]` 会如实反映「仅当前模型 loaded=true」。
- **温度/热状态部分机型无 API** → `null` 降级（不得 500）。
- **设备层验证需在真机/CI**：本环境无法编 APK。
- **回滚边界**：所有改动落在散仙派生工程（或 `nodes/sanxian/`），不影响仓库 `deploy/`、`services/`。

---

## 9. 与准圣对照验收（冒烟）

准圣的 [`nodes/quasheng/tests/mock_ollama.py`](../nodes/quasheng/tests/mock_ollama.py) 已验证契约行为；散仙对标准圣逐条核对：

| 项 | 准圣已验证 | 散仙核对点 |
|---|---|---|
| 响应头 | X-Node-Id / X-Node-Role / Contract-Version | `NodeServer` 发送管道 |
| health 真实电量/温度/thermal_state | —（准圣 null） | `NodeHealthCollector` |
| SSE 块形态（首块 role / 末块 `delta:{}` / `[DONE]`） | ✅ mock 输出 | `Sse.kt` 逐字节对齐 |
| 429 + Retry-After | ✅ | `ConcurrencyGate` |
| 断连取消 | ✅ | `NodeServer` 流式中 `cancel()` |
