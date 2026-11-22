/**
 * S7 健壮性单元测试（node --test，无需 Electron / dsh 运行时）。
 *
 * 覆盖：
 *   1. 日志脱敏：URL token / Bearer / api_key 一律打码
 *   2. 崩溃恢复：READY 后意外退出会受限自动重拉；启动阶段失败不重试
 *   3. 主动停止不触发恢复
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { redactSecrets } from '../redact.js'
import { DSH_STATE, DshServer } from '../server.js'

test('redactSecrets 打码 URL token、Bearer 与常见密钥参数', () => {
  assert.equal(
    redactSecrets('dsh web: http://127.0.0.1:52341/?token=abc123XYZ&x=1'),
    'dsh web: http://127.0.0.1:52341/?token=***&x=1',
  )
  assert.equal(
    redactSecrets('Authorization: Bearer sk-abcdefghijkl'),
    'Authorization: Bearer ***',
  )
  assert.equal(redactSecrets('?api_key=secretvalue'), '?api_key=***')
  assert.equal(redactSecrets('?password=p%40ss'), '?password=***')
  assert.equal(redactSecrets('path=/tmp/ok.txt'), 'path=/tmp/ok.txt')
})

/**
 * 造一个假 dsh CLI：起一个真 HTTP 服务（探活要 200），打印 `dsh web:` + 带 token 的 URL，
 * 然后按参数决定是否自杀。用真实 node 跑，不依赖真实 dsh 运行时。
 */
function fakeBin({ crashAfterReady, crashDelayMs = 80 }) {
  const dir = mkdtempSync(join(tmpdir(), 'biyou-s7-'))
  const file = join(dir, 'fake-dsh.mjs')
  const body = `\
import http from 'node:http'
import { setTimeout as sleep } from 'node:timers/promises'
const server = http.createServer((_req, res) => { res.writeHead(200, {'content-type':'text/html'}); res.end('<html>fake dsh</html>') })
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
process.stdout.write('dsh web: http://127.0.0.1:' + port + '/?token=SECRET-TOKEN-123\\\\n')
await sleep(${crashDelayMs})
${crashAfterReady ? 'process.exit(7)' : 'await new Promise(() => {})'}
`
  writeFileSync(file, body)
  return file
}

function makeServer(bin, opts = {}) {
  const logFile = join(mkdtempSync(join(tmpdir(), 'biyou-s7-log-')), 'dsh.log')
  const server = new DshServer({
    nodePath: process.execPath,
    nodeVersion: process.version,
    dshBin: bin,
    profile: 'biyou',
    host: '127.0.0.1',
    port: 0,
    cwd: tmpdir(),
    dshHome: mkdtempSync(join(tmpdir(), 'biyou-s7-home-')),
    logFile,
    readyTimeoutMs: 5000,
    probeTimeoutMs: 800,
    recoverDelayMs: 60,
    maxRecoverAttempts: 2,
    ...opts,
  })
  return { server, logFile }
}

test('READY 后崩溃：受限自动恢复，且日志中 token 已打码', async () => {
  const { server, logFile } = makeServer(fakeBin({ crashAfterReady: true, crashDelayMs: 150 }), {
    autoRecover: true,
    maxRecoverAttempts: 1,
    recoverDelayMs: 60,
  })
  const statuses = []
  server.on('status', (s) => statuses.push({ state: s.state, recovering: Boolean(s.recovering) }))

  const ready = await server.start()
  assert.equal(ready.state, DSH_STATE.READY)

  // 等崩溃 → 恢复重拉 → 再次 READY
  await new Promise((resolve) => setTimeout(resolve, 500))
  assert.ok(statuses.some((s) => s.recovering), '应广播过 recovering 状态')
  assert.ok(statuses.filter((s) => s.state === DSH_STATE.READY).length >= 2, '应至少两次 READY')

  await server.stop()
  const { readFileSync } = await import('node:fs')
  const logs = readFileSync(logFile, 'utf8')
  assert.ok(!logs.includes('SECRET-TOKEN-123'), '磁盘日志不得含原始 token')
  assert.ok(logs.includes('token=***'), '磁盘日志应有打码痕迹')
})

test('启动阶段失败（探活未通过就退出）不触发恢复', async () => {
  // crashDelayMs=0：进程打印 URL 后立刻退出，探活连不上 → FAILED，不应进入 recovering。
  const { server } = makeServer(fakeBin({ crashAfterReady: true, crashDelayMs: 0 }), {
    autoRecover: true,
    maxRecoverAttempts: 3,
    recoverDelayMs: 40,
  })
  const statuses = []
  server.on('status', (s) => statuses.push({ state: s.state, recovering: Boolean(s.recovering) }))

  const status = await server.start()
  assert.equal(status.state, DSH_STATE.FAILED)
  await new Promise((resolve) => setTimeout(resolve, 200))
  assert.ok(!statuses.some((s) => s.recovering), '启动阶段失败不应自动恢复')
  await server.stop()
})

test('主动 stop 不触发恢复', async () => {
  const { server } = makeServer(fakeBin({ crashAfterReady: false }), {
    autoRecover: true,
    maxRecoverAttempts: 2,
    recoverDelayMs: 40,
  })
  const ready = await server.start()
  assert.equal(ready.state, DSH_STATE.READY)
  await server.stop()
  await new Promise((resolve) => setTimeout(resolve, 150))
  assert.equal(server.status.state, DSH_STATE.STOPPED, 'stop 后应保持 STOPPED，不被恢复逻辑改回')
})
