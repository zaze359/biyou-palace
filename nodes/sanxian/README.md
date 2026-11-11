# 散仙节点智能体（安卓 App · Kotlin / llama.cpp）

把一台手机变成 **自治双模单元**：本地用 llama.cpp 跑轻量模型，对外暴露与准圣（PC/Ollama）**完全一致**的 OpenAI 兼容服务，同时 App 保留本地聊天能力。

协议基准：[`docs/node-agent-contract.md`](../../docs/node-agent-contract.md) v1.0 ｜ 实施细化：[`../.trellis/tasks/10-14-sanxian-node-agent/implement.md`](../.trellis/tasks/10-14-sanxian-node-agent/implement.md) ｜ 参照样板：准圣（[`../nodes/quasheng`](../nodes/quasheng)）

> **本目录现已自包含**：散仙 底座（KMP 骨架、llama.cpp JNI、`LlmEngine`、`ModelDownload`、Compose 聊天 UI）已于 2026-10-18 **完整并入本目录**（`shared/`、`composeApp/`、`nativelib/` 即底座本体，`com.biyou.sanxian` 增量源码并存），不再依赖外部 散仙 仓库，也无运行时 patch 步骤。本目录下的 `com.biyou.sanxian` 包为散仙节点智能体新增源码；构建集成细节与已落地的补丁见 [`NATIVE_INTEGRATION.md`](./NATIVE_INTEGRATION.md)。

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
| embeddings **真实向量化** | ✅ | 独立 embedding 句柄 + JNI `embed` 桥（`NATIVE_INTEGRATION.md` §4）；`embedding_model` 未配置或加载失败时 503 |
| 并发闸门 429 + Retry-After | ✅ | `ConcurrencyGate` |
| 可选 bearer 鉴权（覆盖全端点） | ✅ | `NodeServer` 鉴权中间件 |
| 客户端断连即 `stopGeneration` | ✅ | Flow 取消 → `AndroidLlmEngine.finally` |
| 本地聊天 UI 走 localhost 同端点 | ✅ | `NodeChatClient` 请求 `/v1/chat/completions`；聊天页不再直连引擎 |
| `/v1/logs` 事件环形缓冲（200 条，脱敏） | ✅ | `LogBuffer` + `NodeApi.handleLogs` |
| 发热/降频联动降载（503 + Retry-After，自动恢复） | ✅ | `ThermalGovernor` + `NodeApi.admitInference` |

## 构建与验证（需真机 / CI）

本工程已自包含，无需先派生 散仙。前置：JDK 17、Android SDK 35 + Build-Tools 35.0.0、NDK 27.x、CMake 3.22.1、adb、ANDROID_HOME。本环境无工具链，无法编 APK，请在本机或 CI 执行：

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

## 发热降载（thermal shedding）

触发信号（任一满足即降载）：

- `PowerManager.currentThermalStatus >= thermal_shed_status`（默认 3 = severe，覆盖系统降频/限频）；
- 电池温度 `>= thermal_shed_c`（默认 48℃）。

降载期间行为：

- 有效并发压到 **1**；**新请求立即返回 503**，OpenAI 错误体：
  `{"error":{"code":"thermal_shedding","type":"server_error","message":"设备温度过高，节点已降载（…），请稍后重试"}}`，
  并带 `Retry-After: 10`（秒，可配）；
- **已在途请求不中断**，正常产出到 `[DONE]`；
- 恢复需带迟滞：温度 `<= thermal_resume_c`（默认 45℃）且热状态回落到触发等级以下；`thermal_resume_c` 配到高于 `thermal_shed_c` 时自动收敛为 `thermal_shed_c - 0.1`，避免抖动；
- 热信号读不到（老系统/设备不支持，`null`）**不会误判过热**，维持正常服务。

`/v1/health` 新增（原字段语义不变）：

| 字段 | 含义 |
|---|---|
| `resources.thermal_shedding` | 当前是否降载 |
| `resources.shed_reason` | 触发原因（如 `thermal_status=3`、`temperature=50.0℃ ≥ 48.0℃`） |
| `limits.effective_max_concurrency` | 当前生效并发上限（降载为 1） |

配置项（assets / DataStore 同风格）：

```json
{
  "thermal_shed_status": 3,
  "thermal_shed_c": 48.0,
  "thermal_resume_c": 45.0,
  "thermal_retry_after_s": 10
}
```

真机验证（用系统热状态注入，必要时还原）：

```bash
adb shell cmd thermalservice override-status 3   # 模拟 severe 降频
curl http://127.0.0.1:4011/v1/health             # thermal_shedding=true, effective_max_concurrency=1
curl -X POST http://127.0.0.1:4011/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"<loaded>","messages":[{"role":"user","content":"hi"}]}'   # 503 + Retry-After + thermal_shedding
adb shell cmd thermalservice reset               # 恢复真实热状态
```

## 后台存活：用户侧约定（Android 12+，实测结论）

节点是前台服务（FGS）常驻，但系统仍会主动停止它。实测过的两类失效：

1. **应用 idle 回收**：熄屏后约 1 分 20 秒出现 `Stopping service due to app idle`，服务被停、端口失联；
2. **后台启动 FGS 被拒**：进程重建或看护任务尝试拉起时，系统打印 `Background start not allowed: service ... startFg?=true`，服务不启动（除开机广播、高优先级 FCM 等豁免场景）。

为此加了看护（[`NodeWatchdog.kt`](./composeApp/src/androidMain/kotlin/com/biyou/sanxian/NodeWatchdog.kt)：WorkManager 周期探活 + 失活通知），但**它只能提醒，不能续命**：

- 周期下限 15 分钟，Doze 下还会被推迟到维护窗口，未加白名单时可能是小时级；
- 后台启动 FGS 本身受限，看护的自愈大概率被系统拒绝；
- 应用若处于 `RESTRICTED` 待机桶 / `RUN_ANY_IN_BACKGROUND: ignore`，WorkManager 作业**根本不会执行**，连提醒都发不出。

### 使用约定（缺一不可）

- **保持 App 在前台，或至少留在「最近任务」里**（不要划掉）；
- **电池设为「不受限制」**：`设置 → 应用 → 散仙 → 电池 → 不受限制`（App 启动时若检测到被限制会弹窗引导）；
- **长期当节点用：插电 + 屏幕常亮**最稳（`设置 → 显示 → 休眠` 设为永不，或开发者选项「不锁定屏幕」）；
- **每次重装 / 升级 APK 后都要重新确认**：实测重装会清掉 doze 白名单条目，后台运行限制也可能回退（`RESTRICTED` + `RUN_ANY_IN_BACKGROUND: ignore`）。

### 稳定恢复路径

系统停掉服务后，稳定恢复只有两条：

- **打开 App**：`MainActivity.onStart` 每次回前台都会重新拉起服务；
- **重启设备**：`BootReceiver` 接 `BOOT_COMPLETED` 自动启动。

看护通知只负责「告诉你节点掉了」，不要指望它自动恢复。

### 现场核对命令

```bash
adb shell dumpsys deviceidle whitelist | grep sanxian     # 应有 user,com.biyou.sanxian,<uid>
adb shell cmd appops get com.biyou.sanxian RUN_ANY_IN_BACKGROUND   # 应为 allow / default
adb shell am get-standby-bucket com.biyou.sanxian         # 10=ACTIVE，30=RESTRICTED（受限）
adb shell "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4011/v1/health"
```

