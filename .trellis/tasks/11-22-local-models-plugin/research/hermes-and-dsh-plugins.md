# 调研 · Hermes 本地模型与 DSH 插件生态

## 1. Hermes Agent 的实现

Hermes 的本地模型能力不是捆绑模型，而是“空手启动 + 按需安装”：

```text
Install runtime → Download model → Use
```

### 运行时

- 引擎版本只来自 `pm/lock.json`：版本、平台 URL、SHA-256；不允许 `local_runtime.tag` 覆盖。
- 按平台/GPU 选后端：macOS Metal、NVIDIA CUDA、AMD/Intel Vulkan、否则 CPU。
- 下载到可写 `cache/partials`，支持 Range 续传；解压、校验、激活分阶段。
- 引擎可预置；不存在时走受管安装，不抓 latest。

### 模型

- `catalog.json` 描述模型、量化档位、文件/分片、大小、revision、训练上下文、层数、mmproj、MTP、采样参数。
- 下载前按本机内存估算；推荐“全量 GPU 且 ≥20 tok/s”的最高质量；低于 4-bit 不提供；推荐模型至少 64K 上下文。
- 分片 GGUF 必须齐全才可服务；未完成文件不发布；已有 `.gguf` 可链接入库。

### 服务

- 一个进程一个 llama-server router；稳定端口 18434，避开 8080。
- 稳定 API key 文件，通过 `--api-key-file` 传入。
- 就绪不是 `/health` 200，而是真实生成一次（“法国首都”→ Paris）。
- 崩溃按 1/5/15/60 秒退避；空闲 15 分钟卸载模型；退出停服；硬杀孤儿下次启动接管或替换。
- 探测已有 llama-server（`/props` 指纹）；Ollama/LM Studio 没有该指纹。

### 关键源码

- `hermes_cli/local_runtime/bootstrap.py`
- `hermes_cli/local_runtime/binaries.py`
- `hermes_cli/local_runtime/supervisor.py`
- `hermes_cli/local_runtime/estimator.py`
- `hermes_cli/local_runtime/detect.py`
- `hermes_cli/local_runtime/endpoint.py`
- `hermes_cli/local_runtime/catalog.json`

## 2. DSH 插件市场现状

DSH 自带市场没有官方索引；Discover 只读取用户添加的 index 源。第三方目录站：dsh.so、dsh-plugin.org、dshbase、dshplugin.store。

### 已有类似插件

| 插件 | 能力 | 缺口 |
|---|---|---|
| `yorelog/dsh-open-model` | 搜模型、下载、装运行时、启动、空闲释放、`ctx.llm.registerAdapter()` | macOS 仅 arm64（mlxcel）；无 Hermes 级硬件适配；0 star |
| `Vmarcelo49/dsh-local-models` | 选 GGUF、VRAM 估算、调参、注册 provider | 需自备 llama-server；不下载模型 |
| `Lbunc/dsh-local-llm-controller` | 一键启停、35B/9B 预设、自动注册 | 需自备 llama.cpp；偏 Windows |
| `PerryLink/dsh-local-ai` | Ollama 路由、模型拉取/删除、云端回退 | 依赖已有 Ollama；无内置运行时 |

### 对本机的判断

开发机为 macOS x64（Intel）。`dsh-open-model` 的 macOS 路径只支持 arm64，`dsh-local-llm-controller` 偏 Windows，`dsh-local-models` 需要用户自己编译 llama-server。**没有现成插件覆盖 macOS x64 + Windows x64 + 托管运行时 + 模型下载。**

## 3. 可复用的 DSH 接法

来自 `dsh-open-model`（MIT）的公开实现：

```js
// LLM provider
ctx.inject(['llm'], (child) => {
  child.effect(() => {
    const handle = child.llm.registerAdapter(['biyou-local'], adapter)
    return () => handle()
  }, 'biyou-local: llm provider')
})

// Browser ← Host JSON bridge
ctx.inject(['webServer'], (child) => {
  child.effect(() => child.webServer.register({
    kind: 'exact',
    path: '/api/biyou-local/status',
    handler,
  }), 'biyou-local: bridge')
})

// 自定义设置页
ctx.inject(['settings'], (child) => {
  child.effect(() => child.settings.configure({ auto: false }, ctx.fiber), 'biyou-local: settings')
})
```

Browser half：

```js
ctx.slots.register({
  name: 'settings.plugins.tab',
  id: 'biyou.local-models',
  order: 40,
  label: () => '本地模型',
}, LocalModelsPage)
```

`settings.plugins.tab` 由 `dsh-client-ui-settings-plugins` 提供的 Built-in plugins 页面承载，不需要新增 Settings 导航行。

## 4. 结论

采用独立插件 `@biyou/dsh-local-models`：

- 自研运行时/下载/模型管理；
- 复用 `registerAdapter` / `webServer.register` / `settings.plugins.tab` 的接法；
- 首版覆盖 macOS x64 + Windows x64；
- 不以第三方插件作为核心依赖，但保留外部 llama-server / Ollama 探测。
