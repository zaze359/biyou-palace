# 散仙视觉 / OCR 能力接入

> 父任务：`10-14-sanxian-node-agent` · 契约：`docs/node-agent-contract.md` v1.0
> 真机：XQ-AT72（Android 12 / arm64-v8a / 12GB RAM）
> 提交：`d38f1b1bfa292f6133440b348f8bac3e5cd20953`

## 一、已落地内容（既成事实）

### 1. 原生层
- 将上游 llama.cpp `tools/mtmd` 整套源码（69 个文件，约 1.3M）并入 `nodes/sanxian/nativelib/src/main/cpp/third_party/llama.cpp/tools/mtmd`，源码在仓库内，随上述提交入库。
- `CMakeLists.txt` 打开 `LLAMA_BUILD_MTMD`，把 mtmd 静态链接进 `libllama_jni.so`；复用仓库已有的 `vendor/stb/stb_image.h`。
- `jni_bridge.cpp` 新增视觉入口：加载 LLM + mmproj、`mtmd_helper_eval_chunks` 注入图像、视觉 token 采样、释放；纯文本生成路径保持原行为。

### 2. Kotlin 层
- 新增独立视觉引擎：`VisionEngine` 接口、`AndroidVisionEngine`（JNI 包装）、`AndroidVisionImageLoader`（data URL / 本地文件、最长边缩放、体积与解码校验）、`VisionConfig`。
- `NodeServer` / `NodeApi` 接入视觉引擎；`installNodeApi` 增加视觉参数与能力分流。
- 契约 `ChatMessage.content` 从 `String` 扩展为「字符串或 content 数组」，兼容旧客户端；UI 侧 `ChatViewModel` 与 `NodeChatClient` 同步改为 `ChatContent.Text`。
- `/v1/health` 与 `/v1/models` 按真实加载状态声明 `vision` / `ocr`。

### 3. 错误语义分层
| 场景 | 状态码 | code |
|---|---|---|
| 未声明视觉/OCR 能力 | 501 | `capability_not_supported` |
| 已声明但视觉权重未加载 | 503 | `upstream_unavailable`（message=视觉模型未加载） |
| 图片原始体积 > 4MB 或解码失败 | 400 | `invalid_parameter`（message 带实际 KB 与上限） |
| 发热降载拒绝新请求 | 503 | `thermal_shedding`，带 `Retry-After: 10` |

### 4. 配置与权重
- 新增 `vision_model`、`vision_mmproj`、`vision_context_len=4096`、`vision_image_max_tokens=512`。
- 默认值三处对齐：`VisionConfig.imageMaxTokens`、`NodeConfig` 字段与 `fromMap` 兜底、`composeApp/src/androidMain/assets/sanxian.json`。
- README 补视觉配置示例与三档实测数据。
- 设备权重（已推送到 `/sdcard/Android/data/com.biyou.sanxian/files/models/`，属主为 App uid）：
  - `Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` 1,929,901,056 B，sha256 `d02fe9b6…486c12`
  - `mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf` 1,338,428,128 B，sha256 `b9160fe9…60d5e`
  - 两者均与 HF 仓库大小、sha256 一致。

### 5. 本轮修掉的两个缺陷
- **JNI UTF-8 截断 SIGABRT**：llama.cpp 的单个 token 可能只含汉字的部分字节（实测 `0xE5 0xBA`），逐 token `NewStringUTF` 触发 CheckJNI `illegal continuation byte 0` 并 abort。已在文本与视觉两条生成循环中加入跨 token 的 UTF-8 残片缓冲，只向 Java 交付完整字符。
- **缺失视觉权重误报 501**：`resolveModel` 会把不存在的视觉文件静默置空，导致「已配置但未加载」退化成「未配置」。新增 `resolveVisionPath`，即使文件缺失也保留候选路径，由引擎加载失败 → `loaded=false` + 请求 503。

## 二、真机验证数据

### 1. 安装与加载
- 设备内 `libllama_jni.so` 含 `initVision` / `generateVision` / `visionMediaMarker`；assets 视觉配置生效。
- `/v1/health`：视觉模型 `loaded=true`，`capabilities=["chat","vision","ocr"]`，`max_context_len=4096`，`effective_max_concurrency=2`。
- 常驻内存：PSS 约 4.82G / RSS 4.91G；推理峰值 RSS 5.66G（+约 280MB），`MemAvailable` 最低 5.67G；无 OOM、无 lowmemorykiller。
- 发热降载与视觉推理未互相干扰：强制 `thermalservice override-status 3` 时视觉请求得到 503 + `Retry-After: 10`；`reset` 后恢复 `nominal`、有效并发回到 2。

### 2. 单次真实图像请求（1096×2560 设备截图，缩到最长边 1024）
- prompt 616 tokens（图约 512），生成 64 tokens / 97 字，正常收到 `[DONE]`。
- 首 content 89.95s，总耗时 98.42s；其中图像编码 + prompt eval 约 85s，纯文本解码约 8.5s（≈7.5 tok/s）。
- OCR 输出与截图对话框内容逐字一致（标题、两行正文、两个按钮）。
- 小图（1×1）3.19s；纯文本 chat 0.66s，未受视觉句柄影响。

### 3. 三档 × 三次调参（同一截图、同一提示词、每次 force-stop 冷启动）
| 档位 | 次数 | 首 content | 总耗时 | 字数 | 漏读项（除共同外围外） |
|---|---|---|---|---|---|
| 1024 | 1 | 89.920s | 101.937s | 126 | 节点访问 |
| 1024 | 2 | 118.616s | 138.298s | 126 | 节点访问 |
| 1024 | 3 | 125.785s | 139.574s | 97 | 节点访问、URL |
| 512 | 1 | 77.292s | 96.920s | 131 | — |
| 512 | 2 | 104.671s | 125.258s | 126 | 节点访问 |
| 512 | 3 | 112.090s | 133.752s | 131 | — |
| 256 | 1 | 36.757s | 47.813s | 125 | 节点访问、`电池 中` 空格 |
| 256 | 2 | 37.552s | 51.671s | 126 | 节点访问 |
| 256 | 3 | 46.275s | 65.057s | 125 | 节点访问、`电池 中` 空格 |

- 共同漏读（三档全部）：顶部状态栏 `21:02` / `100%`、页面标题 `小手机 · 离线对话`、底部 `消息经由本机节点服务生成`、输入框 `输入消息…`、对话框背后被遮挡的卡片文字。
- 档位区间：1024 首 content 89.9–125.8s、总耗时 101.9–139.6s；512 首 content 77.3–112.1s、总耗时 96.9–133.8s；256 首 content 36.8–46.3s、总耗时 47.8–65.1s。
- 温度与耗时：256 档三次电池温度 34.2 / 35.2 / 36.7℃，首 content 36.8 / 37.6 / 46.3s，正相关。1024/512 未逐次记录温度，但按执行顺序温度累积，1024 的 89.9→125.8s、512 的 77.3→112.1s 与热状态混杂，区间比单点更有意义。
- **1024 不是质量上界**：它 3 次都没读到 `节点访问`，第 3 次连 URL 也丢了；512 反而有 2/3 读到 `节点访问`。1024 第 3 次在 97 字处 `finish_reason=stop`，说明差异来自模型主动收尾与提示词优先级，不能把「多 token = 多识别」当作单调关系。
- 稳定性结论：对话框标题/正文/按钮在 512 与 1024 的 6 次中全部逐字正确；512 出现 1/3 丢 `节点访问`；256 出现 2/3 的空格丢失（`电池中关闭`），已贴近分辨率下限。

## 三、已知问题
- **首字延迟高**：512 档 77–112s，1024 档 90–126s，256 档 37–46s；图像编码是主要成本，纯文本解码只占约 8.5s。
- **耗时与发热强相关**：同一档三次可差 40% 以上；每次 `force-stop` 只能保证服务冷启动，不能复位设备热状态，因此跨档对比存在顺序偏差。
- **外围 chrome 读不到是提示词限制**：提示词只要「主要文字」，状态栏、页面标题、底部提示、输入框在三档全部未被输出，提升 token 上限不会解决。
- `节点访问` 这类中号标签在所有档位都不稳定，属于边际内容。
- 视觉与文本共用同一进程内存；目前余量约 5.6G，未做互斥加载。

## 四、后续待办
1. mmproj 量化：f16（约 1.34G）→ Q8/Q4，优先压缩视觉编码器权重与编码耗时。
2. 评估更小视觉编码器 / 更小 VL 模型，降低图像编码固定成本。
3. 提示词或分区裁剪：若要读状态栏、标题、底部提示，需显式提示或按区域裁剪，而不是继续加 token。
4. 长跑 OCR 的节流与降载阈值复核：连续视觉请求下的升温曲线、降载触发频率、`Retry-After` 是否足够。
5. release 签名与 CI：当前只有 debug 包，视觉 so 与权重校验尚未进入流水线。

## 五、验证与提交
- 单测：`shared` 65 + `composeApp` 6，0 失败。
- 提交 `d38f1b1bfa292f6133440b348f8bac3e5cd20953`，88 个文件（含 mtmd 69 个）。
- 本节任务卡为事后补记，未提交。
