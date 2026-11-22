/**
 * 受管 llama.cpp 运行时：下载、校验、解压、激活、发现。
 *
 * 安装只改 `active-runtime.json`，不移动已安装目录；旧版本保留，用户可手动清理。
 * 解压先落到 `cache/extract/`，找到 `llama-server` 后再原子 rename 到
 * `runtimes/<tag>-<backend>/`，失败不会留下半套运行时。
 */
import { spawn } from 'node:child_process'
import { access, chmod, mkdir, readdir, rename } from 'node:fs/promises'
import { constants as fsConstants } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { hashFile } from './downloader.js'
import { candidateTargets, detectGpu, normalizeManifest, selectTarget } from './platform.js'
import { readJson, readText, removeQuietly, writeJsonAtomic } from './state.js'

const MANIFEST_URL = new URL('./manifest.json', import.meta.url)

export class RuntimeManager {
  #paths
  #downloader
  #logger
  #manifest = null
  #platform = process.platform
  #arch = process.arch
  #gpu = null
  #running = null
  #manifestUrl = MANIFEST_URL

  constructor({ paths, downloader, logger = console, manifestUrl = MANIFEST_URL, platform = process.platform, arch = process.arch, gpu = null }) {
    this.#paths = paths
    this.#downloader = downloader
    this.#logger = logger
    this.#manifestUrl = manifestUrl
    this.#platform = platform
    this.#arch = arch
    this.#gpu = gpu
  }

  /** 读取并校验固定清单；清单是唯一版本来源。 */
  async manifest() {
    if (this.#manifest) return this.#manifest
    const raw = JSON.parse(await readText(this.#manifestUrl))
    this.#manifest = normalizeManifest(raw)
    return this.#manifest
  }

  /** 平台/后端探测结果（GPU 探测缓存一次）。 */
  async detection() {
    const manifest = await this.manifest()
    if (this.#gpu === null) this.#gpu = await detectGpu(this.#platform)
    const selection = selectTarget({
      manifest,
      platform: this.#platform,
      arch: this.#arch,
      gpu: this.#gpu,
    })
    return {
      ...selection,
      gpu: this.#gpu,
      engine: manifest.engine,
      tag: manifest.tag,
      backends: [...new Set(candidateTargets(manifest, this.#platform, this.#arch).map((target) => target.backend))],
    }
  }

  /** 运行时状态：探测 + 已安装 + 激活。 */
  async status() {
    const detection = await this.detection()
    const active = await readJson(this.#paths.activeRuntime, null)
    const activeValid = active ? await this.#isValidRuntime(active) : false
    const installed = await this.#listInstalled()
    return {
      platform: detection.platform,
      arch: detection.arch,
      gpu: detection.gpu,
      engine: detection.engine,
      tag: detection.tag,
      supported: detection.supported,
      reason: detection.reason ?? null,
      target: detection.target
        ? {
            key: detection.target.key,
            backend: detection.target.backend,
            version: detection.tag,
            size_bytes: detection.target.size_bytes,
            archive: detection.target.archive,
          }
        : null,
      backends: detection.backends,
      installed,
      active: activeValid ? active : null,
    }
  }

  /** 安装（或复用）本机 target；下载未完成时抛 DOWNLOAD_*，可恢复。 */
  async install({ backend } = {}) {
    if (this.#running) return this.#running
    this.#running = this.#install({ backend }).finally(() => {
      this.#running = null
    })
    return this.#running
  }

  async #install({ backend }) {
    const manifest = await this.manifest()
    const detection = await this.detection()
    const selection = backend
      ? selectTarget({ manifest, platform: this.#platform, arch: this.#arch, gpu: this.#gpu, preferredBackend: backend })
      : detection
    if (!selection.supported || !selection.target) {
      throw runtimeError('UNSUPPORTED_PLATFORM', selection.reason ?? `暂不支持 ${this.#platform}-${this.#arch}`)
    }
    const target = selection.target
    const destination = join(this.#paths.runtimes, `${manifest.tag}-${target.backend}`)
    const existing = await readJson(join(destination, 'runtime.json'), null)
    if (existing && (await this.#isValidRuntime(existing))) {
      await this.#activate(existing)
      return { ...existing, reused: true }
    }

    const archive = join(this.#paths.cache, `${manifest.tag}-${target.backend}-${basename(new URL(target.url).pathname)}`)
    const jobId = `runtime-${manifest.tag}-${target.backend}`
    this.#downloader.start({
      id: jobId,
      kind: 'runtime',
      title: `${manifest.engine} ${manifest.tag} · ${target.backend}`,
      files: [{ url: target.url, dest: archive, size_bytes: target.size_bytes, sha256: target.sha256 }],
    })
    const record = await this.#downloader.wait(jobId)
    if (!record || record.status !== 'completed') {
      throw runtimeError(`DOWNLOAD_${(record?.status ?? 'FAILED').toUpperCase()}`, record?.error?.message ?? '运行时下载未完成')
    }

    const extractRoot = join(this.#paths.extraction, `${jobId}-${Date.now()}`)
    await removeQuietly(extractRoot)
    await this.#extract(archive, extractRoot, target.archive)
    const found = await findRuntimeBinary(extractRoot, target.binary)
    if (!found) {
      await removeQuietly(extractRoot)
      throw runtimeError('BINARY_MISSING', `解压后找不到 ${target.binary}`)
    }
    const installRoot = found.root === extractRoot ? extractRoot : found.root
    await chmod(found.binary, 0o755).catch(() => {})
    await mkdir(this.#paths.runtimes, { recursive: true })
    await removeQuietly(destination)
    const runtime = {
      schema_version: 1,
      engine: manifest.engine,
      tag: manifest.tag,
      backend: target.backend,
      platform: target.platform,
      arch: target.arch,
      binary: relativeTo(installRoot, found.binary),
      source: 'managed',
      sha256: target.sha256,
      installedAt: new Date().toISOString(),
    }
    if (installRoot === extractRoot) {
      await rename(extractRoot, destination)
    } else {
      await rename(installRoot, destination)
      await removeQuietly(extractRoot)
    }
    await writeJsonAtomic(join(destination, 'runtime.json'), runtime)
    await this.#activate(runtime)
    return runtime
  }

  /** 读取激活运行时；失效时退回最新已安装。 */
  async resolve() {
    const active = await readJson(this.#paths.activeRuntime, null)
    if (active && (await this.#isValidRuntime(active))) return this.#runtimeEntry(active)
    const installed = await this.#listInstalled()
    if (installed.length > 0) return this.#runtimeEntry(installed[0])
    const external = await findOnPath('llama-server')
    if (external) return { kind: 'external', command: external, tag: 'external', backend: 'external', binary: external }
    return null
  }

  /** 探测外部 llama-server / Ollama；只用于提示，不自动接管。 */
  async detectExternal() {
    const [pathBinary, server, ollama] = await Promise.all([
      findOnPath('llama-server'),
      probeLlamaServer('http://127.0.0.1:8080'),
      probeOllama('http://127.0.0.1:11434'),
    ])
    return { pathBinary, server, ollama }
  }

  async #listInstalled() {
    const runtimes = []
    let entries = []
    try {
      entries = await readdir(this.#paths.runtimes, { withFileTypes: true })
    } catch {
      return runtimes
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const record = await readJson(join(this.#paths.runtimes, entry.name, 'runtime.json'), null)
      if (record && (await this.#isValidRuntime(record))) runtimes.push(record)
    }
    runtimes.sort((a, b) => String(b.installedAt).localeCompare(String(a.installedAt)))
    return runtimes
  }

  async #activate(runtime) {
    await writeJsonAtomic(this.#paths.activeRuntime, { ...runtime, activatedAt: new Date().toISOString() })
  }

  async #isValidRuntime(record) {
    if (!record || typeof record.binary !== 'string') return false
    const root = record.source === 'managed'
      ? join(this.#paths.runtimes, `${record.tag}-${record.backend}`)
      : dirname(record.binary)
    const binary = record.binary.startsWith('/') ? record.binary : join(root, record.binary)
    try {
      await access(binary, fsConstants.X_OK)
      return true
    } catch {
      return false
    }
  }

  #runtimeEntry(record) {
    const root = join(this.#paths.runtimes, `${record.tag}-${record.backend}`)
    const binary = record.binary.startsWith('/') ? record.binary : join(root, record.binary)
    return { ...record, kind: 'managed', command: binary, root }
  }

  /** 解压：tar.gz 用 tar；zip 在 Windows 用 Expand-Archive，其它平台用 unzip。 */
  async #extract(archive, destination, type) {
    await removeQuietly(destination)
    await mkdir(destination, { recursive: true })
    if (type === 'tar.gz') {
      await runCommand('tar', ['-xzf', archive, '-C', destination])
      return
    }
    if (this.#platform === 'win32') {
      await runCommand('powershell', [
        '-NoProfile', '-NonInteractive', '-Command',
        `Expand-Archive -LiteralPath "${archive.replaceAll('"', '`"')}" -DestinationPath "${destination.replaceAll('"', '`"')}" -Force`,
      ])
      return
    }
    await runCommand('unzip', ['-q', '-o', archive, '-d', destination])
  }
}

/** 递归查找运行时二进制；最多 3 层，避免扫整棵目录。 */
export async function findRuntimeBinary(root, binaryName, depth = 0) {
  const target = process.platform === 'win32' && !binaryName.endsWith('.exe') ? `${binaryName}.exe` : binaryName
  let entries = []
  try {
    entries = await readdir(root, { withFileTypes: true })
  } catch {
    return null
  }
  for (const entry of entries) {
    const path = join(root, entry.name)
    if (entry.isFile() && entry.name === target) return { binary: path, root }
  }
  if (depth >= 3) return null
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const found = await findRuntimeBinary(join(root, entry.name), binaryName, depth + 1)
    if (found) return found
  }
  return null
}

/** 在 PATH 中查找可执行文件，Windows 按 PATHEXT 补扩展名。 */
export async function findOnPath(name) {
  const path = process.env.PATH ?? ''
  const separator = process.platform === 'win32' ? ';' : ':'
  const extensions = process.platform === 'win32'
    ? (process.env.PATHEXT ?? '.EXE;.CMD;.BAT').split(';').filter(Boolean)
    : ['']
  for (const dir of path.split(separator)) {
    if (!dir) continue
    for (const extension of extensions) {
      const candidate = join(dir, `${name}${extension.toLowerCase()}`)
      try {
        await access(candidate, fsConstants.X_OK)
        return candidate
      } catch {
        /* 继续找 */
      }
    }
  }
  return null
}

/** 通过 /props 识别真实 llama-server：有 build_info 且 role 为 router 或 model。 */
export async function probeLlamaServer(baseUrl, timeoutMs = 1200) {
  try {
    const response = await fetch(`${baseUrl.replace(/\/$/, '')}/props`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) return null
    const data = await response.json()
    if (typeof data?.build_info !== 'string' && typeof data?.model_path !== 'string') return null
    if (data?.role && data.role !== 'router' && data.role !== 'model') return null
    return { baseUrl, role: data.role ?? 'model', build: data.build_info ?? null, modelPath: data.model_path ?? null }
  } catch {
    return null
  }
}

/** 探测 Ollama；只返回标签，不注册 provider。 */
export async function probeOllama(baseUrl, timeoutMs = 1200) {
  try {
    const response = await fetch(`${baseUrl.replace(/\/$/, '')}/api/tags`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) return null
    const data = await response.json()
    const models = Array.isArray(data?.models) ? data.models.map((model) => model?.name).filter(Boolean) : []
    return { baseUrl, models }
  } catch {
    return null
  }
}

/** 读取日志尾部；失败返回空数组。 */
export async function readLogTail(file, lines = 40) {
  try {
    const handle = await import('node:fs/promises').then((fs) => fs.open(file, 'r'))
    try {
      const info = await handle.stat()
      const length = Math.min(info.size, 64 * 1024)
      const buffer = Buffer.alloc(length)
      await handle.read(buffer, 0, length, info.size - length)
      return buffer.toString('utf8').split(/\r?\n/).filter(Boolean).slice(-lines)
    } finally {
      await handle.close()
    }
  } catch {
    return []
  }
}

/** 流式哈希（供外部校验已存在的归档）。 */
export { hashFile }

export function runtimeError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}

function relativeTo(root, path) {
  return path.startsWith(root) ? path.slice(root.length).replace(/^[\\/]/, '') : path
}

function runCommand(command, args, timeoutMs = 120_000) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
    let stderr = ''
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      reject(runtimeError('EXTRACT_TIMEOUT', `解压超时：${command}`))
    }, timeoutMs)
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf8')
    })
    child.on('error', (error) => {
      clearTimeout(timer)
      reject(runtimeError('EXTRACT_FAILED', `无法执行 ${command}：${error.message}`))
    })
    child.on('exit', (code) => {
      clearTimeout(timer)
      if (code === 0) resolve()
      else reject(runtimeError('EXTRACT_FAILED', `${command} 解压失败（${code}）：${stderr.slice(-500)}`))
    })
  })
}
