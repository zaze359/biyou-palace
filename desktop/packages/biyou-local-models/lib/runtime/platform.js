/**
 * 平台/架构/后端选择。
 *
 * 清单里的 target 是唯一事实来源：本机组合没有 target 就明确拒绝，不猜、
 * 不运行时抓 latest。后端选择只做保守推断，用户可以通过插件配置覆盖。
 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

/** 后端优先级：越靠前越优先。 */
const BACKEND_ORDER = ['metal', 'cuda', 'vulkan', 'cpu']

/** 清单结构校验；返回规范化后的 targets 数组。 */
export function normalizeManifest(raw) {
  if (!raw || typeof raw !== 'object') throw manifestError('manifest 不是对象')
  if (raw.schema_version !== 1) throw manifestError(`不支持的 manifest schema：${raw.schema_version}`)
  if (typeof raw.engine !== 'string' || !raw.engine) throw manifestError('manifest.engine 缺失')
  if (typeof raw.tag !== 'string' || !raw.tag) throw manifestError('manifest.tag 缺失')
  if (!raw.targets || typeof raw.targets !== 'object') throw manifestError('manifest.targets 缺失')

  const targets = Object.entries(raw.targets).map(([key, target]) => {
    const normalized = {
      key,
      platform: target?.platform,
      arch: target?.arch,
      backend: target?.backend,
      url: target?.url,
      sha256: target?.sha256,
      size_bytes: Number(target?.size_bytes),
      archive: target?.archive,
      binary: target?.binary,
    }
    for (const field of ['platform', 'arch', 'backend', 'url', 'sha256', 'archive', 'binary']) {
      if (typeof normalized[field] !== 'string' || normalized[field].length === 0) {
        throw manifestError(`target ${key} 缺少 ${field}`)
      }
    }
    if (!Number.isFinite(normalized.size_bytes) || normalized.size_bytes <= 0) {
      throw manifestError(`target ${key} 的 size_bytes 无效`)
    }
    if (!/^[a-f0-9]{64}$/i.test(normalized.sha256)) throw manifestError(`target ${key} 的 sha256 无效`)
    if (!['tar.gz', 'zip'].includes(normalized.archive)) throw manifestError(`target ${key} 的 archive 类型不支持`)
    return normalized
  })

  if (targets.length === 0) throw manifestError('manifest.targets 为空')
  return { schemaVersion: raw.schema_version, engine: raw.engine, tag: raw.tag, targets }
}

/** 本机 platform/arch 的候选 target，按后端优先级排序。 */
export function candidateTargets(manifest, platform = process.platform, arch = process.arch) {
  return manifest.targets
    .filter((target) => target.platform === platform && target.arch === arch)
    .sort((a, b) => BACKEND_ORDER.indexOf(a.backend) - BACKEND_ORDER.indexOf(b.backend))
}

/**
 * 选择本机 target。
 * @param {{manifest: object, platform?: string, arch?: string, gpu?: string, preferredBackend?: string}} input
 * @returns {{target: object|null, platform: string, arch: string, supported: boolean, reason?: string, candidates: object[]}}
 */
export function selectTarget({ manifest, platform = process.platform, arch = process.arch, gpu = 'unknown', preferredBackend } = {}) {
  const candidates = candidateTargets(manifest, platform, arch)
  const base = { platform, arch, supported: candidates.length > 0, candidates }
  if (candidates.length === 0) {
    return {
      ...base,
      target: null,
      reason: `暂不支持 ${platform}-${arch}；可用组合：${[...new Set(manifest.targets.map((t) => `${t.platform}-${t.arch}`))].join('、')}`,
    }
  }

  if (preferredBackend) {
    const explicit = candidates.find((target) => target.backend === preferredBackend)
    if (!explicit) {
      return {
        ...base,
        target: null,
        reason: `${platform}-${arch} 没有 ${preferredBackend} 后端；可选：${candidates.map((t) => t.backend).join('、')}`,
      }
    }
    return { ...base, target: explicit }
  }

  if (platform === 'win32') {
    const desired = gpu === 'nvidia' ? 'cuda' : gpu === 'amd' || gpu === 'intel' ? 'vulkan' : 'cpu'
    return { ...base, target: candidates.find((target) => target.backend === desired) ?? candidates[0] }
  }
  return { ...base, target: candidates[0] }
}

/** 探测 Windows 显卡厂商；失败一律 unknown，不阻塞安装。 */
export async function detectGpu(platform = process.platform, run = execFileAsync) {
  if (platform !== 'win32') return 'unknown'
  try {
    await run('nvidia-smi', ['--query-gpu=name', '--format=csv,noheader'], { timeout: 4000, windowsHide: true })
    return 'nvidia'
  } catch {
    /* 没有 NVIDIA 驱动，继续看通用显卡列表 */
  }
  try {
    const { stdout } = await run(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', '(Get-CimInstance Win32_VideoController).Name'],
      { timeout: 6000, windowsHide: true },
    )
    const text = String(stdout).toLowerCase()
    if (/nvidia/.test(text)) return 'nvidia'
    if (/amd|radeon/.test(text)) return 'amd'
    if (/intel/.test(text)) return 'intel'
  } catch {
    /* 探测失败按未知处理 */
  }
  return 'unknown'
}

/**
 * 保守估算模型内存占用。首版只用于排序与提示，启动后以 llama-server 实际日志为准。
 * @param {object} model catalog 条目
 * @param {number} ctx 目标上下文
 */
export function estimateMemory(model, ctx) {
  const memory = model.memory ?? {}
  const weights = Number(memory.weights_bytes) || 0
  const kv = Math.round((Number(memory.kv_bytes_per_1k_ctx) || 0) * (ctx / 1000))
  const compute = Number(memory.compute_bytes) || 0
  const overhead = Number(memory.overhead_bytes) || 0
  return { totalBytes: weights + kv + compute + overhead, weightsBytes: weights, kvBytes: kv, computeBytes: compute, overheadBytes: overhead }
}

/** 依据总内存给出可运行性判断；不区分显存与系统内存是首版的有意保守。 */
export function classifyFit(estimate, totalRamBytes) {
  if (!totalRamBytes) return { fit: 'unknown', ratio: null }
  const ratio = estimate.totalBytes / totalRamBytes
  if (ratio <= 0.7) return { fit: 'ram', ratio }
  if (ratio <= 0.9) return { fit: 'tight', ratio }
  return { fit: 'no', ratio }
}

function manifestError(message) {
  const error = new Error(`运行时清单无效：${message}`)
  error.code = 'INVALID_MANIFEST'
  return error
}
