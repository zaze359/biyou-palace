# 设计 · 本地模型插件（dsh-local-models）

## 1. 结论

做一个**独立 dsh 插件**，而不是改品牌插件或复用第三方插件：

- Host half 负责运行时、模型、进程、provider 注册；
- Browser half 只负责设置页与进度展示；
- 两者通过同源 JSON bridge 通信；
- 本地模型通过 `ctx.llm.registerAdapter()` 成为 dsh 的 provider，不改 profile、不写静态配置。

## 2. 架构

```text
dsh Host
  └─ @biyou/dsh-local-models（Host half）
       ├─ RuntimeManager   清单 / 下载 / 校验 / 解压 / 激活
       ├─ ModelManager     目录 / 下载 / 导入 / 完整性
       ├─ Supervisor       llama-server 生命周期
       ├─ LocalProvider    ctx.llm.registerAdapter('biyou-local')
       └─ Bridge           /api/biyou-local/*（同源 + 白名单）
  └─ Browser half
       └─ settings.plugins.tab「本地模型」
            Runtime / Models / Status
```

不放在 Electron main：本地模型属于 dsh 的能力，Host half 可直接拿 `llm`、`webServer`、`settings`，热重载与退出清理也由 Cordis effect 统一管理。

## 3. 包结构

```text
desktop/packages/biyou-local-models/
├── package.json
├── lib/
│   ├── index.js              Host apply：装配、effect、provider、bridge
│   ├── client.js             Browser half：settings.plugins.tab
│   ├── runtime/
│   │   ├── manifest.json     固定的 llama.cpp 构建清单
│   │   ├── catalog.json      首版模型目录
│   │   ├── platform.js       平台/架构识别与后端选择
│   │   ├── downloader.js     通用下载器（Range、partials、校验）
│   │   ├── runtime.js        运行时安装/激活/发现
│   │   ├── models.js         模型目录/下载/导入/完整性
│   │   ├── gguf.js           GGUF 头部解析
│   │   ├── supervisor.js     llama-server 生命周期
│   │   ├── provider.js       ctx.llm adapter
│   │   ├── bridge.js         同源 JSON bridge
│   │   └── state.js          原子读写与目录
│   └── assets/icon.svg
└── test/
    ├── unit/
    └── integration/
```

`package.json`：

```json
{
  "name": "@biyou/dsh-local-models",
  "version": "0.1.0",
  "type": "module",
  "main": "lib/index.js",
  "exports": {
    ".": "./lib/index.js",
    "./client": "./lib/client.js",
    "./package.json": "./package.json"
  },
  "dsh": {
    "client": {
      "platform": "web",
      "inject": [
        "@deepseek-ai/dsh-client-ui-renderer",
        "@deepseek-ai/dsh-client-ui-layout"
      ]
    }
  }
}
```

## 4. 运行时清单

`runtime/manifest.json` 是唯一版本来源；安装器只读它，不抓 latest。

```json
{
  "schema_version": 1,
  "engine": "llama.cpp",
  "tag": "bXXXXX",
  "targets": {
    "darwin-x64": {
      "backend": "metal",
      "url": "https://.../llama-bXXXXX-bin-macos-x64.zip",
      "sha256": "...",
      "archive": "zip",
      "binary": "llama-server"
    },
    "win32-x64": {
      "backend": "cpu",
      "url": "https://.../llama-bXXXXX-bin-win-cpu-x64.zip",
      "sha256": "...",
      "archive": "zip",
      "binary": "llama-server.exe"
    }
  }
}
```

规则：

- 每个 target 必须有 `url`、`sha256`、`binary`；
- 不支持的组合在安装前拒绝，不猜测；
- 解压到临时目录，校验通过后原子改名为 `runtimes/<tag>-<backend>/`；
- 激活只改 `active-runtime.json`，不移动已安装目录；
- 旧版本保留最近一个，其余可清理。

## 5. 模型目录

`runtime/catalog.json`：

```json
{
  "schema_version": 1,
  "models": [
    {
      "id": "qwen3-8b-q4km",
      "display_name": "Qwen3 8B Q4_K_M",
      "repo": "unsloth/Qwen3-8B-GGUF",
      "revision": "<commit>",
      "license": "apache-2.0",
      "tool_calling": true,
      "n_ctx_train": 131072,
      "files": [
        {
          "path": "Qwen3-8B-Q4_K_M.gguf",
          "size_bytes": 5200000000,
          "sha256": "..."
        }
      ],
      "memory": {
        "weights_bytes": 5200000000,
        "kv_bytes_per_1k_ctx": 134000000,
        "min_ctx": 65536,
        "recommended_ctx": 131072
      }
    }
  ]
}
```

首版只放经过工具调用验证的模型；不提供低于 4-bit 的档位；分片模型用 `files[]` 表达，齐全才算可服务。

## 6. 下载器

同一个下载器服务运行时与模型：

- 目标目录：`userData/local-models/cache/partials/<plan-id>/<file>`
- 支持 `Range` 续传；服务端不支持 Range 时从头开始；
- 进度按整份下载计划统计（已完成文件 + 当前文件字节）；
- 暂停在 chunk 边界；恢复继续写 partial；
- 完成后校验 `sha256`，再原子 rename 到最终路径；
- 未完成或校验失败的文件不出现在模型列表；
- 下载 job 持久化到 `downloads.json`，进程重启后可见并可恢复。

## 7. GGUF 与硬件适配

`gguf.js` 只读头部，不加载权重：

- 校验 magic/version；
- 读取架构、层数、量化类型、训练上下文、词表大小、MoE 标记；
- 文件名只作为兜底。

`platform.js`：

- `darwin` + `x64` → Metal 优先，失败回退 CPU；
- `win32` + `x64` → 有 NVIDIA 用 CUDA，AMD/Intel 用 Vulkan，否则 CPU；
- 其他平台明确 `unsupported`。

内存估算：

```text
estimated = weights + kv(ctx) + compute_buffer + runtime_overhead
```

首版用保守系数；估算只用于排序与提示，启动后以实际日志/健康检查为准。

## 8. Supervisor

状态机：

```text
stopped → starting → ready → stopping → stopped
              └── failed ←── crash
```

要点：

- 端口：默认 `18434`，被占用则取随机空闲端口；
- 状态文件：`state/server.json`，含 `base_url`、`api_key`、`pid`、`model_id`、`started_at`；
- API key：随机生成，`state/.api_key`，0600，通过 `--api-key-file` 传入；
- 就绪：`/health` 200 后发一次真实生成（固定短提示 + 期望词），通过才 ready；
- 崩溃：1/5/15/60 秒退避重启，超过次数进入 failed 并提示；
- 空闲：15 分钟无请求卸载模型（不是停 router）；
- 退出：Cordis effect 的 disposer 停服；硬杀后的孤儿在下次启动按 `server.json` 的 pid 判断并接管或替换；
- 外部服务：探测 `:8080` 与配置端口的 `/props`，识别真实 llama-server；Ollama 用 `/api/tags` 单独提示。

## 9. Provider 适配

在 `index.js`：

```js
ctx.inject(['llm'], (child) => {
  child.effect(() => {
    const handle = child.llm.registerAdapter(['biyou-local'], adapter)
    return () => handle()
  }, 'biyou-local: llm provider')
})
```

`provider.js` 负责：

- `listModels()` 返回注册表里可服务的模型；
- 请求前确保对应模型已加载（必要时启动/切换）；
- 将 dsh messages/tools 转为 OpenAI chat/completions；
- 解析 SSE：text、reasoning、tool calls、usage、finish；
- 失败时返回带模型 id 与本地服务状态的错误，不吞成 unknown。

不写 profile patch，不改 `agent-default-model`；用户在模型选择器里选本地模型即可。

## 10. Bridge 与设置页

Host 注册精确路由：

```js
webServer.register({ kind: 'exact', path: '/api/biyou-local/status', handler })
webServer.register({ kind: 'exact', path: '/api/biyou-local/action', handler })
```

- 仅接受同源浏览器请求（`Sec-Fetch-Site` / Origin / Host 校验）；
- `action` 只允许白名单：`runtime.install`、`runtime.pause`、`runtime.resume`、`model.download`、`model.cancel`、`model.import`、`model.use`、`server.start`、`server.stop`、`server.status`；
- 请求体上限 256 KB；
- 返回结构化 `{ ok, value | error }`；
- 不通过 URL 参数传路径，路径由 Host 侧解析。

Browser half 注册：

```js
ctx.slots.inject('settings.plugins.tab', () =>
  ctx.slots.register({
    name: 'settings.plugins.tab',
    id: 'biyou.local-models',
    order: 40,
    label: () => '本地模型',
  }, LocalModelsPage)
)
```

页面三个区：

- **Runtime**：平台/架构、后端、安装状态、版本、安装/更新按钮；
- **Models**：目录、下载进度、暂停/恢复/取消、导入 GGUF、使用/卸载；
- **Status**：服务状态、端口、当前模型、最近日志、错误。

样式只用 dsh 主题 token，不引入第二套色板。

## 11. 持久化

```text
userData/local-models/
├── runtimes/<tag>-<backend>/     llama-server 及依赖
├── models/<owner>/<repo>/...     GGUF 与 mmproj
├── cache/partials/               未完成下载
├── state/
│   ├── active-runtime.json
│   ├── server.json
│   ├── registry.json
│   ├── downloads.json
│   └── .api_key
└── logs/llama-server.log
```

所有写操作：临时文件 + rename；读取容忍缺失/损坏；进程 pid 失效时清理。

## 12. 安全

- 服务只绑定 `127.0.0.1`；
- API key 不进入日志、不返回前端；
- Bridge 同源 + 白名单，不接受任意命令；
- 模型下载 URL 与 hash 来自仓库清单，不接受前端任意 URL；
- 导入本地文件时只接受 `.gguf`，读取头部后入库；
- 插件卸载或禁用时停止子进程，不删除用户模型文件（提示手动清理）。

## 13. 测试策略

- 单元：清单解析、校验失败、分片完整性、GGUF 头、端口/key 持久化、状态机、暂停/恢复。
- 集成：假运行时/假 llama-server 模拟下载→安装→启动→就绪→崩溃→重载→退出清理。
- 手工：macOS x64 与 Windows x64 各跑一次真实 llama.cpp + 一个 GGUF。
- 回归：现有 `npm run smoke`；退出后 `ps` 无 llama-server。

## 14. 备选方案

| 方案 | 结论 |
|---|---|
| 直接依赖 `dsh-open-model` | macOS x64 不支持，0 star，核心能力受制于人 |
| 用 `dsh-local-models` | 只做接线，不自带运行时和模型下载，不满足首启体验 |
| 改品牌插件 | 职责混杂，无法独立启停 |
| Electron main 管理 | 生命周期与 dsh 脱节，UI 还需额外通道 |

选独立 dsh 插件，参考 `dsh-open-model` 的公开接法（MIT），运行时/下载/适配逻辑自研。
