/**
 * 碧游宫本地模型插件 · Host half。
 *
 * 装配顺序：
 *   RuntimeManager → ModelManager → Downloader → Supervisor → Provider → Bridge
 *
 * 所有副作用都挂在 Cordis effect 上：插件禁用/热重载时先移除路由与 provider，
 * 再停掉本插件启动的 llama-server 进程组；用户模型文件保留。
 */
import { homedir, totalmem } from 'node:os'
import { createModelPaths, resolveModelsRoot } from './runtime/state.js'
import { Downloader } from './runtime/downloader.js'
import { RuntimeManager } from './runtime/runtime.js'
import { ModelManager } from './runtime/models.js'
import { Supervisor } from './runtime/supervisor.js'
import { createLocalAdapter, PROVIDER_ID } from './runtime/provider.js'
import { registerBridge } from './runtime/bridge.js'

export const name = 'biyou-local-models'
export const inject = ['webServer', 'connection', 'llm']

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {{port?: number, idleUnloadMs?: number, externalBaseUrl?: string, mirror?: string}} config
 */
export async function apply(ctx, config = {}) {
  const logger = ctx.logger ?? console
  const paths = createModelPaths(resolveModelsRoot())
  // 模型下载完成时主动通知客户端刷新 provider 目录，而不是等用户重开选择器。
  const announcedModels = new Set()
  let providerReady = false
  const downloader = new Downloader({
    paths,
    logger,
    onChange: (records) => {
      for (const record of records) {
        if (!providerReady) continue
        if (record.kind !== 'model' || record.status !== 'completed' || announcedModels.has(record.id)) continue
        announcedModels.add(record.id)
        ctx.emit('llm/models-updated', PROVIDER_ID)
      }
    },
  })
  const runtime = new RuntimeManager({ paths, downloader, logger })
  const models = new ModelManager({ paths, downloader, logger, mirror: config.mirror ?? null })
  const supervisor = new Supervisor({
    paths,
    runtime,
    models,
    logger,
    config: {
      port: config.port,
      idleUnloadMs: config.idleUnloadMs,
      externalBaseUrl: config.externalBaseUrl,
    },
  })

  await downloader.init()
  await supervisor.init().catch((error) => {
    logger.warn?.(`[biyou-local-models] 接管上次服务失败：${error.message}`)
  })

  let externalCache = { at: 0, value: null }
  const detectExternal = async () => {
    if (Date.now() - externalCache.at < 15_000) return externalCache.value
    const value = await runtime.detectExternal().catch(() => null)
    externalCache = { at: Date.now(), value }
    return value
  }
  const buildStatus = async () => {
    const [runtimeStatus, modelStatus, serverStatus, external] = await Promise.all([
      runtime.status(),
      models.status({ totalRamBytes: totalmem() }),
      supervisor.status(),
      detectExternal(),
    ])
    return sanitizeStatus({ runtime: runtimeStatus, catalog: modelStatus, server: serverStatus, external, downloads: downloader.list() }, paths)
  }

  const requireId = (args) => {
    if (typeof args.id !== 'string' || args.id.length === 0) {
      const error = new Error('缺少模型 id')
      error.code = 'INVALID_ARGS'
      throw error
    }
    return args.id
  }

  const findRuntimeDownload = () => downloader.list().find((record) => record.kind === 'runtime' && ['downloading', 'verifying', 'paused', 'failed'].includes(record.status))

  const actions = {
    'runtime.install': async (args) => {
      const installed = await runtime.install({ backend: args.backend ?? config.backend })
      return { installed: true, tag: installed.tag, backend: installed.backend, platform: installed.platform, arch: installed.arch }
    },
    'runtime.pause': async () => {
      const record = findRuntimeDownload()
      if (!record) return null
      downloader.pause(record.id)
      return { id: record.id, status: 'paused' }
    },
    'runtime.resume': async () => {
      const record = findRuntimeDownload()
      if (!record) return null
      const installed = await runtime.install({ backend: config.backend })
      return { installed: true, tag: installed.tag, backend: installed.backend }
    },
    'runtime.cancel': async () => {
      const record = findRuntimeDownload()
      if (!record) return null
      await downloader.cancel(record.id)
      return { id: record.id, status: 'cancelled' }
    },
    'runtime.detect': async () => {
      const external = await detectExternal()
      return external
        ? {
            pathBinary: external.pathBinary ? '<PATH>/llama-server' : null,
            server: external.server ? { baseUrl: external.server.baseUrl, role: external.server.role, build: external.server.build } : null,
            ollama: external.ollama ? { baseUrl: external.ollama.baseUrl, models: external.ollama.models } : null,
          }
        : null
    },

    'model.download': async (args) => safeDownload(await models.download(requireId(args))),
    'model.pause': async (args) => {
      models.pause(requireId(args))
      return { id: args.id, status: 'pausing' }
    },
    'model.resume': async (args) => safeDownload(models.resume(requireId(args))),
    'model.cancel': async (args) => {
      await models.cancel(requireId(args))
      return { id: args.id, status: 'cancelled' }
    },
    'model.import': async (args) => {
      const entry = await models.importPath(args.path)
      await models.writePreset()
      ctx.emit('llm/models-updated', PROVIDER_ID)
      return { id: entry.id, display_name: entry.display_name }
    },
    'model.use': async (args) => {
      const id = requireId(args)
      // 先把刚完成的下载并入 registry，避免“文件已下好但还没登记”的竞态。
      await models.status({ totalRamBytes: totalmem() })
      if (!(await models.registryEntry(id)) || !(await models.isInstalled(id))) {
        const error = new Error(`本地模型尚未安装：${id}`)
        error.code = 'MODEL_NOT_INSTALLED'
        throw error
      }
      await models.writePreset()
      await supervisor.ensureModel(id)
      ctx.emit('llm/models-updated', PROVIDER_ID)
      return buildStatus()
    },
    'model.remove': async (args) => {
      const id = requireId(args)
      const current = await supervisor.status()
      if (current.loaded_model === id) await supervisor.stop()
      await models.remove(id)
      await models.writePreset()
      ctx.emit('llm/models-updated', PROVIDER_ID)
      return buildStatus()
    },

    'server.start': async () => {
      await supervisor.start()
      return (await buildStatus()).server
    },
    'server.stop': async () => {
      await supervisor.stop()
      return (await buildStatus()).server
    },
    'server.status': async () => (await buildStatus()).server,
    'catalog.refresh': async () => buildStatus(),
  }

  ctx.effect(() => {
    return registerBridge({
      webServer: ctx.webServer,
      connection: ctx.connection,
      status: buildStatus,
      actions,
      logger,
    })
  }, 'biyou-local-models: bridge')

  // 与官方 adapter 一样走顶层 inject 后的直接注册；注册生命周期跟随插件 fiber。
  const adapter = createLocalAdapter({ models, supervisor, logger })
  ctx.llm.registerAdapter([PROVIDER_ID], adapter)
  providerReady = true
  logger.info?.(`[biyou-local-models] 已注册 provider ${PROVIDER_ID}`)

  ctx.effect(() => {
    // 返回 Promise 的 disposer 会被 Cordis 等待：dsh 收到 SIGTERM 后不会在
    // llama-server 还没停完时就退出，避免桌面端退出后残留。
    return async () => {
      await supervisor.dispose().catch((error) => logger.warn?.(`[biyou-local-models] 停服失败：${error.message}`))
      downloader.dispose()
    }
  }, 'biyou-local-models: cleanup')

  logger.info?.('[biyou-local-models] 数据目录 $DSH_HOME/local-models')
}

/** 前端/日志只拿必要信息：去掉密钥、绝对二进制路径与下载 URL。 */
export function sanitizeStatus(status, paths) {
  const scrub = (value) => scrubText(value, paths)
  const sanitizeRuntime = (runtime) => ({
    ...runtime,
    active: runtime.active
      ? { tag: runtime.active.tag, backend: runtime.active.backend, platform: runtime.active.platform, arch: runtime.active.arch, installedAt: runtime.active.installedAt }
      : null,
    installed: (runtime.installed ?? []).map((item) => ({
      tag: item.tag,
      backend: item.backend,
      platform: item.platform,
      arch: item.arch,
      installedAt: item.installedAt,
    })),
  })
  return {
    runtime: sanitizeRuntime(status.runtime),
    catalog: {
      provider: status.catalog.provider,
      models: (status.catalog.models ?? []).map((model) => ({
        ...model,
        download: model.download ? sanitizeDownload(model.download, scrub) : null,
      })),
    },
    downloads: (status.downloads ?? []).map((record) => sanitizeDownload(record, scrub)),
    server: {
      ...status.server,
      error: status.server.error ? scrub(status.server.error) : null,
      log_tail: (status.server.log_tail ?? []).map(scrub),
    },
    external: status.external
      ? {
          pathBinary: status.external.pathBinary ? '<PATH>/llama-server' : null,
          server: status.external.server
            ? { baseUrl: status.external.server.baseUrl, role: status.external.server.role, build: status.external.server.build }
            : null,
          ollama: status.external.ollama ? { baseUrl: status.external.ollama.baseUrl, models: status.external.ollama.models } : null,
        }
      : null,
    platform: process.platform,
    arch: process.arch,
  }
}

function sanitizeDownload(record, scrub) {
  return {
    id: record.id,
    kind: record.kind,
    title: record.title,
    status: record.status,
    totalBytes: record.totalBytes,
    receivedBytes: record.receivedBytes,
    updatedAt: record.updatedAt,
    error: record.error ? { code: record.error.code, message: scrub(record.error.message) } : null,
    files: (record.files ?? []).map((file) => ({
      name: fileName(file.dest ?? ''),
      size_bytes: file.size_bytes,
      receivedBytes: file.receivedBytes,
      status: file.status,
    })),
  }
}

function safeDownload(record) {
  if (!record) return null
  return { id: record.id, status: record.status, receivedBytes: record.receivedBytes, totalBytes: record.totalBytes }
}

function fileName(path) {
  if (!path) return ''
  const parts = String(path).split(/[\\/]/)
  return parts[parts.length - 1]
}

/** 把用户目录、插件数据目录替换成符号，避免把本机路径带回前端。 */
function scrubText(value, paths) {
  if (typeof value !== 'string') return value
  let text = value
  const home = homedir()
  if (home) text = text.split(home).join('~')
  if (paths?.root) text = text.split(paths.root).join('<local-models>')
  return text
}

