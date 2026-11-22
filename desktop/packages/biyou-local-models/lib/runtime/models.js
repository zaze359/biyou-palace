/**
 * 模型目录、下载、导入与完整性。
 *
 * 目录里的 `catalog.json` 是精选清单：只放经过验证的模型，文件/分片与 sha256
 * 都固定。已安装状态落在 `state/registry.json`；下载中的状态由 Downloader
 * 统一维护。分片 GGUF 缺任一分片就不进入 servable 列表。
 */
import { link, mkdir, realpath, stat, symlink } from 'node:fs/promises'
import { basename, dirname, extname, join, resolve } from 'node:path'
import { estimateMemory, classifyFit } from './platform.js'
import { readGgufHeader } from './gguf.js'
import { readJson, readText, removeQuietly, writeJsonAtomic, writeTextAtomic } from './state.js'

const CATALOG_URL = new URL('./catalog.json', import.meta.url)
const SHARD_PATTERN = /-(\d{5})-of-(\d{5})\.gguf$/i

export class ModelManager {
  #paths
  #downloader
  #logger
  #catalog = null
  #registry = null
  #syncing = null
  #catalogUrl = CATALOG_URL
  #mirror = null

  constructor({ paths, downloader, logger = console, catalogUrl = CATALOG_URL, mirror = null }) {
    this.#paths = paths
    this.#downloader = downloader
    this.#logger = logger
    this.#catalogUrl = catalogUrl
    this.#mirror = mirror
  }

  /** 加载并校验精选目录。 */
  async catalog() {
    if (this.#catalog) return this.#catalog
    const raw = JSON.parse(await readText(this.#catalogUrl))
    if (!raw || raw.schema_version !== 1 || !Array.isArray(raw.models)) {
      throw modelError('INVALID_CATALOG', '模型目录结构无效')
    }
    const seen = new Set()
    for (const model of raw.models) {
      validateCatalogModel(model)
      if (seen.has(model.id)) throw modelError('INVALID_CATALOG', `模型 id 重复：${model.id}`)
      seen.add(model.id)
    }
    this.#catalog = raw
    return raw
  }

  /** 读取 registry；损坏时按空处理并保留文件，不误删用户模型。 */
  async registry() {
    if (this.#registry) return this.#registry
    const raw = await readJson(this.#paths.registry, { schema_version: 1, models: {} })
    this.#registry = raw && typeof raw.models === 'object' && raw.models ? raw : { schema_version: 1, models: {} }
    return this.#registry
  }

  /** 合并目录、registry、下载任务与内存估算后的完整状态。 */
  async status({ totalRamBytes = 0 } = {}) {
    const catalog = await this.catalog()
    await this.#syncDownloads()
    const registry = await this.registry()
    const downloads = this.#downloader.list().filter((record) => record.kind === 'model')
    const models = []
    for (const model of catalog.models) {
      const entry = registry.models[model.id] ?? null
      const files = await this.#fileStates(model, entry)
      const complete = files.every((file) => file.present && file.size_ok)
      const servable = Boolean(entry) && complete && shardComplete(model.files)
      const ctx = Number(model.context?.default) || Number(model.context?.train) || 40960
      const estimate = estimateMemory(model, ctx)
      const download = downloads.find((record) => record.id === jobId(model.id)) ?? null
      models.push({
        id: model.id,
        display_name: model.display_name,
        description: model.description ?? '',
        repo: model.repo,
        revision: model.revision,
        license: model.license,
        tool_calling: model.tool_calling,
        context: model.context,
        files: files.map((file) => ({ path: file.path, size_bytes: file.size_bytes, present: file.present, size_ok: file.size_ok })),
        total_bytes: model.files.reduce((sum, file) => sum + file.size_bytes, 0),
        installed: Boolean(entry) && complete,
        servable,
        state: servable ? 'ready' : entry ? (complete ? 'incomplete' : 'missing') : 'available',
        source: entry?.source ?? null,
        imported_from: entry?.imported_from ?? null,
        gguf: entry?.gguf ?? null,
        installed_at: entry?.installed_at ?? null,
        estimate: { ...estimate, ...classifyFit(estimate, totalRamBytes) },
        download,
      })
    }

    // 导入的模型不在精选目录里，也要出现在列表与 provider 中。
    for (const [id, entry] of Object.entries(registry.models)) {
      if (models.some((model) => model.id === id)) continue
      const info = await filePresent(entry.files?.[0]?.path)
      const present = Boolean(info)
      models.push({
        id,
        display_name: entry.display_name ?? id,
        description: entry.description ?? '本地导入的 GGUF',
        repo: null,
        revision: null,
        license: entry.license ?? 'unknown',
        tool_calling: entry.tool_calling ?? null,
        context: entry.context ?? { train: 40960, default: 40960, max: 40960, yarn: false },
        files: (entry.files ?? []).map((file) => ({
          path: file.path,
          size_bytes: file.size_bytes,
          present,
          size_ok: Boolean(info) && (!file.size_bytes || info.size === file.size_bytes),
        })),
        total_bytes: entry.files?.reduce((sum, file) => sum + (file.size_bytes ?? 0), 0) ?? 0,
        installed: present,
        servable: present,
        state: present ? 'ready' : 'missing',
        source: 'imported',
        imported_from: entry.imported_from ?? null,
        gguf: entry.gguf ?? null,
        installed_at: entry.installed_at ?? null,
        estimate: entry.estimate ?? null,
        download: null,
      })
    }

    return {
      provider: catalog.provider ?? 'biyou-local',
      models,
      runtime_supported: true,
    }
  }

  /** 可服务模型（按目录顺序）。 */
  async servableModels() {
    const status = await this.status()
    return status.models.filter((model) => model.servable)
  }

  /** 启动下载；已存在暂停任务时自动续传。 */
  async download(id) {
    const model = await this.#requireCatalogModel(id)
    if (await this.isInstalled(id)) return { status: 'completed', id }
    const job = jobId(id)
    const existing = this.#downloader.get(job)
    if (existing?.status === 'paused' || existing?.status === 'failed') {
      return this.#downloader.resume(job)
    }
    if (existing && ['downloading', 'verifying'].includes(existing.status)) return existing
    const files = model.files.map((file) => ({
      url: fileUrl(model, file, this.#mirror),
      dest: this.#modelFilePath(model, file.path),
      size_bytes: file.size_bytes,
      sha256: file.sha256,
    }))
    return this.#downloader.start({ id: job, kind: 'model', title: model.display_name, files })
  }

  pause(id) {
    return this.#downloader.pause(jobId(id))
  }

  resume(id) {
    return this.#downloader.resume(jobId(id))
  }

  async cancel(id) {
    return this.#downloader.cancel(jobId(id))
  }

  /** 导入本地 .gguf：只建链接，不复制；读取头部元数据后入库。 */
  async importPath(inputPath) {
    if (typeof inputPath !== 'string' || inputPath.trim() === '') {
      throw modelError('INVALID_PATH', '请提供 .gguf 文件路径')
    }
    const source = resolve(inputPath.trim())
    if (extname(source).toLowerCase() !== '.gguf') throw modelError('INVALID_PATH', '只支持 .gguf 文件')
    let info
    try {
      info = await stat(source)
    } catch {
      throw modelError('FILE_NOT_FOUND', `文件不存在：${source}`)
    }
    if (!info.isFile()) throw modelError('INVALID_PATH', `不是普通文件：${source}`)
    const real = await realpath(source)
    let header = null
    try {
      header = await readGgufHeader(real)
    } catch (error) {
      this.#logger.warn?.(`[biyou-local-models] 读取 GGUF 头失败：${error.message}`)
    }

    const id = await this.#uniqueImportedId(basename(real, extname(real)))
    const dest = join(this.#paths.models, 'imported', id, 'model.gguf')
    await mkdir(dirname(dest), { recursive: true })
    await linkOrSymlink(real, dest)
    const context = {
      train: header?.contextLength ?? 40960,
      default: header?.contextLength ?? 40960,
      max: header?.contextLength ?? 40960,
      yarn: false,
    }
    const entry = {
      id,
      display_name: header?.name || basename(real, extname(real)),
      description: '本地导入的 GGUF',
      source: 'imported',
      imported_from: real,
      license: 'unknown',
      tool_calling: null,
      context,
      files: [{ path: dest, size_bytes: info.size, sha256: null }],
      gguf: header
        ? {
            architecture: header.architecture,
            quantization: header.quantization,
            block_count: header.blockCount,
            context_length: header.contextLength,
          }
        : null,
      estimate: estimateMemory(
        {
          memory: {
            weights_bytes: info.size,
            kv_bytes_per_1k_ctx: (header?.blockCount ?? 36) * 4096,
            compute_bytes: 402653184,
            overhead_bytes: 402653184,
          },
        },
        context.default,
      ),
      installed_at: new Date().toISOString(),
    }
    const registry = await this.registry()
    registry.models[id] = entry
    await writeJsonAtomic(this.#paths.registry, registry)
    return entry
  }

  /** 移除模型：只清理本插件模型目录里自己的文件，导入源文件不动。 */
  async remove(id) {
    const registry = await this.registry()
    const entry = registry.models[id]
    if (!entry) return false
    delete registry.models[id]
    await writeJsonAtomic(this.#paths.registry, registry)
    if (entry.source === 'imported') {
      await removeQuietly(join(this.#paths.models, 'imported', id))
    } else {
      // 只删这个模型自己的文件；同一 repo 下其它量化档位不受影响。
      for (const file of entry.files ?? []) {
        await removeQuietly(file.path)
        await removeQuietly(`${file.path}.sha256`)
      }
    }
    return true
  }

  /** 写 router 的 preset INI；supervisor 启动前调用。 */
  async writePreset() {
    const models = await this.servableModels()
    await this.registry()
    const lines = []
    for (const model of models) {
      const first = model.files[0]
      if (!first) continue
      const path = this.#presetPath(model.id)
      if (!path) continue
      lines.push(`[${model.id}]`)
      lines.push(`model = ${path.replaceAll('\\', '/')}`)
      lines.push(`alias = ${model.id}`)
      const ctx = Math.max(1024, Number(model.context?.default) || 40960)
      lines.push(`ctx-size = ${ctx}`)
      if (model.context?.yarn && model.context?.train) {
        lines.push('rope-scaling = yarn')
        lines.push(`yarn-orig-ctx = ${model.context.train}`)
      }
      lines.push('jinja = 1')
      lines.push('')
    }
    await writeTextAtomic(this.#paths.modelsPreset, lines.join('\n'), 0o600)
    return models
  }

  /** 某个精确 id 的目录条目；provider 解析请求时用。 */
  async catalogModel(id) {
    const catalog = await this.catalog()
    return catalog.models.find((model) => model.id === id) ?? null
  }

  /** 注册表里某个精确 id 的条目。 */
  async registryEntry(id) {
    return (await this.registry()).models[id] ?? null
  }

  /** 模型是否已安装且文件齐全。 */
  async isInstalled(id) {
    const entry = await this.registryEntry(id)
    if (!entry?.files?.length) return false
    for (const file of entry.files) {
      const info = await filePresent(file.path)
      if (!info) return false
      if (file.size_bytes && info.size !== file.size_bytes) return false
    }
    return true
  }

  /** 第一个分片 / 主文件路径；provider 传给 router 的 id 不变，这里只给 preset 用。 */
  #presetPath(id) {
    const entry = this.#registry?.models?.[id]
    return entry?.files?.[0]?.path ?? null
  }

  async #fileStates(model, entry) {
    return Promise.all(
      model.files.map(async (file) => {
        const dest = this.#modelFilePath(model, file.path)
        const info = await filePresent(dest)
        return {
          path: file.path,
          size_bytes: file.size_bytes,
          present: Boolean(info),
          size_ok: info ? info.size === file.size_bytes : false,
        }
      }),
    )
  }

  async #syncDownloads() {
    if (this.#syncing) return this.#syncing
    this.#syncing = this.#syncDownloadsOnce().finally(() => {
      this.#syncing = null
    })
    return this.#syncing
  }

  async #syncDownloadsOnce() {
    const registry = await this.registry()
    const catalog = await this.catalog()
    let changed = false
    for (const model of catalog.models) {
      if (registry.models[model.id]) continue
      const record = this.#downloader.get(jobId(model.id))
      if (!record || record.status !== 'completed') continue
      const files = []
      for (const file of model.files) {
        const dest = this.#modelFilePath(model, file.path)
        const info = await filePresent(dest)
        if (!info || info.size !== file.size_bytes) {
          files.length = 0
          break
        }
        files.push({ path: dest, size_bytes: file.size_bytes, sha256: file.sha256, verified_at: new Date().toISOString() })
      }
      if (files.length !== model.files.length) continue
      registry.models[model.id] = {
        id: model.id,
        display_name: model.display_name,
        source: 'catalog',
        repo: model.repo,
        revision: model.revision,
        license: model.license,
        tool_calling: model.tool_calling,
        context: model.context,
        files,
        installed_at: new Date().toISOString(),
      }
      changed = true
    }
    if (changed) await writeJsonAtomic(this.#paths.registry, registry)
  }

  async #requireCatalogModel(id) {
    const model = await this.catalogModel(id)
    if (!model) throw modelError('MODEL_NOT_FOUND', `模型不在精选目录：${id}`)
    return model
  }

  #modelFilePath(model, path) {
    const safe = safeSegments(path)
    return join(this.#paths.models, ...model.repo.split('/'), ...safe)
  }

  async #uniqueImportedId(base) {
    const slug = String(base)
      .trim()
      .replace(/[^\w.-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase() || 'imported'
    let id = `imported-${slug}`
    const registry = await this.registry()
    let counter = 2
    while (registry.models[id]) id = `imported-${slug}-${counter++}`
    return id
  }
}

/** 分片完整性：同一前缀的 -00001-of-0000N 必须 1..N 齐全。 */
export function shardComplete(files) {
  const groups = new Map()
  for (const file of files) {
    const match = SHARD_PATTERN.exec(file.path)
    if (!match) continue
    const prefix = file.path.slice(0, match.index)
    const index = Number(match[1])
    const total = Number(match[2])
    const group = groups.get(prefix) ?? { total, seen: new Set() }
    if (group.total !== total) return false
    group.seen.add(index)
    groups.set(prefix, group)
  }
  for (const group of groups.values()) {
    for (let index = 1; index <= group.total; index += 1) {
      if (!group.seen.has(index)) return false
    }
  }
  return true
}

/** HF 文件 URL；使用镜像时由调用方覆盖。 */
export function fileUrl(model, file, mirror = null) {
  const root = mirror ? String(mirror).replace(/\/$/, '') : 'https://huggingface.co'
  return `${root}/${model.repo}/resolve/${model.revision}/${file.path}`
}

export function jobId(id) {
  return `model-${id}`
}

function validateCatalogModel(model) {
  if (!model || typeof model.id !== 'string' || !model.id) throw modelError('INVALID_CATALOG', '模型缺少 id')
  if (typeof model.repo !== 'string' || !/^[\w.-]+\/[\w.-]+$/.test(model.repo)) {
    throw modelError('INVALID_CATALOG', `模型 ${model.id} 的 repo 无效`)
  }
  if (!Array.isArray(model.files) || model.files.length === 0) throw modelError('INVALID_CATALOG', `模型 ${model.id} 没有文件`)
  for (const file of model.files) {
    if (!file.path || typeof file.path !== 'string') throw modelError('INVALID_CATALOG', `模型 ${model.id} 文件路径无效`)
    safeSegments(file.path)
    if (!Number.isFinite(file.size_bytes) || file.size_bytes <= 0) throw modelError('INVALID_CATALOG', `模型 ${model.id} 文件大小无效`)
    if (!/^[a-f0-9]{64}$/i.test(file.sha256)) throw modelError('INVALID_CATALOG', `模型 ${model.id} 文件 sha256 无效`)
  }
  if (!shardComplete(model.files)) throw modelError('INVALID_CATALOG', `模型 ${model.id} 分片不完整`)
}

function safeSegments(path) {
  const normalized = path.replaceAll('\\', '/')
  if (normalized.startsWith('/')) throw modelError('INVALID_PATH', `路径必须是相对路径：${path}`)
  const segments = normalized.split('/').filter(Boolean)
  for (const segment of segments) {
    if (segment === '..' || segment === '.' || segment === '') throw modelError('INVALID_PATH', `路径包含非法段：${path}`)
  }
  return segments
}

async function filePresent(path) {
  if (!path) return null
  try {
    const info = await stat(path)
    return info.isFile() ? info : null
  } catch {
    return null
  }
}

/** 优先符号链接；Windows 无权限时退硬链接；两者都失败就报可操作错误。 */
async function linkOrSymlink(source, dest) {
  try {
    await symlink(source, dest, 'file')
    return
  } catch {
    /* 继续尝试硬链接 */
  }
  try {
    await link(source, dest)
    return
  } catch {
    /* 继续报错 */
  }
  throw modelError(
    'LINK_FAILED',
    '无法创建模型链接（Windows 需要开发者模式或管理员权限）；请把文件移到模型目录后重试',
  )
}

export function modelError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
