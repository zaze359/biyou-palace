/**
 * dsh 要求的 provider 归属头。这里不 import dsh-llm（插件不从 profile 解析
 * 包路径），只复刻同一个 `User-Agent` 形状；版本从当前进程的 dsh 入口读取，
 * 读不到时回退到锁定版本。
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const FALLBACK_VERSION = '0.2.2-rc.5'
let cachedVersion = null

export function dshVersion() {
  if (cachedVersion) return cachedVersion
  const candidates = []
  const bin = process.argv[1]
  if (bin) candidates.push(join(dirname(bin), '..', 'package.json'))
  try {
    candidates.push(join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'package.json'))
  } catch {
    /* 忽略 */
  }
  for (const file of candidates) {
    try {
      const manifest = JSON.parse(readFileSync(file, 'utf8'))
      if (manifest?.name === '@deepseek-ai/dsh' || manifest?.name === 'dsh') {
        cachedVersion = manifest.version
        return cachedVersion
      }
    } catch {
      /* 继续找 */
    }
  }
  cachedVersion = FALLBACK_VERSION
  return cachedVersion
}

/** 与 dsh-llm 的 attributionHeaders() 同形：小写 user-agent。 */
export function attributionHeaders() {
  const suffix = process.env.DSH_USER_AGENT_SUFFIX
  const base = `deepseek-harness/${dshVersion()} (+https://github.com/deepseek-ai/deepseek-harness)`
  const value = suffix && /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(suffix) ? `${base} ${suffix}` : base
  return { 'user-agent': value }
}
