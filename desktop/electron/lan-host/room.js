/**
 * 一个联阵会话（Room）的状态机。
 *
 * 只处理协议内的事情：成员、令牌、租约、封禁、限速、事件。
 * 不碰 HTTP/UDP，方便单测直接驱动。
 */
import { EventEmitter } from 'node:events'
import { randomUUID } from 'node:crypto'
import {
  DEFAULTS,
  PROTOCOL,
  errorBody,
  generateToken,
  isPlainObject,
  isValidDevice,
  safeEqual,
  sha256Hex,
  utcNowIso,
  verifyPasscode,
} from './util.js'

export class Room extends EventEmitter {
  constructor({
    name,
    mode,
    capacity = DEFAULTS.capacity,
    hostName,
    hostPlatform,
    appVersion,
    passcode = null,
    leaseTtlSeconds = DEFAULTS.leaseTtlSeconds,
    heartbeatIntervalSeconds = DEFAULTS.heartbeatIntervalSeconds,
    rateLimitAttempts = DEFAULTS.rateLimitAttempts,
    rateLimitWindowMs = DEFAULTS.rateLimitWindowMs,
    clock = () => Date.now(),
  }) {
    super()
    this.protocol = PROTOCOL
    this.roomId = randomUUID()
    this.name = name
    this.mode = mode
    this.capacity = capacity
    this.hostName = hostName
    this.hostPlatform = hostPlatform
    this.appVersion = appVersion
    this.startedAt = utcNowIso(clock())
    this.leaseTtlMs = leaseTtlSeconds * 1000
    this.heartbeatIntervalSeconds = heartbeatIntervalSeconds
    this.rateLimitAttempts = rateLimitAttempts
    this.rateLimitWindowMs = rateLimitWindowMs
    this.httpPort = 0
    this.clock = clock
    this.active = true
    this.passcode = passcode
    this.members = new Map()
    this.bans = new Set()
    this.rate = new Map()
    this.memberListeners = new Map()
  }

  get players() {
    return this.members.size
  }

  view() {
    return {
      protocol: this.protocol,
      room_id: this.roomId,
      name: this.name,
      mode: this.mode,
      host: {
        name: this.hostName,
        platform: this.hostPlatform,
        app_version: this.appVersion,
      },
      capacity: this.capacity,
      players: this.players,
      started_at: this.startedAt,
      lease: {
        ttl_seconds: this.leaseTtlMs / 1000,
        heartbeat_interval_seconds: this.heartbeatIntervalSeconds,
      },
    }
  }

  discoveryView() {
    return {
      t: 'room',
      v: PROTOCOL,
      room_id: this.roomId,
      name: this.name,
      mode: this.mode,
      protocol: PROTOCOL,
      capacity: this.capacity,
      players: this.players,
      host: this.hostName,
      app: this.appVersion,
      port: this.httpPort,
      started_at: this.startedAt,
    }
  }

  memberViews() {
    return [...this.members.values()].map((member) => ({
      member_id: member.memberId,
      device_id: member.deviceId,
      name: member.name,
      platform: member.platform,
      app_version: member.appVersion,
      role: member.role,
      agent: member.agent,
      status: member.status,
      joined_at: utcNowIso(member.joinedAt),
      last_seen: utcNowIso(member.lastSeen),
      lease_expires_at: utcNowIso(member.leaseExpiresAt),
    }))
  }

  snapshot() {
    return { room: this.view(), members: this.memberViews() }
  }

  /** 同一 IP + device_id 每分钟最多 5 次入阵尝试，先于其它校验 */
  allowJoinAttempt(ip, deviceId, now = this.clock()) {
    const key = `${ip}|${deviceId || '-'}`
    const attempts = this.rate.get(key) ?? []
    const cutoff = now - this.rateLimitWindowMs
    while (attempts.length > 0 && attempts[0] <= cutoff) attempts.shift()
    if (attempts.length >= this.rateLimitAttempts) {
      this.rate.set(key, attempts)
      return false
    }
    attempts.push(now)
    this.rate.set(key, attempts)
    return true
  }

  onMemberEvent(memberId, listener) {
    const listeners = this.memberListeners.get(memberId) ?? new Set()
    listeners.add(listener)
    this.memberListeners.set(memberId, listeners)
    return () => {
      listeners.delete(listener)
      if (listeners.size === 0) this.memberListeners.delete(memberId)
    }
  }

  publish(memberId, event) {
    const listeners = this.memberListeners.get(memberId)
    if (!listeners) return
    for (const listener of [...listeners]) {
      try {
        listener(event)
      } catch {
        /* SSE 写失败由连接自身清理，不影响其它成员 */
      }
    }
  }

  broadcast(event) {
    for (const memberId of this.members.keys()) this.publish(memberId, event)
  }

  closeMemberEvents(memberId) {
    this.publish(memberId, null)
    this.memberListeners.delete(memberId)
  }

  roomStateEvent() {
    return { type: 'room.state', data: { players: this.players, capacity: this.capacity } }
  }

  emitChange() {
    this.emit('change', this.snapshot())
  }

  authenticate(token) {
    if (typeof token !== 'string' || token.length === 0) return null
    const tokenHash = sha256Hex(token)
    for (const member of this.members.values()) {
      if (safeEqual(member.tokenHash, tokenHash)) return member
    }
    return null
  }

  findExisting(deviceId) {
    if (!deviceId) return null
    for (const member of this.members.values()) {
      if (member.deviceId === deviceId) return member
    }
    return null
  }

  async join(payload, ip) {
    const device = isPlainObject(payload) ? payload.device : undefined
    const rawDeviceId =
      isPlainObject(device) && typeof device.device_id === 'string' ? device.device_id : ''
    const now = this.clock()

    if (!this.allowJoinAttempt(ip, rawDeviceId, now)) {
      return {
        status: 429,
        body: errorBody('rate_limited', '入阵尝试过于频繁'),
        headers: { 'Retry-After': String(Math.ceil(this.rateLimitWindowMs / 1000)) },
      }
    }

    if (!isPlainObject(payload)) return { status: 400, body: errorBody('invalid_request', '请求体必须是 JSON 对象') }
    if (payload.protocol !== PROTOCOL) {
      return { status: 409, body: errorBody('protocol_mismatch', '协议版本不兼容') }
    }
    if (payload.room_id !== this.roomId) {
      return { status: 404, body: errorBody('room_not_found', '阵不存在或已更换') }
    }
    if (!this.active) return { status: 409, body: errorBody('room_closed', '阵已散') }

    let existing = this.findExisting(rawDeviceId)
    const effectivePlayers = this.members.size - (existing ? 1 : 0)
    if (effectivePlayers >= this.capacity) return { status: 409, body: errorBody('room_full', '阵已满') }

    if (!isValidDevice(device)) return { status: 400, body: errorBody('invalid_device', '设备字段缺失或非法') }
    if (this.bans.has(device.device_id)) {
      return { status: 403, body: errorBody('banned', '该设备已被阵主移出本次阵') }
    }

    if (this.mode === 'closed') {
      const passcode = payload.passcode
      if (typeof passcode !== 'string' || passcode.length === 0) {
        return { status: 401, body: errorBody('passcode_required', '闭阵需要阵符') }
      }
      if (!(await verifyPasscode(passcode, this.passcode))) {
        return { status: 403, body: errorBody('passcode_invalid', '阵符错误') }
      }
      // 校验阵符期间阵可能已散、容量可能已变，重新确认一次
      if (!this.active) return { status: 409, body: errorBody('room_closed', '阵已散') }
      existing = this.findExisting(device.device_id)
      const currentEffective = this.members.size - (existing ? 1 : 0)
      if (currentEffective >= this.capacity) return { status: 409, body: errorBody('room_full', '阵已满') }
      if (this.bans.has(device.device_id)) {
        return { status: 403, body: errorBody('banned', '该设备已被阵主移出本次阵') }
      }
    }

    if (existing) {
      this.members.delete(existing.memberId)
      this.closeMemberEvents(existing.memberId)
      this.broadcast({
        type: 'member.left',
        data: { member_id: existing.memberId, name: existing.name, reason: 'replaced' },
      })
    }

    const token = generateToken()
    const agent = isPlainObject(payload.agent) ? payload.agent : {}
    const role = agent.root === 'inner' ? 'inner' : 'outer'
    const member = {
      memberId: randomUUID(),
      deviceId: device.device_id,
      name: device.name,
      platform: device.platform,
      appVersion: device.app_version,
      role,
      agent,
      tokenHash: sha256Hex(token),
      joinedAt: now,
      lastSeen: now,
      leaseExpiresAt: now + this.leaseTtlMs,
      status: 'active',
    }
    this.members.set(member.memberId, member)

    this.broadcast({
      type: 'member.joined',
      data: { member_id: member.memberId, name: member.name, platform: member.platform, role: member.role },
    })
    this.broadcast(this.roomStateEvent())
    this.emitChange()

    return {
      status: 200,
      body: {
        ok: true,
        member_id: member.memberId,
        session_token: token,
        role: member.role,
        room: this.view(),
        lease: {
          ttl_seconds: this.leaseTtlMs / 1000,
          heartbeat_interval_seconds: this.heartbeatIntervalSeconds,
        },
      },
      token,
      member,
    }
  }

  heartbeat(member, now = this.clock()) {
    const current = this.members.get(member.memberId)
    if (!current) return { status: 401, body: errorBody('invalid_token', '会话不存在或已失效') }
    current.lastSeen = now
    current.leaseExpiresAt = now + this.leaseTtlMs
    if (current.status !== 'active') {
      current.status = 'active'
      this.emitChange()
    }
    return {
      status: 200,
      body: {
        ok: true,
        server_time: utcNowIso(now),
        lease_expires_at: utcNowIso(current.leaseExpiresAt),
        players: this.players,
      },
    }
  }

  leave(member) {
    const removed = this.members.get(member.memberId)
    if (!removed) return { status: 401, body: errorBody('invalid_token', '会话不存在或已失效') }
    this.members.delete(removed.memberId)
    this.closeMemberEvents(removed.memberId)
    this.broadcast({
      type: 'member.left',
      data: { member_id: removed.memberId, name: removed.name, reason: 'left' },
    })
    this.broadcast(this.roomStateEvent())
    this.emitChange()
    return { status: 200, body: { ok: true } }
  }

  kick(memberId) {
    const removed = this.members.get(memberId)
    if (!removed) return { status: 404, body: errorBody('member_not_found', '成员不存在') }
    this.members.delete(memberId)
    this.bans.add(removed.deviceId)
    const data = { member_id: removed.memberId, name: removed.name, reason: 'kicked' }
    this.publish(removed.memberId, { type: 'member.kicked', data })
    this.closeMemberEvents(removed.memberId)
    this.broadcast({ type: 'member.kicked', data })
    this.broadcast(this.roomStateEvent())
    this.emitChange()
    return { status: 200, body: { ok: true } }
  }

  /** 租约过期只标 offline，保留成员与令牌；重新心跳可恢复 active */
  expire(now = this.clock()) {
    const offline = []
    for (const member of this.members.values()) {
      if (member.status === 'active' && member.leaseExpiresAt <= now) {
        member.status = 'offline'
        offline.push(member)
      }
    }
    for (const member of offline) {
      this.broadcast({ type: 'member.offline', data: { member_id: member.memberId, name: member.name } })
    }
    if (offline.length > 0) this.emitChange()
    return offline
  }

  disband() {
    if (!this.active) return false
    this.active = false
    const members = [...this.members.values()]
    const data = { room_id: this.roomId, reason: 'host_disbanded' }
    for (const member of members) {
      this.publish(member.memberId, { type: 'room.closed', data })
      this.closeMemberEvents(member.memberId)
    }
    this.members.clear()
    this.bans.clear()
    this.rate.clear()
    this.emit('disbanded')
    this.emitChange()
    return true
  }
}
