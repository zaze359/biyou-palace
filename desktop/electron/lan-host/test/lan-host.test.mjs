/**
 * 联阵服务集成测试：直接起真实 HTTP/UDP，按契约场景跑。
 *
 *   node --test electron/lan-host/test/lan-host.test.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createSocket } from 'node:dgram'
import { createServer } from 'node:net'
import { setTimeout as sleep } from 'node:timers/promises'
import { FormationHost } from '../index.js'
import { hashPasscode, isPrivateAddress, verifyPasscode } from '../util.js'

function device(prefix) {
  const suffix = Math.random().toString(16).slice(2, 10)
  return {
    device_id: `${prefix}-${suffix}`,
    name: `验收设备 ${suffix}`,
    platform: 'android',
    app_version: '0.1.0',
  }
}

function joinBody(roomId, dev, passcode) {
  const body = {
    protocol: 1,
    room_id: roomId,
    device: dev,
    agent: { root: 'outer', skills: ['chat'], models: [], max_concurrency: 1 },
  }
  if (passcode !== undefined) body.passcode = passcode
  return body
}

async function call(base, method, path, { body, token, hostToken } = {}) {
  const headers = {}
  if (body !== undefined) headers['content-type'] = 'application/json'
  if (token) headers.authorization = `Bearer ${token}`
  if (hostToken) headers['x-biyou-host-token'] = hostToken
  const response = await fetch(base + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  let json = null
  try {
    json = text ? JSON.parse(text) : null
  } catch {
    json = { raw: text }
  }
  return { status: response.status, body: json, headers: response.headers }
}

async function startHost(options = {}) {
  const host = new FormationHost({
    logger: () => {},
    hostName: '测试阵主',
    platform: 'macos',
    appVersion: '0.1.0',
  })
  const snapshot = await host.open({
    name: '测试阵',
    mode: 'open',
    httpPort: 0,
    discoveryPort: 0,
    announceIntervalMs: 0,
    ...options,
  })
  return {
    host,
    snapshot,
    base: `http://127.0.0.1:${snapshot.endpoints.http_port}`,
    roomId: snapshot.room.room_id,
    hostToken: host.snapshot({ includeHostToken: true }).hostToken,
  }
}

async function waitFor(predicate, timeoutMs = 3000, intervalMs = 50) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await predicate()
    if (value) return value
    await sleep(intervalMs)
  }
  return null
}

async function openEvents(base, token) {
  const controller = new AbortController()
  const response = await fetch(`${base}/lan/v1/events`, {
    headers: { authorization: `Bearer ${token}` },
    signal: controller.signal,
  })
  return { response, controller, reader: response.body.getReader() }
}

async function readEvents(reader, predicate, timeoutMs = 4000) {
  const decoder = new TextDecoder()
  let text = ''
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const remaining = Math.max(1, deadline - Date.now())
    const result = await Promise.race([
      reader.read(),
      sleep(remaining).then(() => ({ timeout: true })),
    ])
    if (result.timeout) break
    if (result.done) return { text, done: true }
    text += decoder.decode(result.value, { stream: true })
    if (predicate(text)) return { text, done: false }
  }
  throw new Error(`等待 SSE 事件超时，已收到：\n${text}`)
}

async function discover(port, timeoutMs = 2000) {
  const socket = createSocket('udp4')
  await new Promise((resolve) => socket.bind(0, '127.0.0.1', resolve))
  const message = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('UDP discover 超时')), timeoutMs)
    socket.once('message', (data) => {
      clearTimeout(timer)
      resolve(JSON.parse(data.toString('utf8')))
    })
    socket.send(Buffer.from(JSON.stringify({ t: 'discover', v: 1 })), port, '127.0.0.1')
  })
  socket.close()
  return message
}

test('开放阵：入阵/心跳/成员/踢人/封禁/离阵/错误码/限速/散阵', { timeout: 20_000 }, async () => {
  const { host, base, roomId, hostToken } = await startHost()
  try {
    const health = await call(base, 'GET', '/lan/v1/health')
    assert.equal(health.status, 200)
    assert.equal(health.body.protocol, 1)

    const room = await call(base, 'GET', '/lan/v1/room')
    assert.equal(room.status, 200)
    assert.equal(room.body.room.room_id, roomId)
    assert.equal(room.body.room.mode, 'open')

    assert.equal((await call(base, 'GET', '/lan/v1/members')).status, 403)
    assert.equal((await call(base, 'GET', '/lan/v1/members', { hostToken: 'wrong' })).status, 403)

    const devA = device('open-a')
    const joined = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devA) })
    assert.equal(joined.status, 200)
    assert.equal(joined.body.room.players, 1)
    assert.ok(joined.body.session_token)

    const heartbeat = await call(base, 'POST', '/lan/v1/heartbeat', { token: joined.body.session_token })
    assert.equal(heartbeat.status, 200)
    assert.equal(heartbeat.body.players, 1)

    const members = await call(base, 'GET', '/lan/v1/members', { hostToken })
    assert.equal(members.status, 200)
    assert.equal(members.body.members.length, 1)
    assert.equal(members.body.members[0].device_id, devA.device_id)

    const kicked = await call(base, 'POST', `/lan/v1/members/${joined.body.member_id}/kick`, {
      hostToken,
    })
    assert.equal(kicked.status, 200)
    assert.equal(
      (await call(base, 'POST', '/lan/v1/heartbeat', { token: joined.body.session_token })).body.error.code,
      'invalid_token',
    )
    const banned = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devA) })
    assert.equal(banned.status, 403)
    assert.equal(banned.body.error.code, 'banned')

    const devB = device('open-b')
    const joinedB = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devB) })
    assert.equal(joinedB.status, 200)
    assert.equal((await call(base, 'POST', '/lan/v1/leave', { token: joinedB.body.session_token })).status, 200)
    assert.equal(
      (await call(base, 'POST', '/lan/v1/heartbeat', { token: joinedB.body.session_token })).body.error.code,
      'invalid_token',
    )

    const mismatch = await call(base, 'POST', '/lan/v1/join', {
      body: { ...joinBody(roomId, devB), protocol: 9 },
    })
    assert.equal(mismatch.status, 409)
    assert.equal(mismatch.body.error.code, 'protocol_mismatch')

    const missingRoom = await call(base, 'POST', '/lan/v1/join', {
      body: { ...joinBody(roomId, devB), room_id: 'missing' },
    })
    assert.equal(missingRoom.status, 404)
    assert.equal(missingRoom.body.error.code, 'room_not_found')

    const invalidDevice = await call(base, 'POST', '/lan/v1/join', {
      body: { protocol: 1, room_id: roomId, device: {} },
    })
    assert.equal(invalidDevice.status, 400)
    assert.equal(invalidDevice.body.error.code, 'invalid_device')

    const rateDev = device('rate-open')
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const result = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, rateDev) })
      assert.equal(result.status, 200, `第 ${attempt + 1} 次入阵应成功`)
    }
    const limited = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, rateDev) })
    assert.equal(limited.status, 429)
    assert.equal(limited.body.error.code, 'rate_limited')
    assert.equal(limited.headers.get('retry-after'), '60')

    const disbanded = await call(base, 'POST', '/lan/v1/disband', { hostToken })
    assert.equal(disbanded.status, 200)
    assert.equal(disbanded.body.closed, true)
    const stopped = await waitFor(async () => host.state === 'standalone')
    assert.ok(stopped, `散阵后应回到 standalone，实际 ${host.state}`)
  } finally {
    await host.stop()
  }
})

test('闭阵：缺阵符/错阵符/正确阵符/阵满/限速', { timeout: 20_000 }, async () => {
  const { host, base, roomId, hostToken } = await startHost({
    mode: 'closed',
    passcode: '1234',
    capacity: 1,
  })
  try {
    const room = await call(base, 'GET', '/lan/v1/room')
    assert.equal(room.body.room.mode, 'closed')

    const devA = device('closed-a')
    const missing = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devA) })
    assert.equal(missing.status, 401)
    assert.equal(missing.body.error.code, 'passcode_required')

    const wrong = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devA, 'wrong') })
    assert.equal(wrong.status, 403)
    assert.equal(wrong.body.error.code, 'passcode_invalid')

    const joined = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devA, '1234') })
    assert.equal(joined.status, 200)

    const devB = device('closed-b')
    const full = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, devB, '1234') })
    assert.equal(full.status, 409)
    assert.equal(full.body.error.code, 'room_full')

    const mismatch = await call(base, 'POST', '/lan/v1/join', {
      body: { ...joinBody(roomId, devB, '1234'), protocol: 9 },
    })
    assert.equal(mismatch.status, 409)
    assert.equal(mismatch.body.error.code, 'protocol_mismatch')

    assert.equal(
      (await call(base, 'POST', `/lan/v1/members/${joined.body.member_id}/kick`, { hostToken })).status,
      200,
    )

    const rateDev = device('rate-closed')
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const result = await call(base, 'POST', '/lan/v1/join', {
        body: joinBody(roomId, rateDev, 'wrong'),
      })
      assert.equal(result.body.error.code, 'passcode_invalid')
    }
    const limited = await call(base, 'POST', '/lan/v1/join', {
      body: joinBody(roomId, rateDev, 'wrong'),
    })
    assert.equal(limited.status, 429)
    assert.equal(limited.body.error.code, 'rate_limited')
  } finally {
    await host.stop()
  }
})

test('UDP 发现：discover → room，字段与 HTTP 端口一致', { timeout: 10_000 }, async () => {
  const { host, snapshot } = await startHost()
  try {
    assert.equal(snapshot.discovery.ok, true)
    const message = await discover(snapshot.endpoints.discovery_port)
    assert.equal(message.t, 'room')
    assert.equal(message.protocol, 1)
    assert.equal(message.room_id, snapshot.room.room_id)
    assert.equal(message.name, snapshot.room.name)
    assert.equal(message.mode, 'open')
    assert.equal(message.port, snapshot.endpoints.http_port)
    assert.equal(message.capacity, 8)
    assert.equal(message.players, 0)
  } finally {
    await host.stop()
  }
})

test('租约过期标记 offline，重新心跳恢复 active', { timeout: 10_000 }, async () => {
  const { host, base, roomId } = await startHost({
    leaseTtlSeconds: 1,
    heartbeatIntervalSeconds: 1,
    expirySweepMs: 50,
  })
  try {
    const dev = device('lease')
    const joined = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, dev) })
    assert.equal(joined.status, 200)

    const offline = await waitFor(async () => {
      const snapshot = host.snapshot()
      return snapshot.members[0]?.status === 'offline' ? snapshot : null
    }, 3000)
    assert.ok(offline, '租约过期后成员应标记 offline')

    const heartbeat = await call(base, 'POST', '/lan/v1/heartbeat', {
      token: joined.body.session_token,
    })
    assert.equal(heartbeat.status, 200)
    assert.equal(host.snapshot().members[0].status, 'active')
  } finally {
    await host.stop()
  }
})

test('SSE：被踢端收到 member.kicked 并关闭连接', { timeout: 10_000 }, async () => {
  const { host, base, roomId, hostToken } = await startHost()
  let events
  try {
    const joined = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, device('sse')) })
    events = await openEvents(base, joined.body.session_token)
    assert.equal(events.response.status, 200)

    const kicked = await call(base, 'POST', `/lan/v1/members/${joined.body.member_id}/kick`, {
      hostToken,
    })
    assert.equal(kicked.status, 200)

    const received = await readEvents(events.reader, (text) => text.includes('event: member.kicked'))
    assert.match(received.text, /event: member\.kicked/)
    assert.equal(received.done, false)
  } finally {
    events?.controller.abort()
    await host.stop()
  }
})

test('SSE：散阵广播 room.closed 并关闭连接', { timeout: 10_000 }, async () => {
  const { host, base, roomId, hostToken } = await startHost()
  let events
  try {
    const joined = await call(base, 'POST', '/lan/v1/join', { body: joinBody(roomId, device('close')) })
    events = await openEvents(base, joined.body.session_token)

    const disbanded = await call(base, 'POST', '/lan/v1/disband', { hostToken })
    assert.equal(disbanded.status, 200)
    assert.equal(disbanded.body.closed, true)

    const received = await readEvents(events.reader, (text) => text.includes('event: room.closed'))
    assert.match(received.text, /event: room\.closed/)
    assert.ok(await waitFor(async () => host.state === 'standalone', 3000))
  } finally {
    events?.controller.abort()
    await host.stop()
  }
})

test('端口冲突时从起始端口向后顺延', { timeout: 10_000 }, async () => {
  const blocker = createServer()
  await new Promise((resolve) => blocker.listen(0, '127.0.0.1', resolve))
  const blockedPort = blocker.address().port
  const host = new FormationHost({ logger: () => {} })
  try {
    const snapshot = await host.open({
      name: '端口测试',
      mode: 'open',
      bind: '127.0.0.1',
      httpPort: blockedPort,
      discovery: false,
    })
    assert.notEqual(snapshot.endpoints.http_port, blockedPort)
    assert.ok(snapshot.endpoints.http_port > blockedPort)
    const health = await call(`http://127.0.0.1:${snapshot.endpoints.http_port}`, 'GET', '/lan/v1/health')
    assert.equal(health.status, 200)
  } finally {
    await host.stop()
    await new Promise((resolve) => blocker.close(resolve))
  }
})

test('默认单机；非法开阵参数被拒绝', { timeout: 10_000 }, async () => {
  const host = new FormationHost({ logger: () => {} })
  const initial = host.snapshot()
  assert.equal(initial.state, 'standalone')
  assert.equal(initial.room, null)
  assert.equal(initial.endpoints, null)

  await assert.rejects(() => host.open({ mode: 'weird' }), /mode/)
  await assert.rejects(() => host.open({ name: '' }), /阵名/)
  await assert.rejects(() => host.open({ capacity: 0 }), /容量/)
  await assert.rejects(() => host.open({ mode: 'closed', passcode: '123' }), /阵符/)
  assert.equal(host.state, 'standalone')
})

test('工具：私有地址判断与 Argon2id 阵符哈希', { timeout: 10_000 }, async () => {
  for (const address of ['127.0.0.1', '10.0.0.2', '172.16.1.1', '192.168.1.8', '169.254.1.1', '::1', 'fe80::1', 'fd00::1']) {
    assert.equal(isPrivateAddress(address), true, `${address} 应视为私有地址`)
  }
  for (const address of ['8.8.8.8', '1.1.1.1', '2001:db8::1']) {
    assert.equal(isPrivateAddress(address), false, `${address} 不应视为私有地址`)
  }

  const stored = await hashPasscode('1234')
  assert.ok(stored.salt && stored.hash)
  assert.notEqual(Buffer.from(stored.hash, 'base64').toString('utf8'), '1234')
  assert.equal(await verifyPasscode('1234', stored), true)
  assert.equal(await verifyPasscode('4321', stored), false)
})
