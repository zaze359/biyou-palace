#!/usr/bin/env node
/**
 * 从 assets/brand/icon.svg 渲染 assets/icon.png（默认 1024×1024）。
 *
 * SVG 是图标的唯一事实源：改形状改 SVG，PNG 由本脚本重生成，不再手写像素。
 * 需要 Electron（本项目已有 devDependency），不引入额外图形库。
 *
 * 用法：node scripts/render-icon.mjs [--size 1024]
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const DESKTOP_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const SVG = join(DESKTOP_ROOT, 'assets', 'brand', 'icon.svg')
const OUT = join(DESKTOP_ROOT, 'assets', 'icon.png')
const MAIN = join(DESKTOP_ROOT, 'scripts', 'render-icon.main.cjs')

const sizeArgIndex = process.argv.indexOf('--size')
const size = sizeArgIndex > -1 ? Number(process.argv[sizeArgIndex + 1]) : 1024

if (!existsSync(SVG)) {
  console.error(`✗ 缺少图标源文件：${SVG}`)
  process.exit(1)
}

const electronBinary = (await import('electron')).default
if (!electronBinary) {
  console.error('✗ 找不到 Electron，请先 npm install')
  process.exit(1)
}

const env = { ...process.env }
// Electron 在  ELECTRON_RUN_AS_NODE=1 下会退化成纯 Node，require('electron') 直接失败。
delete env.ELECTRON_RUN_AS_NODE
delete env.NODE_OPTIONS

const result = spawnSync(electronBinary, [MAIN, SVG, OUT, String(size)], {
  stdio: 'inherit',
  env,
})

if (result.status !== 0) {
  console.error(`✗ 渲染失败（exit ${result.status}）`)
  process.exit(result.status ?? 1)
}

mkdirSync(dirname(OUT), { recursive: true })
console.log(`✓ ${OUT}（${size}×${size}，源自 assets/brand/icon.svg）`)
