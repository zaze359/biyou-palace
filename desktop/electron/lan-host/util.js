/**
 * 联阵协议公共工具：常量、时间、地址判断、令牌与阵符哈希。
 *
 * 协议基准：docs/lan-formation-contract.md
 */
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import { networkInterfaces, platform as osPlatform } from 'node:os'
import { argon2idAsync } from '@noble/hashes/argon2.js'

export const PROTOCOL = 1
export const TOKEN_BYTES = 32

export const DEFAULTS = Object.freeze({
  httpPort: 4022,
  discoveryPort: 4021,
  capacity: 8,
  leaseTtlSeconds: 30,
  heartbeatIntervalSeconds: 10,
  rateLimitAttempts: 5,
  rateLimitWindowMs: 60_000,
  announceIntervalMs: 2_000,
  expirySweepMs: 1_000,
  keepAliveMs: 15_000,
  maxBodyBytes: 1_000_000,
})

// 闭阵只存 Argon2id 哈希。参数按 OWASP「Argon2id 19 MiB / t=2 / p=1」取最小档，
// 单次校验约 0.2~0.4 秒；桌面端开阵一次、入阵一次，可以接受。
const ARGON2 = Object.freeze({ t: 2, m: 19_456, p: 1, dkLen: 32 })

/** RFC 3339 UTC，秒级精度（与契约示例一致） */
export function utcNowIso(now = Date.now()) {
  return new Date(now).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

export function errorBody(code, message) {
  return { ok: false, error: { code, message } }
}

export function sha256Hex(value) {
  return createHash('sha256').update(String(value), 'utf8').digest('hex')
}

/** 长度不同的字符串直接判否；等长时用恒定时间比较，避免时序泄漏 */
export function safeEqual(left, right) {
  const a = Buffer.from(String(left ?? ''), 'utf8')
  const b = Buffer.from(String(right ?? ''), 'utf8')
  if (a.length !== b.length) return false
  return timingSafeEqual(a, b)
}

export function generateToken() {
  return randomBytes(TOKEN_BYTES).toString('base64url')
}

export function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** device 字段：四项都必须是 1..limit 的非空字符串 */
export function isValidDevice(device) {
  if (!isPlainObject(device)) return false
  for (const [key, limit] of [
    ['device_id', 128],
    ['name', 64],
    ['platform', 32],
    ['app_version', 32],
  ]) {
    const value = device[key]
    if (typeof value !== 'string' || value.trim().length === 0 || value.length > limit) return false
  }
  return true
}

export async function hashPasscode(passcode) {
  const salt = randomBytes(16)
  const hash = await argon2idAsync(String(passcode), salt, ARGON2)
  return {
    salt: Buffer.from(salt).toString('base64'),
    hash: Buffer.from(hash).toString('base64'),
  }
}

export async function verifyPasscode(passcode, stored) {
  if (!stored?.salt || !stored?.hash || typeof passcode !== 'string' || passcode.length === 0) return false
  try {
    const salt = Buffer.from(stored.salt, 'base64')
    const expected = Buffer.from(stored.hash, 'base64')
    const actual = Buffer.from(await argon2idAsync(passcode, salt, ARGON2))
    if (actual.length !== expected.length) return false
    return timingSafeEqual(actual, expected)
  } catch {
    return false
  }
}

export function normalizeAddress(address) {
  if (!address) return ''
  let value = String(address).trim()
  if (value.startsWith('[') && value.endsWith(']')) value = value.slice(1, -1)
  if (value.toLowerCase().startsWith('::ffff:')) {
    const tail = value.slice(7)
    if (tail.includes('.')) return tail
  }
  return value
}

export function isLoopbackAddress(address) {
  const value = normalizeAddress(address)
  return value === '::1' || value === '127.0.0.1' || value.startsWith('127.')
}

/**
 * 仅允许私有/本地源地址：RFC1918、链路本地、回环、IPv6 ULA/链路本地。
 * 契约要求「拒绝非私有源地址」，不把联阵暴露到公网接口。
 */
export function isPrivateAddress(address) {
  const value = normalizeAddress(address)
  if (!value) return false
  if (isLoopbackAddress(value)) return true

  if (value.includes('.')) {
    const parts = value.split('.').map(Number)
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
      return false
    }
    const [a, b] = parts
    if (a === 10) return true
    if (a === 172 && b >= 16 && b <= 31) return true
    if (a === 192 && b === 168) return true
    if (a === 169 && b === 254) return true
    return false
  }

  const lower = value.toLowerCase()
  if (lower.startsWith('fc') || lower.startsWith('fd')) return true // fc00::/7
  const first = Number.parseInt(lower.split(':')[0] || '', 16)
  if (Number.isFinite(first) && first >= 0xfe80 && first <= 0xfebf) return true // fe80::/10
  return false
}

export function platformName(platform = osPlatform()) {
  if (platform === 'darwin') return 'macos'
  if (platform === 'win32') return 'windows'
  return platform
}

function ipv4ToInt(address) {
  const parts = String(address).split('.').map(Number)
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null
  return (((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0)
}

function intToIpv4(value) {
  return [value >>> 24, (value >>> 16) & 255, (value >>> 8) & 255, value & 255].join('.')
}

/** 全局广播 + 各局域网接口的定向广播地址 */
export function broadcastAddresses() {
  const addresses = new Set(['255.255.255.255'])
  for (const infos of Object.values(networkInterfaces())) {
    for (const info of infos ?? []) {
      if (info.family !== 'IPv4' || info.internal) continue
      const address = ipv4ToInt(info.address)
      const mask = ipv4ToInt(info.netmask)
      if (address === null || mask === null) continue
      addresses.add(intToIpv4(((address & mask) | (~mask >>> 0)) >>> 0))
    }
  }
  return [...addresses]
}

export function clampInteger(value, fallback, min, max) {
  const number = Number(value)
  if (!Number.isInteger(number)) return fallback
  return Math.min(max, Math.max(min, number))
}

export function optionalString(value, maxLength) {
  if (value === undefined || value === null) return ''
  if (typeof value !== 'string') return ''
  return value.length > maxLength ? value.slice(0, maxLength) : value
}
