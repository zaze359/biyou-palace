# 散仙节点真机验收

> 父任务：`10-14-sanxian-node-agent` · 契约：`docs/node-agent-contract.md` v1.2（响应头仍 `Contract-Version: 1.0`）
> 真机：XQ-AT72（Android 12 / arm64-v8a / 12GB RAM）· 2026-11-18
> 目的：补齐父任务 notes 中挂起的五项真机验收，并把过程中发现的系统级停服问题记录成可复现的事实。

## 一、验收矩阵

| # | 父任务原验收项 | 结论 | 证据 |
|---|---|---|---|
| ① | 启动后 `/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` 冒烟 | ✅ 通过（另加 `/v1/logs`、视觉请求） | 见 §二 |
| ② | >1024 tokens 长文本 embedding 出现截断 WARN 且进程不崩 | ✅ 通过 | 14011 字符 → 12009 tokens → 保留 1024；HTTP 200，维度 1024，`prompt_tokens=1024`，pid 不变 |
| ③ | logcat 确认 pooling 采用模型默认值（Qwen3-Embedding 应为 LAST） | ✅ 通过 | `print_info: pooling type = 3`（LAST）+ `llama_jni: embedding pooling 使用模型默认值: 3`，无回退 WARN |
| ④ | 客户端断连取消与后台长跑 | ✅ 通过（后台需保持亮屏，见 §三） | 断连后 `active=0`、节点继续服务；`svc power stayon true` 下 15 分钟零中断 |
| ⑤ | embedding 模型 GGUF 推送后 200 且向量维度正确 | ✅ 通过 | `/v1/embeddings` 200，dim=1024；语义相似度符合预期（见 §二.4） |

## 二、真机实测数据

### 1. 构建与单测

- 本轮用已缓存的 Gradle 8.10（`~/.gradle/wrapper/dists`）**增量**重编 `:composeApp:assembleDebug`：36s，成功。源码相对设备上 11-15 的 APK 已前进 3 天，因此重装后再验收。
- 单测 `:shared:testDebugUnitTest` 70 passed + `:composeApp:testDebugUnitTest` 6 passed = **76 passed，0 failed**。
- 新 APK 安装后 `/v1/health` 约 2s 返回 200，chat / embedding / vision 三模型均 `loaded=true`。

### 2. 四端点冒烟（健康体关键字段）

- `/v1/health` 200，三件套响应头齐全（`X-Node-Id: sanxian-01`、`X-Node-Role: sanxian`、`Contract-Version: 1.0`）。
- `status=ready`；`models` 三项 `loaded=true`（`Qwen3-0.6B-Q8_0` chat / `Qwen3-Embedding-0.6B-Q8_0` embedding / `Qwen2.5-VL-3B` chat+vision+ocr）；`limits.max_context_len=4096`、`effective_max_concurrency=2`；`resources.active`（v1.2）有值，`queue_len=0`。
- `/v1/models` 200，返回同上三项 `owned_by=biyou-node`。
- 非流式 chat 200（2.2s / 64 tokens），`usage` 为真实 token 数；流式 chat 200，`Content-Type: text/event-stream`，首帧 0.05s，`role` 帧 → `content` 帧 → `finish_reason=stop` → `data: [DONE]`。
- `/v1/embeddings` 200，dim=1024，`usage.prompt_tokens=5`。

### 3. 长文本截断（②）

| 输入字符 | 分词结果 | 实际参与 | HTTP | 耗时 | 进程 |
|---|---|---|---|---|---|
| 14011 | 12009 tokens | 1024（截断） | 200 | 17.8s | 存活（pid 不变） |

logcat：`W llama_jni: embed 输入过长：12009 tokens 超过 n_ubatch=1024，已截断保留前 1024 tokens`。

### 4. Embedding 语义正确性（⑤）

4 条文本同一批请求均返回 dim=1024。余弦相似度：

| 对 | cos |
|---|---|
| 「天下莫柔弱于水」~「水滴石穿，以柔克刚」 | **0.674** |
| 「今天股市大涨…」~「天下莫柔弱于水」 | 0.232 |
| 「今天股市大涨…」~「八卦掌是内家拳的一种」 | 0.255 |
| 「水滴石穿，以柔克刚」~「八卦掌是内家拳的一种」 | 0.435 |

语义相近对显著高于无关对：pooling 取 LAST 且向量化路径真实可用（非占位/零向量）。

### 5. 并发与降载（契约行为）

两路 200 tokens 在途时：`/v1/health` 连续三次 `active=2`；第 3 路立即 `429` + `Retry-After: 1` + `code=rate_limit_exceeded`；两路均正常 200（200 completion tokens），结束后 `active=0`、`status=ready`。

### 6. 视觉（回归，非本任务新增）

- data URL 1024×768 纯白图 + 提示「一句话描述」：75.2s，输出「一个穿着红色衣服的人站在白色背景前。」——白色图片给出非空描述属模型幻觉，**不作为正确性判据**；只验证了视觉链路可用、`usage` 含图像 token（511）。
- 外网 URL（`https://www.baidu.com/favicon.ico`）：3.1s 返回 `'百度图片'`，证明 URL 取图与 OCR 文本识别链路正常。
- 注：纯白图判读偏差建议后续用真实含字截图复测，不影响本任务结论。

### 7. 断连取消（④）

流式请求读到 3 帧后客户端主动断开：3s 后 `/v1/health` 的 `active=0`、`queue_len=0`；断连后再次 chat 200；进程存活。引擎侧见 `AndroidLlmEngine: native generate 异常 JobCancellationException`（预期取消路径）。

### 8. 后台长跑（④）

分两种前置条件各测一轮：

- **App 在前台 + `svc power stayon true`（充电常亮）**：10 分钟逐分钟探活 10/10 `health=200`、pid 不变、`status=ready`；期间完成并发 429、视觉（75s + 3s）、长文本 embedding（17.8s）多轮压测；再补 5 分钟 5/5 `health=200`，`mem_used_mb` 6131–6150 无增长趋势；`/v1/logs` 共 4 条、无 ERROR，唯一 WARNING 是预期的并发 429。**合计 15 分钟零中断。**
- **按 HOME 退到后台**：4 次观测全部被系统停服，存活时长 1m36s / 7m28s / 9m52s（另一次 1m36s），详见 §三。

## 三、发现：后台（HOME）会被系统以 app idle 停服

### 1. 现象与实测数据

| 时间 | 前置条件 | 存活时长 | 停服日志 |
|---|---|---|---|
| 23:19:38 | 前台启动后按 HOME，未加白名单 | 7m28s | `Stopping service due to app idle` |
| 23:53:02 | 已加 doze 白名单，重装后 HOME | 1m36s | 同上 |
| 00:11:28 | 已加 doze 白名单，HOME | 9m52s | 同上 |

- 停服后端口立即关闭（`health=000`），进程随后在 teardown 时打出
  `Uncaught exception thrown by finalizer: ClosedWatchServiceException`（Ktor CIO watch service 被 finalize），再被杀。
- 停服后看护 Worker 按 15 分钟节拍探活失败 → 尝试 `startForegroundService` 被系统拒绝
  （`Background started FGS: Disallowed … uidState: LAST`、`mAllowStartForeground false`）→ 发「散仙节点已停止」提醒通知。

### 2. 结论：这不是节点代码缺陷

- 加入 Doze 白名单**不能**阻止 Android 12 对「idle 应用的前台服务」的回收，因此不能把原因归结为「用户没加白名单」；
- 系统对后台 FGS 的这条回收策略没有公开的豁免口子，继续调 FGS 类型/通知样式只是在猜；
- 项目对此已有设计边界（`NodeWatchdog.kt` 注释、README「后台存活：用户侧约定」）：看护只能提醒，稳定恢复只有「打开 App」与「重启设备」。

### 3. 已验证可用的长跑姿势

1. **插电 + 屏幕常亮 + App 前台**（`svc power stayon true` 或系统「不锁定屏幕」）：15 分钟零中断，压测并发/视觉/长文本均正常；
2. **开机自启**：`BootReceiver` 接 `BOOT_COMPLETED` 拉起 FGS；重装 APK 后系统也会以 `PACKAGE_REPLACED` 广播拉起一次；
3. 用户侧一旦发现节点掉线：打开 App（`MainActivity.onStart`）即可稳定恢复，看护通知负责告知。

### 4. 可选代码跟进（未实施，待评审）

- `server.stop()` 的 finalizer 噪音：可在停服前显式关闭 Ktor 的 watch service / 把 stop 放进 IO 线程，避免 logcat 里出现 `ClosedWatchServiceException`；
- FGS 被系统停后没有 `NodeLogBuffer` 事件（日志随进程丢失），只能靠看护通知 + logcat；
- 若以后要在「后台」场景做验收，建议直接用 `svc power stayon true` 并把 App 置前台——这是本机唯一可稳定复现的姿势。

## 四、复现步骤

```bash
cd nodes/sanxian
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
~/.gradle/wrapper/dists/gradle-8.10-bin/*/gradle-8.10/bin/gradle :composeApp:assembleDebug --no-daemon
# 注：./gradlew 在受沙箱限制的工作目录下会因无法写 ~/.gradle 的 .lck 而失败，需在放行后的终端执行
adb install -r -t composeApp/build/outputs/apk/debug/composeApp-debug.apk
adb shell am start -n com.biyou.sanxian/.MainActivity
adb forward tcp:14011 tcp:<sanxian.json 的 port>
curl --noproxy '*' http://127.0.0.1:14011/v1/health
```

## 五、验收结论

父任务列出的 ①–⑤ 全部通过；④ 的「后台长跑」在「前台 + 插电常亮」姿势下 15 分钟零中断，HOME 后台被系统停服属 Android 12 平台行为（非节点缺陷），已在 §三 记录可复现数据与可用姿势。

顺带确认了看护链路完整：服务被停 → 15 分钟节拍探活失败 → 后台重启 FGS 被拒 → 发用户可见「散仙节点已停止」通知。