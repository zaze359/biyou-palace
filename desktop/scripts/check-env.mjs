#!/usr/bin/env node
/**
 * 版本一致性自检：三处锁定的版本必须对得上，dsh 依赖必须装好。
 * `npm run dev` 会先跑它，避免「代码没问题但环境不对」的假故障。
 */
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { DESKTOP_ROOT, defaultUserDataDir } from '../electron/paths.js'
import { PINNED, gte } from '../electron/versions.js'

const problems = []
const warnings = []

const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'))
const pkg = readJson(join(DESKTOP_ROOT, 'package.json'))
const nodeVersionFile = readFileSync(join(DESKTOP_ROOT, '.node-version'), 'utf8').trim()
const profilePkgPath = join(DESKTOP_ROOT, 'runtime', 'home', 'profiles', PINNED.profile, 'package.json')

function expectEqual(label, actual, expected) {
  if (actual !== expected) problems.push(`${label}: 期望 ${expected}，实际 ${actual ?? '(缺失)'}`)
}

expectEqual('package.json engines.node', pkg.engines?.node, PINNED.node)
expectEqual('.node-version', nodeVersionFile, PINNED.node)
expectEqual('package.json dsh.version', pkg.dsh?.version, PINNED.dsh)
expectEqual('package.json dsh.electron', pkg.dsh?.electron, PINNED.electron)
expectEqual('package.json devDependencies.electron', pkg.devDependencies?.electron, PINNED.electron)
expectEqual('package.json dsh.profile', pkg.dsh?.profile, PINNED.profile)

if (!existsSync(profilePkgPath)) {
  problems.push(`dsh profile 模板缺失：${profilePkgPath}`)
} else {
  const profile = readJson(profilePkgPath)
  expectEqual('profile dsh 版本', profile.dependencies?.['@deepseek-ai/dsh'], PINNED.dsh)
  for (const bundle of ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app']) {
    if (!profile.dsh?.profile?.bundles?.includes(bundle)) problems.push(`profile bundles 缺少 ${bundle}`)
  }
}

const profileModules = join(DESKTOP_ROOT, 'runtime', 'home', 'profiles', PINNED.profile, 'node_modules')
const dshInstalled = join(profileModules, '@deepseek-ai', 'dsh', 'package.json')
if (!existsSync(dshInstalled)) {
  problems.push(`dsh 依赖未安装：在 desktop/runtime/home/profiles/${PINNED.profile} 执行 npm install（或 npm run dsh:install）`)
} else {
  const installed = readJson(dshInstalled).version
  expectEqual('已安装 dsh 版本', installed, PINNED.dsh)
}

const electronPackage = join(DESKTOP_ROOT, 'node_modules', 'electron', 'package.json')
if (!existsSync(electronPackage)) {
  problems.push('Electron 未安装：在 desktop 执行 npm install')
} else {
  expectEqual('已安装 Electron 版本', readJson(electronPackage).version, PINNED.electron)
}

const pinnedNode = join(DESKTOP_ROOT, '.runtime', 'node', 'bin', 'node')
if (!existsSync(pinnedNode)) {
  warnings.push(`未下载锁定 Node ${PINNED.node}（npm run node:bootstrap）；当前会回退到 Electron 内置 Node`)
}
if (process.versions.node !== PINNED.node) {
  warnings.push(`当前 shell 的 Node 是 ${process.versions.node}（锁定 ${PINNED.node}）；只影响构建脚本，dsh 用 .runtime/node 启动`)
}
if (!gte(process.versions.node, '20.0.0')) {
  problems.push(`构建脚本需要 Node >= 20，当前 ${process.versions.node}`)
}

const userData = defaultUserDataDir()
const profileRuntime = join(userData, 'dsh', 'profiles', PINNED.profile)
const summary = [
  ['锁定版本', `Node ${PINNED.node} · Electron ${PINNED.electron} · dsh ${PINNED.dsh}`],
  ['dsh profile', PINNED.profile],
  ['用户数据', userData],
  ['运行时副本', existsSync(profileRuntime) ? profileRuntime : '(首次启动时创建)'],
]

for (const [label, value] of summary) console.log(`  ${label.padEnd(10, ' ')} ${value}`)

for (const warning of warnings) console.log(`  ! ${warning}`)
if (problems.length) {
  console.error('\n版本自检未通过：')
  for (const problem of problems) console.error(`  ✗ ${problem}`)
  process.exit(1)
}

console.log('\n✓ 版本自检通过')
