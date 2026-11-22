/**
 * 通用下载器：运行时与模型共用。
 *
 * 约定：
 * - 未完成文件只存在于 `cache/partials/<job>/`，校验通过后才 rename 到最终路径；
 * - 支持 Range 续传，服务端不支持 Range 时从头下载；
 * - 暂停/取消都通过 AbortSignal，停在 chunk 边界；
 * - `downloads.json` 记录 job，进程重启后任务以 paused 状态出现，可恢复；
 * - 校验失败的文件不发布，也不写完成标记。
 */
import { createHash } from 'node:crypto'
import { createReadStream, createWriteStream } from 'node:fs'
import { mkdir, readFile, rename, rm, stat, statfs, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import { Readable, Transform } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { readJson, removeQuietly, writeJsonAtomic } from './state.js'

const RETRIES = 3
const PROGRESS_INTERVAL_MS = 150

export class Downloader {
  #paths
  #fetch
  #onChange
  #records = new Map()
  #controllers = new Map()
  #waiters = new Map()
  #persistTimer = null
  #now

  /**
   * @param {{paths: object, fetchImpl?: typeof fetch, onChange?: (records: object[]) => void, now?: () => number}} options
   */
  constructor({ paths, fetchImpl = fetch, onChange = () => {}, now = Date.now }) {
    this.#paths = paths
    this.#fetch = fetchImpl
    this.#onChange = onChange
    this.#now = now
  }

  /** 读取持久化任务；上次没跑完的统一降级为 paused。 */
  async init() {
    const raw = await readJson(this.#paths.downloads, [])
    if (!Array.isArray(raw)) return
    for (const item of raw) {
      if (!item || typeof item.id !== 'string') continue
      const record = {
        ...item,
        status: ['downloading', 'verifying'].includes(item.status) ? 'paused' : item.status,
        updatedAt: item.updatedAt ?? this.#now(),
      }
      this.#records.set(record.id, record)
    }
    this.#emit()
  }

  /** 当前全部任务（浅拷贝，防外部改状态机）。 */
  list() {
    return [...this.#records.values()].map((record) => structuredClone(record))
  }

  /** 取单个任务。 */
  get(id) {
    const record = this.#records.get(id)
    return record ? structuredClone(record) : null
  }

  /**
   * 启动一个下载计划。
   * @param {{id: string, kind?: string, title?: string, files: {url: string, dest: string, size_bytes?: number, sha256: string}[]}} job
   */
  start(job) {
    const existing = this.#records.get(job.id)
    if (existing && ['downloading', 'verifying'].includes(existing.status)) return structuredClone(existing)
    if (!Array.isArray(job.files) || job.files.length === 0) throw downloadError('INVALID_JOB', '下载计划没有文件')
    const record = {
      id: job.id,
      kind: job.kind ?? 'file',
      title: job.title ?? job.id,
      status: 'downloading',
      createdAt: existing?.createdAt ?? this.#now(),
      updatedAt: this.#now(),
      totalBytes: job.files.reduce((sum, file) => sum + (Number(file.size_bytes) || 0), 0),
      receivedBytes: 0,
      files: job.files.map((file, index) => ({
        index,
        url: file.url,
        dest: file.dest,
        size_bytes: Number(file.size_bytes) || 0,
        sha256: String(file.sha256).toLowerCase(),
        receivedBytes: 0,
        status: 'pending',
      })),
      error: null,
    }
    this.#records.set(record.id, record)
    this.#emit()
    this.#kick(record)
    return structuredClone(record)
  }

  /** 暂停；只请求中断，状态在下载循环里落定。 */
  pause(id) {
    const record = this.#records.get(id)
    if (!record || !['downloading', 'verifying'].includes(record.status)) return false
    record.paused = true
    this.#controllers.get(id)?.abort()
    return true
  }

  /** 继续一个 paused / failed / cancelled 的任务。 */
  resume(id) {
    const record = this.#records.get(id)
    if (!record) throw downloadError('JOB_NOT_FOUND', `下载任务不存在：${id}`)
    if (['downloading', 'verifying'].includes(record.status)) return structuredClone(record)
    record.status = 'downloading'
    record.error = null
    record.paused = false
    record.updatedAt = this.#now()
    this.#emit()
    this.#kick(record)
    return structuredClone(record)
  }

  /** 取消并清掉未完成分片。 */
  async cancel(id) {
    const record = this.#records.get(id)
    if (!record) return false
    record.cancelled = true
    this.#controllers.get(id)?.abort()
    record.status = 'cancelled'
    record.updatedAt = this.#now()
    await removeQuietly(join(this.#paths.partials, id))
    this.#emit()
    this.#resolveWaiters(id)
    return true
  }

  /** 插件卸载：中断所有进行中的下载并清掉进度定时器。 */
  dispose() {
    clearTimeout(this.#persistTimer)
    this.#persistTimer = null
    for (const controller of this.#controllers.values()) controller.abort()
  }

  /** 等待任务进入终态（测试与 Host 动作都可用）。 */
  wait(id) {
    const record = this.#records.get(id)
    if (!record) return Promise.resolve(null)
    if (this.#isSettled(record.status)) return Promise.resolve(structuredClone(record))
    return new Promise((resolve) => {
      const waiters = this.#waiters.get(id) ?? []
      waiters.push(() => resolve(structuredClone(this.#records.get(id))))
      this.#waiters.set(id, waiters)
    })
  }

  #isSettled(status) {
    return ['completed', 'failed', 'paused', 'cancelled'].includes(status)
  }

  #kick(record) {
    const controller = new AbortController()
    this.#controllers.set(record.id, controller)
    this.#run(record, controller.signal)
      .catch(() => {
        /* #run 自己落状态；这里只防未处理 rejection */
      })
      .finally(() => {
        this.#controllers.delete(record.id)
        this.#resolveWaiters(record.id)
        this.#emit()
      })
  }

  async #run(record, signal) {
    try {
      await this.#assertDiskSpace(record)
      for (const file of record.files) {
        if (signal.aborted) throw abortError()
        await this.#downloadFile(record, file, signal)
        record.receivedBytes = record.files.reduce((sum, item) => sum + (item.receivedBytes || 0), 0)
      }
      if (signal.aborted) throw abortError()
      record.status = 'completed'
      record.updatedAt = this.#now()
      record.error = null
    } catch (error) {
      record.receivedBytes = record.files.reduce((sum, item) => sum + (item.receivedBytes || 0), 0)
      if (signal.aborted) {
        record.status = record.cancelled ? 'cancelled' : 'paused'
        record.error = null
      } else {
        record.status = 'failed'
        record.error = { code: error.code ?? 'DOWNLOAD_FAILED', message: error.message }
      }
      record.updatedAt = this.#now()
    } finally {
      await this.#persist()
    }
  }

  async #downloadFile(record, file, signal) {
    // 已经完整下载并校验过的文件直接跳过（支持 job 恢复）。
    if (await this.#isPublished(file)) {
      file.receivedBytes = file.size_bytes
      file.status = 'completed'
      return
    }

    const partial = join(this.#paths.partials, record.id, `${file.index}-${basename(file.dest)}`)
    await mkdir(dirname(partial), { recursive: true })
    await mkdir(dirname(file.dest), { recursive: true })

    for (let attempt = 1; attempt <= RETRIES; attempt += 1) {
      try {
        await this.#downloadAttempt(file, partial, signal, (received) => {
          file.receivedBytes = received
          record.receivedBytes = record.files.reduce((sum, item) => sum + (item.receivedBytes || 0), 0)
          this.#throttledEmit(record)
        })
        break
      } catch (error) {
        if (error.name === 'AbortError' || signal.aborted) throw abortError()
        if (error.code === 'CHECKSUM_MISMATCH' || error.code === 'SIZE_MISMATCH') {
          await removeQuietly(partial)
          throw error
        }
        if (attempt === RETRIES) throw error
        await sleep(attempt * 1000, signal)
      }
    }

    const actualSize = (await stat(partial)).size
    if (file.size_bytes > 0 && actualSize !== file.size_bytes) {
      await removeQuietly(partial)
      throw downloadError('SIZE_MISMATCH', `${basename(file.dest)} 大小不符：期望 ${file.size_bytes}，实际 ${actualSize}`)
    }

    record.status = 'verifying'
    record.updatedAt = this.#now()
    this.#emit()
    const actualHash = await hashFile(partial)
    if (file.sha256 && actualHash !== file.sha256.toLowerCase()) {
      await removeQuietly(partial)
      throw downloadError('CHECKSUM_MISMATCH', `${basename(file.dest)} 校验失败：期望 ${file.sha256}，实际 ${actualHash}`)
    }
    if (signal.aborted) throw abortError()

    await rename(partial, file.dest)
    await writeFile(`${file.dest}.sha256`, JSON.stringify({ sha256: actualHash, size: actualSize }), { mode: 0o600 })
    file.receivedBytes = actualSize
    file.status = 'completed'
  }

  async #downloadAttempt(file, partial, signal, onProgress) {
    let offset = 0
    try {
      offset = (await stat(partial)).size
    } catch {
      offset = 0
    }
    if (file.size_bytes > 0 && offset > file.size_bytes) {
      await removeQuietly(partial)
      offset = 0
    }
    if (file.size_bytes > 0 && offset === file.size_bytes) {
      onProgress(offset)
      return
    }

    const headers = { 'accept-encoding': 'identity' }
    if (offset > 0) headers.range = `bytes=${offset}-`
    let response
    try {
      response = await this.#fetch(file.url, { headers, redirect: 'follow', signal })
    } catch (error) {
      if (error.name === 'AbortError' || signal.aborted) throw abortError()
      throw downloadError('NETWORK_ERROR', `网络请求失败：${file.url}（${error.message}）`)
    }
    if (response.status === 416) {
      if (file.size_bytes > 0 && offset === file.size_bytes) return
      await removeQuietly(partial)
      throw downloadError('RANGE_REJECTED', '服务端拒绝了续传范围')
    }
    if (!response.ok) {
      throw downloadError(`HTTP_${response.status}`, `下载失败：HTTP ${response.status} ${file.url}`)
    }

    let append = false
    let startingAt = 0
    if (response.status === 206) {
      append = offset > 0
      startingAt = offset
    } else if (response.status === 200) {
      // 服务端不支持 Range：已有分片作废，从头开始。
      if (offset > 0) await removeQuietly(partial)
      append = false
      startingAt = 0
    } else {
      throw downloadError(`HTTP_${response.status}`, `下载返回了意外状态：${response.status}`)
    }

    if (!response.body) throw downloadError('EMPTY_BODY', '下载响应没有 body')
    let received = startingAt
    const progress = new Transform({
      transform(chunk, _encoding, callback) {
        received += chunk.length
        onProgress(received)
        callback(null, chunk)
      },
    })
    await pipeline(
      Readable.fromWeb(response.body),
      progress,
      createWriteStream(partial, { flags: append ? 'a' : 'w', mode: 0o600 }),
    )
    onProgress(received)
  }

  /** 预留 5% + 64MB 余量；statfs 不可用时不做判断。 */
  async #assertDiskSpace(record) {
    const total = record.files.reduce((sum, file) => sum + (file.size_bytes || 0), 0)
    if (total <= 0) return
    try {
      await mkdir(this.#paths.root, { recursive: true })
      const stats = await statfs(this.#paths.root)
      const free = Number(stats.bavail) * Number(stats.bsize)
      const needed = Math.round(total * 1.05) + 64 * 1024 * 1024
      if (free < needed) {
        throw downloadError(
          'INSUFFICIENT_DISK',
          `磁盘空间不足：需要约 ${(needed / 1024 ** 3).toFixed(1)} GB，可用 ${(free / 1024 ** 3).toFixed(1)} GB`,
        )
      }
    } catch (error) {
      if (error.code === 'INSUFFICIENT_DISK') throw error
      // statfs 在某些文件系统不可用；此时交给写入错误兜底。
    }
  }

  /** 最终文件存在且 size/sidecar 都匹配才算已发布。 */
  async #isPublished(file) {
    try {
      const actual = await stat(file.dest)
      if (file.size_bytes > 0 && actual.size !== file.size_bytes) return false
      const sidecar = JSON.parse(await readFile(`${file.dest}.sha256`, 'utf8'))
      return sidecar.size === actual.size && String(sidecar.sha256).toLowerCase() === file.sha256.toLowerCase()
    } catch {
      return false
    }
  }

  #throttledEmit(record) {
    if (this.#persistTimer) return
    this.#persistTimer = setTimeout(() => {
      this.#persistTimer = null
      this.#emit()
      void this.#persist()
    }, PROGRESS_INTERVAL_MS)
    this.#onChange(this.list())
  }

  #emit() {
    this.#onChange(this.list())
  }

  #resolveWaiters(id) {
    const waiters = this.#waiters.get(id)
    if (!waiters) return
    this.#waiters.delete(id)
    for (const resolve of waiters) resolve()
  }

  async #persist() {
    await writeJsonAtomic(this.#paths.downloads, this.list())
  }
}

/** 流式计算文件 sha256。 */
export function hashFile(file) {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256')
    createReadStream(file)
      .on('data', (chunk) => hash.update(chunk))
      .on('error', reject)
      .on('end', () => resolve(hash.digest('hex')))
  })
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError())
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => {
      clearTimeout(timer)
      reject(abortError())
    }, { once: true })
  })
}

function abortError() {
  const error = new Error('已取消')
  error.name = 'AbortError'
  return error
}

function downloadError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
