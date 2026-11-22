#!/usr/bin/env node
/**
 * 开发态 Dock 应用名修正。
 *
 * 为什么需要：macOS Dock / LaunchServices 显示的名字取自 app bundle 的
 * `Info.plist`（CFBundleName / CFBundleDisplayName），而不是运行时
 * `app.setName()`。开发模式直接跑 node_modules 里的 Electron.app，它的
 * bundle 名是 "Electron"，于是 Dock hover 显示 Electron（图标虽已由
 * `app.dock.setIcon()` 换成碧游宫，但名字不受它控制）。
 *
 * 打包产物由 electron-builder 按 productName 生成 bundle，名字本来就是
 * 碧游宫，不需要也不应该改；所以这里只处理 node_modules 下的开发 bundle。
 *
 * 幂等：两个键已是目标名就直接跳过。`npm install` 重装 electron 会覆盖
 * Info.plist，但下次 dev 启动会自动重新打补丁。
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { DESKTOP_ROOT } from '../electron/paths.js'

const APP_NAME = '碧游宫'

if (process.platform !== 'darwin') process.exit(0)

const plist = join(DESKTOP_ROOT, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'Info.plist')
if (!existsSync(plist)) process.exit(0)

let xml = readFileSync(plist, 'utf8')

// 只替换这两个键紧随其后的 <string> 值，避免误伤其它字段。
function setKey(text, key) {
  const re = new RegExp(`(<key>${key}</key>\\s*<string>)([^<]*)(</string>)`)
  if (!re.test(text)) return { text, changed: false }
  const next = text.replace(re, `$1${APP_NAME}$3`)
  return { text: next, changed: next !== text }
}

const nameResult = setKey(xml, 'CFBundleName')
xml = nameResult.text
const displayResult = setKey(xml, 'CFBundleDisplayName')
xml = displayResult.text

if (!nameResult.changed && !displayResult.changed) process.exit(0)

const bak = `${plist}.biyou-bak`
if (!existsSync(bak)) writeFileSync(bak, readFileSync(plist), { mode: 0o644 })
writeFileSync(plist, xml)
console.log(`  Dock 应用名  ${APP_NAME}（开发 bundle 已改名，备份 ${bak.split('/').pop()}）`)
