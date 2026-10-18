# 散仙节点 · 原生 / 构建集成指南

本目录现为**自包含工程**：smallphone 底座已于 2026-10-18 完整并入本目录（`shared/`、`composeApp/`、`nativelib/` 即底座本体；`com.biyou.sanxian` 增量并存）。下面 §2–§7 描述的改动**均已永久应用到仓库文件**，此处保留为「变更记录 + 原生集成细节」与 embeddings JNI 落地参考。

> 前置：JDK 17、Android SDK 35 + Build-Tools 35.0.0、NDK 27.x、Gradle 8.10（同 smallphone）。

---

## 1. 合并状态（已并入仓库，无需手动派生）

> 2026-10-18 起，smallphone 底座已**完整并入 `nodes/sanxian`**（`shared/`、`composeApp/`、`nativelib/` 即底座本体；`com.biyou.sanxian` 增量源码并存，全工程约 34M，含 llama.cpp/ggml 原生源码）。下列 §2–§7 描述的所有改动**均已永久应用到仓库文件**，不再有运行时 patch。本文件保留为「变更记录 + 原生集成细节」，供后续维护与 embeddings JNI 落地参考。

新增源码包名为 `com.biyou.sanxian`（与 smallphone 的 `com.smallphone.llm` 并存，互不冲突）。

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

vendored llama.cpp 已原生支持：`llama_set_embeddings` / `llama_get_embeddings_seq` / `model_params.pooling_type`（见 `nativelib/.../include/llama.h`）。`LlamaNative` 当前**无** embed 方法，需补：

### 4.1 `LlamaNative.kt`（fork 的 `shared/src/androidMain/.../LlamaNative.kt`）追加

```kotlin
/** 开启/关闭上下文 embedding 输出（加载 embedding 模型后调用一次） */
external fun setEmbeddingsMode(handle: Long, on: Boolean)
/** 单条文本向量化，返回 n_embd 维向量 */
external fun embed(handle: Long, text: String): FloatArray
```

### 4.2 `jni_bridge.cpp` 追加（与现有 `Java_com_smallphone_llm_LlamaNative_generate` 同文件）

```cpp
extern "C" JNIEXPORT void JNICALL
Java_com_smallphone_llm_LlamaNative_setEmbeddingsMode(JNIEnv*, jobject, jlong h, jboolean on) {
    if (h) llama_set_embeddings(reinterpret_cast<llama_context*>(h), on);
}

extern "C" JNIEXPORT jfloatArray JNICALL
Java_com_smallphone_llm_LlamaNative_embed(JNIEnv* env, jobject, jlong h, jstring jtext) {
    auto* ctx = reinterpret_cast<llama_context*>(h);
    auto* model = llama_get_model(ctx);
    const auto n_embd = llama_model_n_embd(model);

    const char* txt = env->GetStringUTFChars(jtext, nullptr);
    // 1) tokenize
    const int n_toks = llama_tokenize(model, txt, strlen(txt), nullptr, 0, true, true);
    std::vector<llama_token> toks(n_toks);
    llama_tokenize(model, txt, strlen(txt), toks.data(), n_toks, true, true);
    env->ReleaseStringUTFChars(jtext, txt);

    // 2) build batch + encode
    llama_batch batch = llama_batch_get_one(toks.data(), n_toks, 0, 0);
    if (llama_encode(ctx, batch) != 0) return env->NewFloatArray(0);

    // 3) 取 pooled / seq=0 的 embedding
    const float* emb = llama_get_embeddings_seq(ctx, 0);
    jfloatArray out = env->NewFloatArray(n_embd);
    env->SetFloatArrayRegion(out, 0, n_embd, emb);
    return out;
}
```
> ⚠️ 具体 API 名（`llama_tokenize`、`llama_batch_get_one`、`llama_get_embeddings_seq` 签名）随 vendored llama.cpp 版本而异，编译时按实际头文件校正。embedding 模型加载时须在 `loadModel` 后调一次 `setEmbeddingsMode(handle, true)`（可在 `AndroidLlmEngine.loadModel` 末尾，当 `config.embedMode` 时调用）。

### 4.3 `AndroidLlmEngine.kt`（fork）实现 EmbeddingEngine

```kotlin
class AndroidLlmEngine(...) : LlmEngine, com.biyou.sanxian.node.EmbeddingEngine {
    override fun embed(texts: List<String>): List<List<Float>> {
        if (handle == 0L) throw LlmError.NotLoaded()
        return texts.map { LlamaNative.embed(handle, it).toList() }
    }
}
```
（fork 的 `LlmConfig` 可加 `embedMode: Boolean = false` 以在加载时开启 embedding 模式；或在 `NodeForegroundService` 预热后直接调 `LlamaNative.setEmbeddingsMode`。）

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
（`INTERNET` 权限 smallphone 已有。）

---

## 6. 启动钩子（`SmallPhoneApplication.onCreate`）

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
