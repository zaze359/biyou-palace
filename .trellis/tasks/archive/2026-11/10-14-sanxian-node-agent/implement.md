# Implement · 散仙安卓节点智能体（细化方案 v2）

> 协议基准：`docs/node-agent-contract.md` v1.0 ｜ 验收标准见 `prd.md`
> 上游资产勘察：`research/smallphone-assessment.md`，并已在 2026-10-17 直接通读 smallphone 真实源码
> 参照样板：准圣（Python/Ollama）子任务已**实跑验证**全部契约行为（见 `10-14-quasheng-node-agent` 与 `nodes/quasheng/tests/mock_ollama.py`）

本方案在 v1 草案基础上，把每一项落地到 smallphone 的**真实代码**，并消除与已验证准圣行为的偏差。

---

## 0. 必须对齐的契约行为基线（来自准圣实跑验证）

散仙与准圣共享同一份契约，以下 8 条准圣已逐条跑通，**散仙必须镜像一致**：

| # | 行为 | 散仙落点 |
|---|---|---|
| 1 | 响应头 `X-Node-Id` / `X-Node-Role=sanxian` / `Contract-Version: 1.0` | Ktor 中间件统一加 |
| 2 | `/v1/health` 全字段；`role=sanxian`、`runtime=llama.cpp`、`arch=arm64`、`contract_version=1.0` | NodeServer 拼装 |
| 3 | 散仙专属 `battery_pct` / `temperature_c` / `thermal_state` **真实填充**（准圣恒为 null） | `NodeHealthCollector`（新增） |
| 4 | `/v1/models` 列出本地已加载模型（OpenAI 格式） | 读 `LlmEngineProvider.engine.getModelInfo()` + 能力声明 |
| 5 | `/v1/chat/completions` **SSE 流式**，首块 `delta.role`、末块 `delta:{} finish_reason=stop`、`data:[DONE]` 收尾 | 桥接 `generate(): Flow<String>`，**自产 chunk**（非代理） |
| 6 | `/v1/embeddings` 能力驱动：无能力返回 `501` + `code=capability_not_supported` | `capabilities_map` 判定 |
| 7 | 并发闸门：满载立即 `429` + `Retry-After`（`max_concurrency=1~2`） | 复用准圣 `ConcurrencyGate` 思路 |
| 8 | 鉴权可选 bearer，覆盖全端点（含 health）；**绝不外泄调用方凭据** | Ktor 鉴权插件；散仙无独立后端，但同理不得把 token 带到任何出站调用 |

此外：**客户端断连即调用 `stopGeneration()` 中止生成**（手机发热/耗电敏感，必做）；**本地聊天 UI 走 localhost 同一套端点**（不存在第二套本地推理路径）。

---

## 1. 复用基座 vs 必须改造（已通读源码确认）

### 可复用（直接拿）
- `LlmEngine` 接口（`commonMain`）：`loadModel` / `isModelLoaded` / `generate(session,prompt): Flow<String>` / `stopGeneration` / `getModelInfo` / `close`。**流式用 `Flow<String>`，取消用 `stopGeneration()`——正是 SSE 桥接与断连取消的基础。**
- `AndroidLlmEngine`（`androidMain`）：JNI → `libllama_jni.so`，`generate()` 已用 `Channel<String>` 泵 token、收集方取消时 `finally` 调 `LlamaNative.stopGenerate` + `job.cancel()`。**断连取消链路天然具备。**
- `LlmEngineProvider`：进程级惰性单例；`init()` 由 `SmallPhoneApplication.onCreate` 调用。**这正是 Ktor 服务 + 模型预加载的启动钩子。**
- `LlmConfig`：含 `modelPath / contextSize / threads / gpuLayers / useMmap / kvCacheType / 采样参数` 等。`**模型显式 `loadModel` 才加载**——对应准圣的 `preload_models` 预热。
- `SmallPhoneApplication.onCreate`：应用入口，已在生命周期绑定引擎。
- `ModelDownload` / Compose 聊天 UI：散仙轻量模型获取与本地模式入口直接沿用。

### 必须改造
1. **剥离 iOS**：`shared/build.gradle.kts` 的 `iosX64/iosArm64/iosSimulatorArm64` target、`iosMain` sourceSet、`iosArm64{...linkerOpts(-L.../llama.xcframework...)}` 三段；删 `shared/src/iosMain`、`composeApp/src/iosMain`、`IosLlmEngine` 占位；删 `nativelib/ios` 相关。改后 `shared` 纯安卓编译须验证。
2. **新增 Ktor(CIO) HTTP 服务层**暴露 4 端点（smallphone 当前纯本地 UI 直连 engine，**完全不对外供 HTTP**）。
3. **补健康/资源采集**：电量/温度/热状态当前代码**完全没有**（散仙必填）。
4. **embeddings**：vendored llama.cpp **已原生支持**（`cparams.embeddings` / `llama_get_embeddings_seq` / `pooling_type`）；JNI embed 桥已新增并落地（Plan A 双句柄，见 §2.1，commit `ca35e83`）。
5. **前台服务保活 + 通知**：避免系统杀后台。
6. **本地聊天 UI 改为经 localhost 调本节点**（统一双模路径）。

---

## 2. 关键设计决策

### 2.1 embeddings —— 真实向量化（散仙主承接方），Plan A 已落地
- **证据**：`nativelib/.../llama.h` 含 `cparams.embeddings` / `cparams.pooling_type`、`llama_encode`、`llama_get_embeddings_seq`。**llama.cpp 已支持，无需升级原生库。**
- **已落地方案（Plan A：chat / embedding 双句柄，commit `ca35e83`）**：
  - `LlmConfig` 增 `embeddingModelPath: String?`；`NodeConfig` 增 `embeddingModel`（读取 `sanxian.json` 的 `embedding_model` 字段）。
  - `AndroidLlmEngine` 同时持有 `handle`（chat）与 `embeddingHandle`（embedding），二者独立上下文、KV 互不污染；`loadModel` 新增 `enableEmbeddings` 参数，chat 句柄传 `false`、embedding 句柄传 `true`。
  - `LlamaNative` 增 `external fun embed(handle, text): FloatArray`；`jni_bridge.cpp` 新增同名 JNI：分词 → `llama_batch_get_one` → `llama_encode` → `llama_get_embeddings_seq(ctx, 0)` → `jfloatArray(n_embd)`。
  - `AndroidLlmEngine` 实现既有 `EmbeddingEngine` 接口的 `embed(texts)`；`NodeForegroundService` 预热时把 `embedding_model` 传入，服务层（`NodeServer.handleEmbeddings`）无需改动。
  - pooling 不再强制 MEAN：`pooling_type` 保持 `UNSPECIFIED`，跟随模型元数据（Qwen3-Embedding 用 last-token pooling）；仅当元数据缺失/为 `NONE` 时重建 context 回退 `LAST`，并在 logcat 打 WARN。
  - 健壮性：`embed` 按**实际分词结果**截断到 `llama_n_ubatch()`，避免 `llama_encode` 内 `GGML_ASSERT(n_ubatch >= n_tokens)` 触发 `GGML_ABORT`（字符与 token 非一一对应，故不在 Kotlin 侧按字符长度限制）；embedding 句柄 `nBatch=1024`（chat 句柄仍为 512）。
- **能力边界**：`embedding_model` 未配置/能力未声明 → 501；能力已声明但 embedding 句柄未加载 → 503。
- **风险**：见 §5。

### 2.2 SSE 自产（非代理）
准圣是**代理** Ollama 透传字节；散仙是**桥接** `generate(): Flow<String>` **自己拼 OpenAI chunk**。逐块规则（与准圣 mock 输出完全一致，已验证）：
- 首块：`{"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}`
- 中间块：`{"choices":[{"delta":{"content":<piece>},"finish_reason":null}]}`（`piece` 可能为空串，需保留）
- 末块：`{"choices":[{"delta":{},"finish_reason":"stop"}]}`（**delta 无 content，对应 `delta.content=null` 的客户端保护**）
- 收尾：`data: [DONE]`
- 断连：Ktor 检测客户端断开 → 取消 `Flow` 收集 → `AndroidLlmEngine.generate` 的 `finally` 已调 `stopGeneration()` + `job.cancel()`。

### 2.3 健康采集（散仙专属真实字段）—— `NodeHealthCollector`
- `battery_pct`：`BatteryManager.getIntProperty(BATTERY_PROPERTY_CAPACITY)`（API 21+）。
- `temperature_c`：优先 `BatteryManager BATTERY_PROPERTY_TEMPERATURE / 100.0`（摄氏，多数机型有）；API 29+ 可补 `ThermalStatusListener` / `getThermalHeadroom`。**取不到则 `null` 降级，不得 500。**
- `thermal_state`：`nominal` / `warn` / `hot`：用温度阈值映射，或 API 29+ `PowerManager`/`OnThermalStatusChangedListener` 真实状态。无 API 时按温度阈值推断，仍无则 `null`。
- `resources`：`ActivityManager.getMemoryInfo` 取 `availMem/totalMem`；CPU 采样 `/proc/stat` 或 `Debug`/简单轮询（与原准圣 `psutil` 对应）。
- `status` 判定：**与准圣同源纯函数**（`down > loading > degraded > ready`）。`degraded` 触发：内存≥`mem_warn_pct` 或 CPU≥`cpu_warn_pct` 或温度≥`temp_warn`。`loading`：预加载白名单模型尚未全部 `isModelLoaded()`。`down`：引擎不可用/库未链接。

### 2.4 并发闸门（复用准圣 `ConcurrencyGate` 思路）
- `acquire()` / `release()`；流式用 `acquire()` 在出流前抢占、`release()` 在流结束 `finally` 释放（**不能套 `async with`/use，否则返回响应即释放，429 永不被触发**——准圣已踩坑修复）。
- `max_concurrency` 建议 **1~2**（手机资源受限），config 可调。满载返回 `429` + `Retry-After: 1` + OpenAI 错误体。

### 2.5 鉴权与本地双模
- Ktor 鉴权插件：配 `shared_token` 即开启，覆盖全部端点（含 health）；校验失败 `401`。
- **本地聊天 UI 改为经 `localhost:<port>` 调本节点 `/v1/chat/completions`**——契约要求"不存在第二套本地推理路径"，UI 经 HTTP 调本节点最干净，且天然验证双模统一（集群与本地走同代码路径）。

### 2.6 前台服务保活
- 新增 `NodeForegroundService`（extends `Service`）：`onCreate` 启动 Ktor(CIO) 绑定 `0.0.0.0:<port>`。
- `AndroidManifest` 加 `FOREGROUND_SERVICE` 权限 + `service` 声明 `foregroundServiceType="dataSync"`（API 29+）；常驻通知。
- `SmallPhoneApplication.onCreate`（或显式启动按钮）拉起该前台服务；端口/`shared_token`/能力声明/预加载模型/阈值 **外置为配置**（Assets 或 DataStore）。

---

## 3. 有序实施清单

1. **派生**：`smallphone` → `nodes/sanxian/`，保留 `shared/` `nativelib/` `composeApp/`。
2. **剥离 iOS**：`shared/build.gradle.kts` 删 `ios*` target/`iosMain` sourceSet/`xcframework` linkerOpts；删 `shared/src/iosMain` `composeApp/src/iosMain` `IosLlmEngine`；删 `nativelib/ios`；验证 `shared` 纯安卓编译。
3. **依赖**：`shared` + `composeApp` 加 Ktor server(CIO)、`kotlinx.serialization`（统一进 `libs.versions.toml`）。
4. **embeddings JNI（已落地，commit `ca35e83`）**：`LlmConfig.embeddingModelPath` / `NodeConfig.embeddingModel` / `sanxian.json` 的 `embedding_model` → `AndroidLlmEngine.loadModel(enableEmbeddings)` 双句柄 → `LlamaNative.embed` → `jni_bridge.cpp`（pooling 跟随模型元数据、按分词结果截断到 `llama_n_ubatch`）。
5. **健康采集**：新增 `NodeHealthCollector`（androidMain actual，仅安卓），实现电量/温度/热状态/内存；与准圣同构的 `determineStatus` 纯函数。
6. **NodeServer(Ktor)**：4 端点 + 中间件（响应头/鉴权/429）+ 断连取消；`chat` 桥 `generate→SSE`、`embeddings` 桥 `embed→OpenAI embeddings`、能力缺失 `501`。
7. **NodeForegroundService**：启动 Ktor + 常驻通知；`AndroidManifest` 权限/声明。
8. **启动钩子**：`SmallPhoneApplication.onCreate` 启动前台服务 + 按 `preload_models` 调 `engine.loadModel` 预热（对应准圣 warmup）。
9. **本地聊天 UI**：改为经 `localhost:<port>` 调本节点端点。
10. **配置外置**：端口 / `shared_token` / `capabilities_map` / `preload_models` / `mem_warn_pct` / `cpu_warn_pct` / `temp_warn`。
11. **README**：相对 smallphone 改了什么、启动、冒烟（`adb` + `curl`）、能力声明配置。

---

## 4. 验证（分两层）

- **逻辑层（本环境可跑）**：把 `determineStatus`、能力路由、`SSE` 拼装抽成可 JVM 单测的逻辑（Kotlin/JVM 测试或 Robolectric 轻量）；对齐准圣 `nodes/quasheng/tests` 的测试范式。
- **设备层（需真机/CI）**：`./gradlew :composeApp:assembleDebug` → `adb install` → `curl` 对标准圣逐条冒烟：
  - `GET /v1/health`（真实电量/温度/thermal_state、`role=sanxian`、`runtime=llama.cpp`）
  - `GET /v1/models`
  - `POST /v1/chat/completions` 流式（`curl -N`，看 `[DONE]`）
  - `POST /v1/embeddings`：chat-only 模型 → `501`；embedding 模型 → `200`
  - 并发 `max_concurrency+1` → `429` + `Retry-After`
  - 断连：流式中途断开，日志出现"客户端断开，中止生成"
- **注**：本环境**无法编安卓 APK**（需 JDK17/Android SDK/NDK 且真机），设备层验证须用户在真机/CI 执行；准圣冒烟脚本（`mock_ollama.py`）已验证契约行为，散仙对标准圣逐条对照即可。

---

## 5. 风险（已更新）

- **NDK/llama.cpp 构建是最大变量**（与原评估一致）：先确认 `:nativelib` 能产出 `libllama_jni.so` 再大改。
- **embeddings 双句柄的额外内存开销**（Plan A 已落地）：chat 与 embedding 两份模型上下文同时常驻，embedding 句柄的 KV/计算缓冲约为 chat 同量级；低内存机型需评估 `contextSize`、`nBatch` 与并发上限，避免 OOM 或热降频。
- **embedding 模型文件获取/推送**：`sanxian.json` 默认指向 `/sdcard/biyou/models/Qwen3-Embedding-0.6B-Q8_0.gguf`，仓库不内置 GGUF、无自动下载；需用 `./build_and_run_sanxian.sh --push-model` 手动推送。缺失时节点仍可聊天，但 `/v1/embeddings` 返回 503，且 `/v1/health` 的 `loaded` 暂不反映 embedding 模型（已知偏差，后续单独修）。
- **剥离 iOS 后 `shared` 纯安卓编译**：清理 cinterop/sourceSet 后须完整编译验证。
- **后台被杀**：厂商策略差异大，需真机长跑；保活失效时 health 应体现为不可达，由上层剔除。
- **温度/热状态部分机型无 API** → `null` 降级，不得 500。
- **本环境无法端到端验证 APK**：设备层验证依赖真机/CI。
- **回滚边界**：本子任务只落在 `nodes/sanxian/`，不影响 `deploy/`、`services/`，可整目录回退。
