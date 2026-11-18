# 执行记录：散仙真机验收

## 步骤与结果

1. **环境确认**：设备 `XQ_AT72`（Android 12 / arm64-v8a / 12GB）已通过 `adb` 连接；模型目录有 chat / embedding / vision + mmproj 四个 GGUF；App 端 `sanxian.json` 的 `port` 为 **4031**（不是 assets 默认的 4011），因此 `adb forward tcp:14011 tcp:4031`。
2. **重编**：设备上的 APK 是 11-15 的，源码已前进到 11-18（`02a42ab` sanxian active）。用 `~/.gradle/wrapper/dists/.../gradle-8.10` 增量编译 `:composeApp:assembleDebug`，36s，成功；`adb install -r -t` 后 `/v1/health` 约 2s 就绪。
3. **单测**：`:shared:testDebugUnitTest` 70 + `:composeApp:testDebugUnitTest` 6 = **76 passed / 0 failed**。
4. **① 四端点冒烟**：health / models / chat（非流式 + 流式）/ embeddings / logs / 视觉 URL 全部 200，三件套响应头齐全，`active`（v1.2）正常上报。
5. **② 长文本 embedding**：14011 字符 → 12009 tokens → 截断到 1024，WARN 出现在 `llama_jni`，HTTP 200，进程不崩。
6. **③ pooling**：`print_info: pooling type = 3`（LAST）+ `embedding pooling 使用模型默认值: 3`，无回退分支。
7. **⑤ embedding 维度与语义**：dim=1024；「柔弱于水」vs「水滴石穿」cos=0.674，远高于无关句对（0.23–0.26）。
8. **并发**：两路在途时 `active=2` 持续可观测，第 3 路 429 + `Retry-After: 1` + `rate_limit_exceeded`；结束后 `active=0`。
9. **④ 断连**：读到 3 帧断开，3s 后 `active=0`，再请求 200，进程存活。
10. **④ 长跑**：前台 + `svc power stayon true` 下 15 分钟零中断、内存平稳；HOME 后台 4 次观测均在 1m36s–17m50s 内被 `Stopping service due to app idle` 停掉。
11. **看护链路**：停服后 Worker 到点 → 探活失败 → 后台拉起 FGS 被拒 → 发出 `id=1002`「散仙节点已停止」通知；回到 App 后通知被清除。

## 注意

- 一度把「后台停服」归因于缺电池白名单并准备改代码，复核后确认 `MainActivity` / Manifest / 首页横幅**早已具备**白名单引导，且实测加白名单后仍会被 app idle 停服；因此已放弃该改动（`git status` 中 `nodes/sanxian` 无改动），结论以实测为准。
- `./gradlew` 在当前沙箱下无法写 `~/.gradle` 的 `.lck`，编译需用缓存中的 Gradle 发行版并放行该目录。

## 验证命令

```bash
python3 -m pytest nodes/quasheng/tests -q   # 本轮未触及准圣，未跑
cd nodes/sanxian && gradle :shared:testDebugUnitTest :composeApp:testDebugUnitTest --no-daemon
```
