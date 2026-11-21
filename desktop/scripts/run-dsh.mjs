#!/usr/bin/env node
/**
 * 在终端里单独把 dsh 跑起来，不带 Electron。
 * 用途：排查「是壳的问题还是 dsh 的问题」、验证 profile 改动。
 *
 *   npm run dsh:web
 */
import { DESKTOP_ROOT, defaultUserDataDir, layoutPaths } from '../electron/paths.js'
import { PINNED } from '../electron/versions.js'
import { resolveNode } from '../electron/dsh/resolve-node.js'
import { provisionProfile } from '../electron/dsh/provision.js'
import { DSH_STATE, DshServer } from '../electron/dsh/server.js'

const userData = defaultUserDataDir()
const paths = layoutPaths(userData)
const node = resolveNode({ desktopRoot: DESKTOP_ROOT })
const provisioning = provisionProfile({ userDataDir: userData, profile: PINNED.profile, runtimeRoot: `${DESKTOP_ROOT}/runtime` })

console.log(`Node ${node.version} (${node.source}) · dsh ${provisioning.dshVersion} · profile ${PINNED.profile}`)
console.log(`DSH_HOME ${provisioning.dshHome}`)

const server = new DshServer({
  nodePath: node.execPath,
  nodeVersion: node.version,
  nodeEnv: node.env,
  dshBin: provisioning.dshBin,
  profile: PINNED.profile,
  host: '127.0.0.1',
  port: 0,
  cwd: paths.root,
  dshHome: provisioning.dshHome,
  logFile: paths.dshLog,
})

server.on('status', (status) => {
  if (status.state === DSH_STATE.READY) console.log(`\n打开：${status.url}\n（Ctrl-C 停止）`)
  if (status.state === DSH_STATE.FAILED) {
    console.error(`启动失败：${status.error}`)
    for (const line of status.tail ?? []) console.error(`  ${line}`)
  }
})

const status = await server.start()
if (status.state !== DSH_STATE.READY) process.exit(1)

let stopping = false
const shutdown = async () => {
  if (stopping) return
  stopping = true
  console.log('\n停止 dsh…')
  await server.stop()
  process.exit(0)
}

process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
