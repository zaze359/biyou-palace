/**
 * 选择用来跑 dsh 的 Node 可执行文件。
 *
 * 优先级：
 *   1. BIYOU_NODE 环境变量（调试/CI 覆盖）
 *   2. desktop/.runtime/node —— `npm run node:bootstrap` 下载的锁定版本 24.22.3
 *   3. Electron 自带的 Node（ELECTRON_RUN_AS_NODE=1，44.7.0 内置 v24.21.0）
 *   4. PATH 里的 node
 *
 * 2 是唯一「版本被真正固定」的路径；3/4 只要求 >= dsh 的 engines，启动时会在
 * 状态里如实标注来源与版本，方便排查「同机不同版本」的诡异问题。
 */
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { DSH_MIN_NODE, PINNED, gte } from '../versions.js'

function probe(execPath, extraEnv = {}) {
  if (!existsSync(execPath)) return null
  try {
    const output = execFileSync(execPath, ['-v'], {
      encoding: 'utf8',
      timeout: 5000,
      env: { ...process.env, ...extraEnv },
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    return /^v\d+\.\d+\.\d+/.test(output) ? output : null
  } catch {
    return null
  }
}

function fromPath() {
  try {
    const output = execFileSync(process.platform === 'win32' ? 'where' : 'which', ['node'], {
      encoding: 'utf8',
      timeout: 5000,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    return output.split('\n')[0].trim() || null
  } catch {
    return null
  }
}

/**
 * @param {{ desktopRoot: string }} options
 * @returns {{ execPath: string, version: string, source: string, exact: boolean, env: Record<string,string> }}
 */
export function resolveNode({ desktopRoot }) {
  const candidates = []
  const pinnedPath = join(desktopRoot, '.runtime', 'node', 'bin', process.platform === 'win32' ? 'node.exe' : 'node')

  if (process.env.BIYOU_NODE) {
    candidates.push({ execPath: process.env.BIYOU_NODE, source: 'BIYOU_NODE', env: {}, exact: false })
  }
  candidates.push({ execPath: pinnedPath, source: 'pinned(.runtime/node)', env: {}, exact: true })
  candidates.push({ execPath: process.execPath, source: 'electron-runtime', env: { ELECTRON_RUN_AS_NODE: '1' }, exact: false })

  const pathNode = fromPath()
  if (pathNode && pathNode !== process.execPath) {
    candidates.push({ execPath: pathNode, source: 'PATH', env: {}, exact: false })
  }

  const probed = []
  for (const candidate of candidates) {
    const version = probe(candidate.execPath, candidate.env)
    if (!version) continue
    probed.push({ ...candidate, version })
    if (gte(version, DSH_MIN_NODE)) {
      return { ...candidate, version, exact: candidate.exact && version === `v${PINNED.node}` }
    }
  }

  const detail = probed.length
    ? probed.map((item) => `${item.source}=${item.version}`).join(', ')
    : '未找到任何可执行的 node'
  throw new Error(
    `没有满足 dsh 要求的 Node（需要 >= ${DSH_MIN_NODE}）：${detail}。` +
      `请运行 \`npm run node:bootstrap\` 下载锁定的 Node ${PINNED.node}。`,
  )
}
