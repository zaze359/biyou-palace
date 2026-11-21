#!/usr/bin/env node
/**
 * 渲染 assets/icon.png（默认 1024×1024）。
 *
 * 默认事实源是 assets/brand/icon-photo.png —— 写实的青萍剑 logo（水墨剑为主体、青莲为背景）。
 * 需要极简单色几何副版时用 --geo，源切到 assets/brand/icon.svg。
 * PNG / SVG 都由本脚本渲染成 PNG，不手写像素。
 * 需要 Electron（本项目已有 devDependency），不引入额外图形库。
 *
 * 用法：
 *   node scripts/render-icon.mjs [--size 1024]   # 写实 logo（默认）
 *   node scripts/render-icon.mjs --geo           # 几何副版
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const DESKTOP_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const useGeo = process.argv.includes('--geo')
const SRC = join(
  DESKTOP_ROOT,
  'assets',
  'brand',
  useGeo ? 'icon.svg' : 'icon-photo.png',
)
const OUT = join(DESKTOP_ROOT, 'assets', 'icon.png')
const MAIN = join(DESKTOP_ROOT, 'scripts', 'render-icon.main.cjs')

const sizeArgIndex = process.argv.indexOf('--size')
const size = sizeArgIndex > -1 ? Number(process.argv[sizeArgIndex + 1]) : 1024

if (!existsSync(SRC)) {
  console.error(`✗ 缺少图标源文件：${SRC}`)
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

const result = spawnSync(electronBinary, [MAIN, SRC, OUT, String(size)], {
  stdio: 'inherit',
  env,
})

if (result.status !== 0) {
  console.error(`✗ 渲染失败（exit ${result.status}）`)
  process.exit(result.status ?? 1)
}

mkdirSync(dirname(OUT), { recursive: true })
console.log(
  `✓ ${OUT}（${size}×${size}，源自 assets/brand/${useGeo ? 'icon.svg' : 'icon-photo.png'}）`,
)
