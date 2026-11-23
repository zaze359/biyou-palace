#!/usr/bin/env node
/**
 * 下载并落地锁定的 Node 版本到 desktop/.runtime/node。
 *
 * 为什么不用 nvm/fnm：CI、干净机器、以及「双击就能跑」的场景不该依赖用户的
 * 版本管理器。这个脚本只做一件事：把 .node-version 指定的官方包解到 .runtime，
 * 之后 `npm run dev` 会自动优先用它跑 dsh。
 *
 *   npm run node:bootstrap
 */
import { createWriteStream } from 'node:fs'
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { DESKTOP_ROOT } from '../electron/paths.js'
import { hashFile } from './artifact-hash.mjs'
import { verifySha256 } from './shasums.mjs'

const version = (/(\d+\.\d+\.\d+)/.exec(readFileSync(join(DESKTOP_ROOT, '.node-version'), 'utf8')) ?? [
  null,
  '24.22.3',
])[1]
const platform = process.platform
const arch = process.arch
const runtimeDir = join(DESKTOP_ROOT, '.runtime')
const nodeDir = join(runtimeDir, 'node')
const downloadDir = join(runtimeDir, 'download')

if (platform === 'win32') {
  console.error('Windows 暂未提供自动下载；请安装 Node ' + version + '，或直接用 Electron 内置 Node 启动（npm run dev 会自动兜底）。')
  process.exit(1)
}

const platformName = platform === 'darwin' ? 'darwin' : 'linux'
const archive = `node-v${version}-${platformName}-${arch}.tar.gz`
const baseUrl = `https://nodejs.org/dist/v${version}`
const url = `${baseUrl}/${archive}`
const shasumsUrl = `${baseUrl}/SHASUMS256.txt`
const archivePath = join(downloadDir, archive)

/** 下载 SHASUMS256.txt，流式校验 archive；不符则删除 archive 并抛错。 */
async function verifyArchive() {
  console.log(`↓ ${shasumsUrl}`)
  const res = await fetch(shasumsUrl)
  if (!res.ok) throw new Error(`校验和清单下载失败 HTTP ${res.status}：${shasumsUrl}`)
  const shasumsText = await res.text()
  const actual = await hashFile(archivePath)
  try {
    const { expected } = verifySha256(shasumsText, archive, actual)
    console.log(`✓ ${archive} 校验通过（sha256 ${expected.slice(0, 16)}…）`)
  } catch (error) {
    rmSync(archivePath, { force: true })
    throw error
  }
}

function nodeBinaryWorks() {
  const binary = join(nodeDir, 'bin', 'node')
  if (!existsSync(binary)) return false
  try {
    const output = execFileSync(binary, ['-v'], { encoding: 'utf8' }).trim()
    return output === `v${version}`
  } catch {
    return false
  }
}

if (nodeBinaryWorks()) {
  console.log(`✓ Node ${version} 已就位：${join(nodeDir, 'bin', 'node')}`)
  process.exit(0)
}

mkdirSync(downloadDir, { recursive: true })

if (!existsSync(archivePath) || statSync(archivePath).size < 1024 * 1024) {
  console.log(`↓ ${url}`)
  const response = await fetch(url)
  if (!response.ok) throw new Error(`下载失败 HTTP ${response.status}：${url}`)
  await pipeline(Readable.fromWeb(response.body), createWriteStream(archivePath))
} else {
  console.log(`✓ 复用已下载的 ${archive}`)
}

// 无论新下载还是复用，解压前都按官方 SHASUMS256.txt 校验，防止篡改/不完整包入库。
await verifyArchive()

rmSync(nodeDir, { recursive: true, force: true })
mkdirSync(nodeDir, { recursive: true })
console.log('… 解压')
execFileSync('tar', ['-xzf', archivePath, '-C', nodeDir, '--strip-components=1'], { stdio: 'inherit' })

const binary = join(nodeDir, 'bin', 'node')
chmodSync(binary, 0o755)
if (!nodeBinaryWorks()) throw new Error('解压后版本校验失败，删除 .runtime 后重试')

console.log(`✓ Node ${version} → ${binary}`)
console.log('  下次 npm run dev 会自动优先使用它跑 dsh。')
