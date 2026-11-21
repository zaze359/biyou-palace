#!/usr/bin/env node
/**
 * 生成品牌图标占位（assets/icon.png，1024×1024）。
 * 不引入图形库：2× 超采样 + 手写 PNG 编码，保证干净机器也能 `npm run icon`。
 * 正式图标（.icns/.ico）在 S8 打包阶段由设计稿替换。
 */
import { deflateSync } from 'node:zlib'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { DESKTOP_ROOT } from '../electron/paths.js'

const SIZE = 1024
const SCALE = 2
const S = SIZE * SCALE

const clamp01 = (value) => (value < 0 ? 0 : value > 1 ? 1 : value)
const mix = (a, b, t) => a + (b - a) * t

function insideRoundRect(x, y, size, radius) {
  const nx = Math.max(radius - x, 0, x - (size - radius))
  const ny = Math.max(radius - y, 0, y - (size - radius))
  return nx * nx + ny * ny <= radius * radius
}

const buffer = Buffer.alloc(S * S * 4)

// 画布：墨底圆角方 + 碧色径向光
for (let y = 0; y < S; y += 1) {
  for (let x = 0; x < S; x += 1) {
    const index = (y * S + x) * 4
    if (!insideRoundRect(x, y, S, S * 0.22)) continue

    const vertical = y / S
    let r = mix(18, 8, vertical)
    let g = mix(26, 12, vertical)
    let b = mix(24, 12, vertical)

    const dx = (x - S * 0.5) / (S * 0.5)
    const dy = (y - S * 0.42) / (S * 0.5)
    const glow = clamp01(1 - Math.sqrt(dx * dx + dy * dy)) ** 2
    r += glow * 14
    g += glow * 46
    b += glow * 38

    buffer[index] = Math.round(r)
    buffer[index + 1] = Math.round(g)
    buffer[index + 2] = Math.round(b)
    buffer[index + 3] = 255
  }
}

// 纹样：碧色环 + 香火金菱形（阵眼）
const center = S * 0.5
const ringOuter = S * 0.30
const ringInner = S * 0.255

for (let y = 0; y < S; y += 1) {
  for (let x = 0; x < S; x += 1) {
    const index = (y * S + x) * 4
    if (!buffer[index + 3]) continue
    const dist = Math.hypot(x - center, y - center)

    if (dist <= ringOuter && dist >= ringInner) {
      const t = clamp01((x + y) / (2 * S))
      buffer[index] = Math.round(mix(111, 31, t))
      buffer[index + 1] = Math.round(mix(231, 156, t))
      buffer[index + 2] = Math.round(mix(196, 124, t))
    }

    // 金色菱形：|dx| + |dy| <= 半径
    const diamond = Math.abs(x - center) + Math.abs(y - center)
    if (diamond <= S * 0.072) {
      const t = clamp01((y - center) / (S * 0.14) + 0.5)
      buffer[index] = Math.round(mix(232, 200, t))
      buffer[index + 1] = Math.round(mix(196, 148, t))
      buffer[index + 2] = Math.round(mix(120, 84, t))
    }
  }
}

// 2× 盒式降采样
const out = Buffer.alloc(SIZE * SIZE * 4)
for (let y = 0; y < SIZE; y += 1) {
  for (let x = 0; x < SIZE; x += 1) {
    let r = 0
    let g = 0
    let b = 0
    let a = 0
    for (let sy = 0; sy < SCALE; sy += 1) {
      for (let sx = 0; sx < SCALE; sx += 1) {
        const index = ((y * SCALE + sy) * S + (x * SCALE + sx)) * 4
        r += buffer[index]
        g += buffer[index + 1]
        b += buffer[index + 2]
        a += buffer[index + 3]
      }
    }
    const count = SCALE * SCALE
    const index = (y * SIZE + x) * 4
    out[index] = Math.round(r / count)
    out[index + 1] = Math.round(g / count)
    out[index + 2] = Math.round(b / count)
    out[index + 3] = Math.round(a / count)
  }
}

// ---- 最小 PNG 编码 ----
const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

function crc32(buffer) {
  let c = 0xffffffff
  for (const byte of buffer) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length, 0)
  const typeAndData = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(typeAndData), 0)
  return Buffer.concat([length, typeAndData, crc])
}

const ihdr = Buffer.alloc(13)
ihdr.writeUInt32BE(SIZE, 0)
ihdr.writeUInt32BE(SIZE, 4)
ihdr[8] = 8 // bit depth
ihdr[9] = 6 // RGBA
ihdr[10] = 0
ihdr[11] = 0
ihdr[12] = 0

const raw = Buffer.alloc((SIZE * 4 + 1) * SIZE)
for (let y = 0; y < SIZE; y += 1) {
  raw[y * (SIZE * 4 + 1)] = 0
  out.copy(raw, y * (SIZE * 4 + 1) + 1, y * SIZE * 4, (y + 1) * SIZE * 4)
}

const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk('IHDR', ihdr),
  chunk('IDAT', deflateSync(raw, { level: 9 })),
  chunk('IEND', Buffer.alloc(0)),
])

const target = join(DESKTOP_ROOT, 'assets', 'icon.png')
mkdirSync(dirname(target), { recursive: true })
writeFileSync(target, png)
console.log(`✓ ${target}（${SIZE}×${SIZE}, ${(png.length / 1024).toFixed(1)} KB）`)
