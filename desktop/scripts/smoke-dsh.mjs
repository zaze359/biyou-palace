#!/usr/bin/env node
/**
 * dsh 接入的无头冒烟：起 profile → 解析 URL → HTTP 探活 → 停进程 → 确认无残留。
 * 不需要图形界面，CI 里也能跑。
 *
 *   npm run smoke:dsh
 */
import { DESKTOP_ROOT, defaultUserDataDir, layoutPaths } from '../electron/paths.js'
import { PINNED } from '../electron/versions.js'
import { resolveNode } from '../electron/dsh/resolve-node.js'
import { provisionProfile } from '../electron/dsh/provision.js'
import { DSH_STATE, DshServer } from '../electron/dsh/server.js'

const checks = []
const fail = (message) => {
  checks.push({ ok: false, message })
  console.error(`  ✗ ${message}`)
}
const pass = (message) => {
  checks.push({ ok: true, message })
  console.log(`  ✓ ${message}`)
}

const userData = defaultUserDataDir()
const paths = layoutPaths(userData)

let node
try {
  node = resolveNode({ desktopRoot: DESKTOP_ROOT })
  pass(`Node ${node.version}（${node.source}${node.exact ? '，锁定版本' : '，非锁定版本'}）`)
} catch (error) {
  fail(error.message)
  process.exit(1)
}

let provisioning
try {
  provisioning = provisionProfile({ userDataDir: userData, profile: PINNED.profile, runtimeRoot: `${DESKTOP_ROOT}/runtime` })
  pass(`profile 同步：${provisioning.profileDir}`)
  pass(`dsh ${provisioning.dshVersion}`)
} catch (error) {
  fail(error.message)
  process.exit(1)
}

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
  readyTimeoutMs: 300_000,
})

const status = await server.start()
if (status.state !== DSH_STATE.READY) {
  fail(`dsh 未就绪：${status.error ?? 'unknown'}`)
  for (const line of status.tail ?? []) console.error(`    ${line}`)
  await server.stop()
  process.exit(1)
}
pass(`dsh 就绪：${new URL(status.url).host}`)

// 浏览器拿到的 URL 带 token：服务端回 303 + Set-Cookie，之后才是 200 的 HTML。
// 这里按同样的顺序走一遍，确认「Electron 载入这条 URL」真的能拿到界面。
const first = await fetch(status.url, { redirect: 'manual' })
const cookies = first.headers.getSetCookie?.() ?? []
pass(`首次请求 HTTP ${first.status}（${cookies.length} 个 Set-Cookie）`)
if (![200, 302, 303].includes(first.status)) fail(`期望 200/303，实际 ${first.status}`)

const location = first.headers.get('location')
if (location) {
  const followed = await fetch(new URL(location, status.url), {
    headers: { cookie: cookies.map((item) => item.split(';')[0]).join('; ') },
  })
  const html = await followed.text()
  pass(`跟随跳转 HTTP ${followed.status} · ${html.length} bytes`)
  if (followed.status !== 200) fail(`期望 200，实际 ${followed.status}`)
  if (!/<html/i.test(html)) fail('返回内容不像 HTML')
  if (!/dsh|deepseek/i.test(html)) fail('返回内容不含 dsh 标识')
}

const pid = status.pid
const stillAlive = (target) => {
  try {
    process.kill(target, 0)
    return true
  } catch {
    return false
  }
}

// dsh 会自己起工具子进程，按进程组识别：组主进程没了且组内无成员才算干净
const groupAlive = () => {
  if (process.platform === 'win32') return false
  try {
    process.kill(-pid, 0)
    return true
  } catch {
    return false
  }
}

await server.stop()
pass('已发送停止信号')

await new Promise((resolve) => setTimeout(resolve, 800))
if (stillAlive(pid)) fail(`进程 ${pid} 仍存活`)
else pass('主进程已退出')
if (groupAlive()) fail(`进程组 ${pid} 仍有残留（可能留下工具子进程）`)
else pass('进程组无残留')

const failed = checks.filter((item) => !item.ok)
console.log(`\n${failed.length === 0 ? '✓ dsh 接入冒烟通过' : `✗ ${failed.length} 项失败`}`)
process.exit(failed.length === 0 ? 0 : 1)
