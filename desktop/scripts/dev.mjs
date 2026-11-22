#!/usr/bin/env node
/**
 * 启动器：先自检，再用「干净环境」拉起 Electron。
 *
 * 为什么不让 npm 直接调 electron：
 *   - 有些终端/IDE 会设置 ELECTRON_RUN_AS_NODE=1，Electron 会退化成纯 Node，
 *     报 "module 'electron' does not provide an export named 'BrowserWindow'"；
 *   - NODE_OPTIONS 里的 --require 垫片（沙箱、APM）会污染主进程。
 * 这两类变量在启动器里统一剔除，避免「换台机器就打不开」。
 *
 * 用法：
 *   node scripts/dev.mjs                # 等同 npm start
 *   node scripts/dev.mjs --dev          # 开发模式
 *   node scripts/dev.mjs --smoke        # 壳冒烟（截图 + 报告）
 *   node scripts/dev.mjs --smoke-quit   # 验证关窗退出与子进程清理
 */
import { spawn, spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { DESKTOP_ROOT } from '../electron/paths.js'

const require = createRequire(import.meta.url)
const args = process.argv.slice(2)
const skipCheck = args.includes('--no-check')

if (!skipCheck) {
  const check = spawnSync(process.execPath, [join(DESKTOP_ROOT, 'scripts', 'check-env.mjs')], { stdio: 'inherit' })
  if (check.status !== 0) process.exit(check.status ?? 1)
}

// 开发态把 Electron.app 的 bundle 名改成产品名，否则 macOS Dock 显示 "Electron"。
// 幂等，打包产物不受影响；失败只告警，不影响启动。
const patch = spawnSync(process.execPath, [join(DESKTOP_ROOT, 'scripts', 'patch-electron-name.mjs')], { stdio: 'inherit' })
if (patch.status !== 0) console.error('! Dock 应用名修正失败，开发态 Dock 可能显示 Electron')

let electronPath
try {
  electronPath = require('electron')
} catch {
  console.error('找不到 Electron，请先在 desktop 目录执行 npm install')
  process.exit(1)
}

const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE
delete env.NODE_OPTIONS

const electronArgs = [DESKTOP_ROOT, ...args.filter((arg) => arg !== '--no-check')]
// 受限环境（容器、CI、无签名的沙箱）里 Chromium 自己的 sandbox 起不来，需要显式关闭
if (process.env.BIYOU_NO_SANDBOX === '1' && !electronArgs.includes('--no-sandbox')) electronArgs.push('--no-sandbox')

const child = spawn(electronPath, electronArgs, { env, stdio: 'inherit' })
child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`Electron 被信号终止：${signal}`)
    process.exit(1)
  }
  process.exit(code ?? 0)
})
