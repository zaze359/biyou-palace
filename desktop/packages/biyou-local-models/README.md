# @biyou/dsh-local-models

碧游宫桌面客户端的本地模型插件：安装 llama.cpp 运行时、下载/导入 GGUF、托管
`llama-server`，并通过 `ctx.llm.registerAdapter(['biyou-local'], adapter)` 把本地
模型注册成 dsh 的一等 provider。

设置页入口：**设置 → 插件 → 本地模型**。

## 目录

```text
lib/
├── index.js              Host apply：装配、provider、bridge、清理
├── client.js             Browser half：settings.plugins.tab
└── runtime/
    ├── manifest.json     固定 llama.cpp 版本与平台构建
    ├── catalog.json      首版模型目录（Qwen3 8B/4B/1.7B Q4_K_M）
    ├── platform.js       平台/架构/后端选择与内存估算
    ├── downloader.js     Range 续传、暂停/恢复/取消、SHA-256
    ├── runtime.js        运行时分发、解压、激活、外部探测
    ├── models.js         registry、下载计划、分片完整性、导入、preset
    ├── gguf.js           GGUF 头解析
    ├── supervisor.js     llama-server router 生命周期
    ├── provider.js       OpenAI chat/completions 适配与 SSE 解析
    ├── attribution.js    dsh provider 归属头
    ├── bridge.js         同源 JSON bridge
    └── state.js          原子读写与数据目录
```

## 数据目录

一切可写数据在 `$DSH_HOME/local-models/`（桌面端即
`<userData>/dsh/local-models/`），不写仓库：

```text
local-models/
├── runtimes/<tag>-<backend>/    受管 llama.cpp
├── models/<owner>/<repo>/       GGUF；导入模型在 models/imported/<id>/
├── cache/partials/<job>/        未完成下载
├── cache/extract-<id>/          解压临时目录
├── state/
│   ├── active-runtime.json
│   ├── registry.json
│   ├── downloads.json
│   ├── models.ini              router 的 --models-preset
│   ├── server.json             不含 key
│   └── .api_key                0600
└── logs/llama-server.log
```

`BIYOU_LOCAL_MODELS_DIR` 可覆盖根目录，只用于测试/排障。

## 运行时清单

`lib/runtime/manifest.json` 是唯一版本来源；不运行时抓 latest。

| key | 平台 | 后端 | 大小 |
|---|---|---|---|
| `darwin-x64` | macOS Intel | Metal | 12.4 MB |
| `darwin-arm64` | macOS Apple Silicon | Metal | 16.6 MB |
| `win32-x64-cpu` | Windows x64 | CPU | 25.0 MB |
| `win32-x64-vulkan` | Windows x64 | Vulkan | 38.9 MB |
| `win32-x64-cuda` | Windows x64 | CUDA 12.4 | 358 MB |

每个 target 都有 `url`、`sha256`、`size_bytes`、`archive`、`binary`。本机组合没有
target 时安装前明确拒绝。Linux/ia32 等首版不支持。

安装流程：下载到 `cache/` → 校验 SHA-256 → 解压到 `cache/extract-*` → 找到
`llama-server` → 整个根目录 rename 到 `runtimes/<tag>-<backend>/` → 写
`active-runtime.json`。校验失败不会激活。

## 模型目录

首版只放 Qwen3（Apache-2.0、支持工具调用）：

| id | 权重 | 默认上下文 |
|---|---|---|
| `qwen3-8b-q4km` | 5.03 GB | 64K（40K 训练上下文 + YaRN） |
| `qwen3-4b-q4km` | 2.50 GB | 64K |
| `qwen3-1.7b-q4km` | 1.11 GB | 64K |

文件 hash 来自 Hugging Face LFS 的 SHA-256；下载默认走
`https://huggingface.co/<repo>/resolve/<revision>/<path>`。桌面端 profile 已默认配置
`mirror: https://hf-mirror.com`（国内网络直连 HF 常被阻断）；海外用户可在
`cordis.patch.yml` 删除该行。自定义镜像示例：

```yaml
- id: ui-biyou-local-models
  name: '@biyou/dsh-local-models'
  config:
    mirror: https://hf-mirror.com
    idleUnloadMs: 900000
    port: 18434
    backend: metal        # 可选：metal | cuda | vulkan | cpu；缺省按平台推断
```

分片 GGUF 必须 `-00001-of-0000N` 全部齐全才进入可服务列表；未完成/校验失败的
文件不发布。

## 服务与 provider

- 一个插件实例只托管一个 `llama-server` **router**。模型由
  `state/models.ini` 描述，`--models-max 1`，第一次请求时加载。
- 端口默认 `18434`；被占用时顺序试 10 个，仍冲突则取系统空闲端口。
- API key 32 字节随机 hex，写 `state/.api_key`（0600），通过
  `--api-key-file` 传给 router；不写日志、不回前端。
- 就绪判定：`/props` 可用后，再向目标模型发一次
  `Reply with exactly: OK` 的真实生成，输出包含 `OK` 才 ready。
- 模型切换：`/models/unload` 旧模型 → `/models/load` 新模型 → 轮询
  `/v1/models` 的 `status.value === loaded` → 真实生成探针。
- 空闲 15 分钟：`/models/unload`，router 保留；下一条消息自动重载。
- 崩溃：1/5/15/60 秒退避重启；4 次后进入 error，UI 显示日志尾部。
- 退出/热重载：按进程组 `SIGTERM` → 3 秒后 `SIGKILL`；异常退出留下的
  `server.json` 在下次启动时先 `/props` 验证，能接管则接管，否则清理。
- 外部服务：探测 PATH 里的 `llama-server`、`127.0.0.1:8080` 的 `/props`、
  `127.0.0.1:11434` 的 Ollama `/api/tags`，只做提示；首版不自动接管。

安全边界：

- router 只绑定 `127.0.0.1`，所有对外请求要求 Bearer key。
- llama.cpp router 不会把 `--api-key-file` 透传给按需拉起的子进程；子进程监听
  随机回环端口，不暴露到网络。这是上游行为，插件侧已保证 router 入口鉴权。
- Bridge 先过 `ctx.connection.requestRejection()`（Host/Origin + 浏览器登录），
  再只接受白名单 action；请求体上限 256KB；不接受前端传 URL/命令。
- 状态与日志返回前会把 `$HOME`、模型根目录替换成 `~` / `<local-models>`。

## 错误码

| code | 场景 |
|---|---|
| `UNSUPPORTED_PLATFORM` | 本机 platform/arch 没有 target |
| `INVALID_MANIFEST` | 清单缺字段/坏 hash |
| `DOWNLOAD_PAUSED` / `DOWNLOAD_FAILED` / `DOWNLOAD_CANCELLED` | 运行时下载未完成 |
| `CHECKSUM_MISMATCH` / `SIZE_MISMATCH` | 下载内容不对，文件不发布 |
| `EXTRACT_FAILED` / `BINARY_MISSING` | 解压失败或找不到 llama-server |
| `NO_RUNTIME` | 没有受管运行时，也没配外部服务 |
| `START_FAILED` / `READY_TIMEOUT` | router 起不来 |
| `LOAD_TIMEOUT` / `READINESS_FAILED` | 模型加载或真实生成探针失败 |
| `UNAUTHORIZED` | key 文件与 router 不一致 |
| `MODEL_NOT_FOUND` / `MODEL_NOT_INSTALLED` | provider 收到未知/未安装模型 |
| `CONTEXT_WINDOW_EXCEEDED` | 请求超过上下文 |
| `LINK_FAILED` | 导入时无法建链接（Windows 权限） |

## 测试

```bash
cd desktop
node packages/biyou-local-models/test/unit/run.mjs
node packages/biyou-local-models/test/integration/run.mjs
# 或 npm run test:local-models
```

- 单元：清单、平台选择、内存估算、分片、GGUF、下载器（完整/续传/校验/取消）、
  导入链接、preset、SSE、status 去敏。
- 集成：假 runtime + 假 llama-server，覆盖导入 → 启动 → 对话 → 工具调用 →
  空闲卸载 → 崩溃重启 → 退出无残留。
- 真实链路已在 macOS x64 + llama.cpp b12620 + Qwen2.5 0.5B（Ollama blob 转
  GGUF 链接）上验证：路由启动、真实生成、工具调用、停止清理均通过。
  Windows 仍需真机验证。

## 首版边界

- 不支持 Linux/Android/iOS；不做模型训练、微调、量化转换、多 GPU。
- 不做模型市场/全量 HF 搜索；目录固定 3 个模型。
- 不做运行时/模型自动更新。
- 外部 Ollama 只提示，不注册 provider；外部 llama-server 需通过
  `config.externalBaseUrl` 显式配置（只允许回环地址）。
- Windows 的符号链接需要开发者模式/管理员权限，失败时退硬链接，仍失败则给出
  可操作提示。
