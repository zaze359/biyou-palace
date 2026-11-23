#!/usr/bin/env node
/**
 * S8 打包入口：自检 → 校验打包前置 → 注入构建元信息 → electron-builder → 校验和清单。
 *
 *   npm run build            # 按当前平台打包
 *   npm run build -- --mac   # 指定 mac（--win / --linux 同理）
 *
 * 前置：runtime/home/profiles/<profile>/node_modules 已装（npm run dsh:install），
 *       锁定 Node 已下载（npm run node:bootstrap）——extraResources 要拷这两个目录。
 * V2 不做签名/公证/自动更新；产物是未签名包，交给 11-21-client-packaging。
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { DESKTOP_ROOT, runtimeSourceRoot, pinnedNodeRoot } from '../electron/paths.js'
import { PINNED } from '../electron/versions.js'
import { hashDirectory, hashFile } from './artifact-hash.mjs'

const APP_VERSION = JSON.parse(readFileSync(join(DESKTOP_ROOT, 'package.json'), 'utf8')).version

const args = process.argv.slice(2)
const platformFlag = args.includes('--mac') ? 'mac' : args.includes('--win') ? 'win' : args.includes('--linux') ? 'linux' : null

function step(label) {
  console.log(`\n→ ${label}`)
}
function die(message) {
  console.error(`  ✗ ${message}`)
  process.exit(1)
}
function cleanEnv() {
  const env = { ...process.env }
  delete env.NODE_OPTIONS
  delete env.ELECTRON_RUN_AS_NODE
  return env
}

// ---- 1. 版本自检 ----
step('版本与依赖自检')
const check = spawnSync(process.execPath, [join(DESKTOP_ROOT, 'scripts', 'check-env.mjs')], { stdio: 'inherit', env: cleanEnv() })
if (check.status !== 0) die('版本自检未通过，先跑 npm run setup')

// ---- 2. 打包前置：runtime 与锁定 Node 必须就位（extraResources 源） ----
step('校验打包资源')
const runtimeDir = runtimeSourceRoot()
const profileModules = join(runtimeDir, 'home', 'profiles', PINNED.profile, 'node_modules')
if (!existsSync(join(profileModules, '@deepseek-ai', 'dsh', 'package.json'))) {
  die(`dsh 运行时未安装：${profileModules}。先跑 npm run dsh:install`)
}
const nodeBin = join(pinnedNodeRoot(), 'node', 'bin', process.platform === 'win32' ? 'node.exe' : 'node')
if (!existsSync(nodeBin)) die(`锁定 Node 未下载：${nodeBin}。先跑 npm run node:bootstrap`)

const icon = join(DESKTOP_ROOT, 'assets', 'icon.png')
if (!existsSync(icon)) die('应用图标缺失：assets/icon.png')

function dirSize(dir) {
  let total = 0
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) total += dirSize(full)
    else if (entry.isFile()) total += statSync(full).size
  }
  return total
}
const sizeMB = (dirSize(profileModules) / 1024 / 1024).toFixed(0)
console.log(`  dsh runtime ${sizeMB} MB · 锁定 Node ${nodeBin}`)

// ---- 3. 构建元信息 ----
const commit = spawnSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: DESKTOP_ROOT, encoding: 'utf8' })
const commitHash = commit.status === 0 ? commit.stdout.trim() : 'unknown'
const builtAt = new Date().toISOString()
const env = {
  ...cleanEnv(),
  BIYOU_BUILD_COMMIT: commitHash,
  BIYOU_BUILD_TIME: builtAt,
}
console.log(`  version ${APP_VERSION} · commit ${commitHash} · built ${builtAt}`)

// ---- 4. electron-builder ----
step('electron-builder 打包')
const builderCli = join(DESKTOP_ROOT, 'node_modules', 'electron-builder', 'cli.js')
if (!existsSync(builderCli)) die('electron-builder 未安装：在 desktop 执行 npm install')
const buildArgs = [builderCli, '--config', join(DESKTOP_ROOT, 'electron-builder.yml')]
if (platformFlag) buildArgs.push(`--${platformFlag}`)
const build = spawnSync(process.execPath, buildArgs, { stdio: 'inherit', cwd: DESKTOP_ROOT, env })
if (build.status !== 0) die(`electron-builder 失败（exit ${build.status ?? 'null'}）`)

// ---- 5. 产物校验和清单 ----
step('生成产物清单')
const distDir = join(DESKTOP_ROOT, 'dist')
if (!existsSync(distDir)) die('未找到 dist/ 产物目录')
const artifacts = []
function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    // .app 是目录产物：记录它本身，不再深入它内部（否则把 2G 的 Frameworks 全列一遍）
    if (entry.isDirectory() && /\.app$/i.test(entry.name)) {
      artifacts.push(full)
      continue
    }
    if (entry.isDirectory()) walk(full)
    else if (entry.isFile() && /\.(dmg|zip|exe|AppImage)$/i.test(entry.name)) artifacts.push(full)
  }
}
walk(distDir)
if (artifacts.length === 0) die('dist/ 下没有安装包产物')

const manifest = {
  version: APP_VERSION,
  pinned: PINNED,
  commit: commitHash,
  builtAt,
  // 用 top-level await 逐产物算内容哈希；目录产物流式读取，不把 2G 全读进内存。
  // 哈希函数见 scripts/artifact-hash.mjs：内容/结构变化必然改变 sha256。
  artifacts: await Promise.all(artifacts.map(async (file) => {
    const isDir = statSync(file).isDirectory()
    return {
      file: relative(distDir, file),
      type: isDir ? 'directory' : 'file',
      bytes: isDir ? dirSize(file) : statSync(file).size,
      sha256: isDir ? await hashDirectory(file) : await hashFile(file),
    }
  })),
}
mkdirSync(distDir, { recursive: true })
writeFileSync(join(distDir, 'build-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)
console.log(`  ${artifacts.length} 个产物，清单 dist/build-manifest.json`)
for (const item of manifest.artifacts) console.log(`  ${item.file}  ${item.sha256.slice(0, 16)}…`)

console.log('\n✓ S8 打包完成（未签名）；交给 11-21-client-packaging 做 CI 与校验')
