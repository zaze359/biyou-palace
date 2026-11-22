#!/usr/bin/env node
/**
 * 开发态 macOS 应用名修正（菜单栏 / LaunchServices）。
 *
 * 为什么需要：macOS 的菜单栏应用名与 LaunchServices 显示名取自 app bundle 的
 * `Info.plist`（CFBundleName / CFBundleDisplayName），而不是运行时
 * `app.setName()`。开发模式直接跑 node_modules 里的 Electron.app，它的 bundle
 * 名是 "Electron"，于是这些地方显示 Electron（Dock 图标虽由
 * `app.dock.setIcon()` 换成碧游宫，但名字不受它控制）。
 *
 * 注意：这里只改 bundle 显示名。不要试图用中文名硬链接替换可执行文件来骗 Dock——
 * Electron 用可执行文件名判断是否打包，改名会让 `app.isPackaged` 误判为 true，
 * 导致 runtime / Node 路径全部指错，开发模式直接起不来。
 *
 * 打包产物由 electron-builder 按 productName 生成 bundle，名字本来就是
 * 碧游宫，不受此脚本影响；这里只处理 node_modules 下的开发 bundle。
 *
 * 幂等：两个键已是目标名就静默跳过。`npm install` 重装 electron 会覆盖
 * Info.plist，下次 dev 启动会自动重新打补丁。
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { DESKTOP_ROOT } from '../electron/paths.js'

const APP_NAME = '碧游宫'

if (process.platform !== 'darwin') process.exit(0)

const plist = join(DESKTOP_ROOT, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'Info.plist')
if (!existsSync(plist)) process.exit(0)

let xml = readFileSync(plist, 'utf8')
let changed = false
for (const key of ['CFBundleName', 'CFBundleDisplayName']) {
  const re = new RegExp(`(<key>${key}</key>\\s*<string>)([^<]*)(</string>)`)
  if (!re.test(xml)) continue
  const next = xml.replace(re, `$1${APP_NAME}$3`)
  if (next !== xml) {
    xml = next
    changed = true
  }
}
if (!changed) process.exit(0)

const bak = `${plist}.biyou-bak`
if (!existsSync(bak)) writeFileSync(bak, readFileSync(plist), { mode: 0o644 })
writeFileSync(plist, xml)
console.log(`  Dock 应用名  ${APP_NAME}（开发 bundle 已改名，备份 ${bak.split('/').pop()}）`)
