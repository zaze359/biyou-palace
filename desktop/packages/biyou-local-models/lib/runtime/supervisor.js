/**
 * llama-server（router 模式）生命周期。
 *
 * 一个插件实例只托管一个 router：模型由 `--models-preset` 描述，按需加载；
 * 空闲时调用 `/models/unload` 卸载模型而不是停 router；崩溃按 1/5/15/60 秒
 * 退避重启；退出/热重载时连同子进程组一起清理。API key 只进 key 文件与
 * Authorization 头，不写日志、不回前端。
 */
import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { createWriteStream } from 'node:fs'
import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises'
import { randomBytes } from 'node:crypto'
import { createServer } from 'node:net'
import { basename } from 'node:path'
import { readJson, removeQuietly, writeJsonAtomic } from './state.js'
import { readLogTail } from './runtime.js'
import { attributionHeaders } from './attribution.js'

const DEFAULT_PORT = 18434
const PORT_CANDIDATES = 10
const READY_TIMEOUT_MS = 180_000
const LOAD_TIMEOUT_MS = 180_000
const RESTART_DELAYS = [1000, 5000, 15000, 60000]
const LOG_TAIL_LINES = 40

export class Supervisor extends EventEmitter {
  #paths
  #runtime
  #models
  #config
  #logger
  #child = null
  #adopted = false
  #pid = null
  #port = null
  #baseUrl = null
  #key = null
  #state = 'stopped'
  #loadedModel = null
  #lastUsedAt = 0
  #startedAt = null
  #error = null
  #stopping = false
  #restartAttempt = 0
  #restartTimer = null
  #idleTimer = null
  #logStream = null
  #ensure = null

  constructor({ paths, runtime, models, config = {}, logger = console }) {
    super()
    this.#paths = paths
    this.#runtime = runtime
    this.#models = models
    this.#config = {
      port: DEFAULT_PORT,
      idleUnloadMs: 15 * 60 * 1000,
      idleCheckMs: 30_000,
      ...config,
    }
    this.#logger = logger
  }

  get state() {
    return this.#state
  }

  /** 只读状态；永远不返回 API key。 */
  async status() {
    const tail = await readLogTail(this.#paths.serverLog, LOG_TAIL_LINES)
    return {
      state: this.#state,
      port: this.#port,
      base_url: this.#baseUrl,
      pid: this.#pid,
      loaded_model: this.#loadedModel,
      started_at: this.#startedAt,
      error: this.#error,
      log_tail: tail,
    }
  }

  /** 启动前的接管判定：状态文件里的进程还能用就接管，否则清理。 */
  async init() {
    await mkdir(this.#paths.state, { recursive: true })
    await mkdir(this.#paths.logs, { recursive: true })
    await this.#loadKey()
    const previous = await readJson(this.#paths.server, null)
    if (!previous?.pid || !isPidAlive(previous.pid)) {
      if (previous) await removeQuietly(this.#paths.server)
      return
    }
    const ok = await probeRouter(previous.base_url, this.#key)
    if (ok) {
      this.#adopted = true
      this.#pid = previous.pid
      this.#port = previous.port
      this.#baseUrl = previous.base_url
      this.#state = 'idle'
      this.#startedAt = previous.started_at ?? null
      const models = await this.#routerModels().catch(() => [])
      const loaded = models.find((model) => model?.status?.value === 'loaded')
      if (loaded) {
        this.#loadedModel = loaded.id
        this.#state = 'ready'
        this.#lastUsedAt = Date.now()
      }
      this.#startIdleTimer()
      this.emit('status', await this.status())
      return
    }
    await killTree(previous.pid)
    await removeQuietly(this.#paths.server)
  }

  /** 启动 router；已就绪则直接返回。 */
  async start() {
    if (this.#child || this.#adopted) {
      if (this.#state === 'starting' || this.#state === 'loading') return this.status()
      if (this.#state === 'ready' || this.#state === 'idle') {
        if (await probeRouter(this.#baseUrl, this.#key)) return this.status()
      }
      // 进程还在但状态不可信（probe 失败/上次失败）：先彻底停掉再重来。
      await this.stop()
    }
    this.#stopping = false
    this.#error = null
    this.#state = 'starting'
    this.emit('status', await this.status())

    if (this.#config.externalBaseUrl) {
      try {
        await this.#useExternal(this.#config.externalBaseUrl)
      } catch (error) {
        this.#fail(error.code ?? 'EXTERNAL_FAILED', error.message)
      }
      return this.status()
    }

    const runtime = await this.#runtime.resolve()
    if (!runtime) {
      this.#fail('NO_RUNTIME', '没有可用的 llama-server 运行时；请先安装运行时')
      return this.status()
    }
    if (!this.#key) await this.#loadKey()
    await this.#models.writePreset()
    this.#port = await findFreePort(this.#config.port)
    this.#baseUrl = `http://127.0.0.1:${this.#port}`
    const args = [
      '--host', '127.0.0.1',
      '--port', String(this.#port),
      '--models-preset', this.#paths.modelsPreset,
      '--models-max', '1',
      '--no-webui',
      '--api-key-file', this.#paths.apiKey,
    ]
    await mkdir(this.#paths.logs, { recursive: true })
    this.#logStream = createWriteStream(this.#paths.serverLog, { flags: 'a', mode: 0o600 })
    this.#logStream.write(`\n[${new Date().toISOString()}] spawn ${basename(runtime.command)}\n`)

    // 不脱离父进程组：桌面端停止 dsh 时按进程组发信号，能一并带走 router。
    // 插件自己的 stop() 只杀 router pid，router 负责清理它拉起的模型子进程。
    const child = spawn(runtime.command, args, {
      cwd: this.#paths.root,
      detached: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.#child = child
    this.#pid = child.pid ?? null
    child.stdout.on('data', (chunk) => this.#logStream?.write(chunk))
    child.stderr.on('data', (chunk) => this.#logStream?.write(chunk))
    child.on('error', (error) => {
      this.#fail('SPAWN_FAILED', `无法启动 llama-server：${error.message}`)
    })
    child.on('exit', (code, signal) => this.#onExit(code, signal))

    this.#startedAt = new Date().toISOString()
    await this.#persistState()
    this.emit('status', await this.status())

    try {
      await this.#waitForRouter(READY_TIMEOUT_MS)
      this.#state = 'idle'
      this.#restartAttempt = 0
      this.#startIdleTimer()
      this.emit('status', await this.status())
      return this.status()
    } catch (error) {
      this.#fail(error.code ?? 'START_FAILED', error.message)
      await this.stop({ keepError: true })
      this.#state = 'error'
      this.#error = `${error.code ?? 'START_FAILED'}: ${error.message}`
      return this.status()
    }
  }

  /** 确保指定模型已加载，并用一次真实生成确认 ready。 */
  async ensureModel(modelId, signal) {
    await this.start()
    if (this.#state === 'error') {
      throw supervisorError('SERVER_NOT_READY', this.#error ?? '本地服务未就绪')
    }
    if (this.#loadedModel === modelId) {
      this.touch()
      return
    }
    while (this.#ensure) {
      await this.#ensure.catch(() => {})
      if (this.#loadedModel === modelId) {
        this.touch()
        return
      }
    }
    this.#ensure = this.#loadModel(modelId, signal).finally(() => {
      this.#ensure = null
    })
    await this.#ensure
  }

  async #loadModel(modelId, signal) {
    this.#state = 'loading'
    this.#error = null
    this.emit('status', await this.status())
    try {
      if (this.#loadedModel) await this.#unloadModel(this.#loadedModel).catch(() => {})
      // preset 是启动时读取的：模型刚下载/导入后 router 还不认识它，先重启一次。
      const known = await this.#routerModels().catch(() => [])
      if (!known.some((model) => model?.id === modelId)) {
        if (!(await this.#models.isInstalled(modelId))) {
          throw supervisorError('MODEL_NOT_FOUND', `模型未安装：${modelId}`)
        }
        await this.stop()
        await this.start()
        this.#state = 'loading'
      }
      const loadResponse = await this.#authFetch('/models/load', {
        method: 'POST',
        body: JSON.stringify({ model: modelId }),
        signal,
      })
      if (!loadResponse.ok) {
        const detail = await loadResponse.text().catch(() => '')
        throw supervisorError(`HTTP_${loadResponse.status}`, `模型加载请求失败：${detail.slice(0, 300)}`)
      }
      await this.#waitForLoaded(modelId, LOAD_TIMEOUT_MS, signal)
      await this.#probeGeneration(modelId, signal)
      this.#loadedModel = modelId
      this.#state = 'ready'
      this.#lastUsedAt = Date.now()
      await this.#persistState()
      this.emit('status', await this.status())
    } catch (error) {
      this.#state = 'error'
      this.#error = error.message
      this.emit('status', await this.status())
      throw error
    }
  }

  /** 请求入口：确保模型加载后返回原始 fetch Response，由 provider 解析 SSE。 */
  async chat(body, signal) {
    await this.ensureModel(body.model, signal)
    this.touch()
    return this.#authFetch('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    })
  }

  /** 记录一次真实使用；空闲卸载只看这个时间。 */
  touch() {
    this.#lastUsedAt = Date.now()
    if (this.#state === 'idle') this.#state = 'ready'
  }

  /** 停止 router；默认保留 error 供 UI 展示。 */
  async stop({ keepError = false } = {}) {
    this.#stopping = true
    clearTimeout(this.#restartTimer)
    clearInterval(this.#idleTimer)
    this.#restartTimer = null
    this.#idleTimer = null
    this.#restartAttempt = 0
    const pid = this.#pid
    const child = this.#child
    this.#child = null
    this.#adopted = false
    const exited = child ? waitChildExit(child, 5000) : Promise.resolve()
    if (pid) await killTree(pid, child)
    await exited
    this.#pid = null
    this.#port = null
    this.#baseUrl = null
    this.#loadedModel = null
    this.#startedAt = null
    this.#state = 'stopped'
    if (!keepError) this.#error = null
    this.#logStream?.end()
    this.#logStream = null
    await removeQuietly(this.#paths.server)
    this.#stopping = false
    this.emit('status', await this.status())
  }

  /** 插件卸载时调用：停服并清掉子进程组。 */
  async dispose() {
    clearTimeout(this.#restartTimer)
    clearInterval(this.#idleTimer)
    await this.stop()
  }

  async #useExternal(baseUrl) {
    const parsed = new URL(baseUrl)
    if (!['127.0.0.1', 'localhost', '::1'].includes(parsed.hostname)) {
      throw supervisorError('INVALID_EXTERNAL', '外部服务只允许回环地址')
    }
    this.#baseUrl = parsed.origin
    this.#port = Number(parsed.port) || 80
    const ok = await probeRouter(this.#baseUrl, this.#key)
    if (!ok) throw supervisorError('EXTERNAL_UNREACHABLE', `外部 llama-server 不可达：${this.#baseUrl}`)
    this.#state = 'idle'
    this.#startIdleTimer()
    await this.#persistState()
  }

  async #waitForRouter(timeoutMs) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (this.#state === 'error') throw supervisorError('SPAWN_FAILED', this.#error ?? 'llama-server 退出')
      if (await probeRouter(this.#baseUrl, this.#key)) return
      await sleep(500)
    }
    throw supervisorError('READY_TIMEOUT', `llama-server 在 ${Math.round(timeoutMs / 1000)} 秒内未就绪`)
  }

  async #waitForLoaded(modelId, timeoutMs, signal) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (signal?.aborted) throw abortError()
      if (this.#state === 'error') throw supervisorError('SERVER_NOT_READY', this.#error ?? '服务未就绪')
      const models = await this.#routerModels().catch((error) => {
        if (error.code === 'UNAUTHORIZED') throw error
        return []
      })
      const entry = models.find((model) => model?.id === modelId)
      if (entry?.status?.value === 'loaded') return
      await sleep(500)
    }
    throw supervisorError('LOAD_TIMEOUT', `模型 ${modelId} 在 ${Math.round(timeoutMs / 1000)} 秒内未加载完成`)
  }

  async #probeGeneration(modelId, signal) {
    const response = await this.#authFetch('/v1/chat/completions', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        model: modelId,
        messages: [{ role: 'user', content: 'Reply with exactly: OK' }],
        max_tokens: 16,
        temperature: 0,
        // Qwen3 等默认开启 thinking；探针需要可见文本，显式关掉更稳。
        chat_template_kwargs: { enable_thinking: false },
      }),
    })
    if (!response.ok) throw supervisorError(`HTTP_${response.status}`, `就绪探针失败：HTTP ${response.status}`)
    const data = await response.json()
    const text = String(data?.choices?.[0]?.message?.content ?? '')
    if (!/\bok\b/i.test(text)) {
      throw supervisorError('READINESS_FAILED', `就绪探针没有返回预期内容：${text.slice(0, 80) || '(空)'}`)
    }
  }

  async #unloadModel(modelId) {
    await this.#authFetch('/models/unload', {
      method: 'POST',
      body: JSON.stringify({ model: modelId }),
    })
  }

  async #routerModels() {
    const response = await this.#authFetch('/v1/models', { method: 'GET' })
    if (!response.ok) return []
    const data = await response.json()
    return Array.isArray(data?.data) ? data.data : []
  }

  async #authFetch(path, init = {}) {
    const headers = { authorization: `Bearer ${this.#key}`, ...attributionHeaders() }
    if (init.body) headers['content-type'] = 'application/json'
    const response = await fetch(`${this.#baseUrl}${path}`, {
      ...init,
      headers: { ...headers, ...(init.headers ?? {}) },
    })
    if (response.status === 401 || response.status === 403) {
      const error = supervisorError('UNAUTHORIZED', '本地服务鉴权失败，请重启插件')
      error.status = response.status
      throw error
    }
    return response
  }

  #onExit(code, signal) {
    if (this.#stopping) return
    this.#child = null
    this.#pid = null
    if (this.#state === 'stopped') return
    this.#loadedModel = null
    const reason = `llama-server 退出（code=${code ?? 'null'} signal=${signal ?? 'null'}）`
    this.#error = reason
    this.#state = 'error'
    this.emit('status', this.status())
    if (this.#restartAttempt >= RESTART_DELAYS.length) {
      this.#logger.error?.(`[biyou-local-models] ${reason}；重启次数已用尽`)
      return
    }
    const delay = RESTART_DELAYS[this.#restartAttempt++]
    this.#logger.warn?.(`[biyou-local-models] ${reason}；${delay}ms 后重启`)
    clearTimeout(this.#restartTimer)
    this.#restartTimer = setTimeout(() => {
      this.#restartTimer = null
      if (this.#stopping) return
      this.#state = 'stopped'
      void this.start().catch((error) => this.#fail(error.code ?? 'RESTART_FAILED', error.message))
    }, delay)
  }

  #fail(code, message) {
    this.#state = 'error'
    this.#error = `${code}: ${message}`
    this.emit('status', this.status())
  }

  #startIdleTimer() {
    clearInterval(this.#idleTimer)
    const idleMs = Number(this.#config.idleUnloadMs) || 15 * 60 * 1000
    this.#idleTimer = setInterval(() => {
      void this.#checkIdle(idleMs).catch(() => {})
    }, Number(this.#config.idleCheckMs) || 30_000)
    this.#idleTimer.unref?.()
  }

  async #checkIdle(idleMs) {
    if (this.#state !== 'ready') return
    if (!this.#loadedModel || Date.now() - this.#lastUsedAt < idleMs) return
    const model = this.#loadedModel
    await this.#unloadModel(model).catch(() => {})
    this.#loadedModel = null
    this.#state = 'idle'
    await this.#persistState()
    this.emit('status', await this.status())
  }

  async #loadKey() {
    try {
      const existing = (await readFile(this.#paths.apiKey, 'utf8')).trim()
      if (existing.length >= 32) {
        this.#key = existing
        await chmod(this.#paths.apiKey, 0o600).catch(() => {})
        return
      }
    } catch {
      /* 文件不存在或不可读，生成新的 */
    }
    this.#key = randomBytes(32).toString('hex')
    await mkdir(this.#paths.state, { recursive: true })
    await writeFile(this.#paths.apiKey, `${this.#key}\n`, { mode: 0o600 })
    await chmod(this.#paths.apiKey, 0o600).catch(() => {})
  }

  async #persistState() {
    await writeJsonAtomic(this.#paths.server, {
      pid: this.#pid,
      port: this.#port,
      base_url: this.#baseUrl,
      loaded_model: this.#loadedModel,
      started_at: this.#startedAt,
      state: this.#state,
    })
  }

}

/** 端口探测：优先 18434，被占用就顺序试 10 个，最后交给系统分配。 */
export async function findFreePort(preferred = DEFAULT_PORT) {
  for (let offset = 0; offset < PORT_CANDIDATES; offset += 1) {
    const candidate = preferred + offset
    if (await isPortFree(candidate)) return candidate
  }
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(() => resolve(port))
    })
  })
}

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = createServer()
    server.unref()
    server.once('error', () => resolve(false))
    server.listen(port, '127.0.0.1', () => server.close(() => resolve(true)))
  })
}

async function probeRouter(baseUrl, key) {
  if (!baseUrl) return false
  try {
    const response = await fetch(`${baseUrl}/props`, {
      signal: AbortSignal.timeout(1500),
      headers: { authorization: `Bearer ${key ?? ''}` },
    })
    if (!response.ok) return false
    const data = await response.json()
    return data?.role === 'router' || typeof data?.build_info === 'string'
  } catch {
    return false
  }
}

function isPidAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

/** 按进程组清理；Windows 用 taskkill /T。 */
async function killTree(pid, child = null) {
  if (!pid) return
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
    } else if (child) {
      child.kill('SIGTERM')
    } else {
      process.kill(pid, 'SIGTERM')
    }
  } catch {
    /* 进程已经不在 */
  }
  if (child) await waitChildExit(child, 3000)
  else await waitPidGone(pid, 3000)
  if (!isPidAlive(pid)) return
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
    } else {
      process.kill(pid, 'SIGKILL')
    }
  } catch {
    /* 进程已经不在 */
  }
}

function waitPidGone(pid, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const tick = () => {
      if (!isPidAlive(pid) || Date.now() >= deadline) return resolve()
      setTimeout(tick, 50)
    }
    tick()
  })
}

function waitChildExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve()
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs)
    child.once('exit', () => {
      clearTimeout(timer)
      resolve()
    })
  })
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function abortError() {
  const error = new Error('已取消')
  error.name = 'AbortError'
  return error
}

export function supervisorError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
