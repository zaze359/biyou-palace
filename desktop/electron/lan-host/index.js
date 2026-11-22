/**
 * 桌面端联阵服务（阵主 / 阵眼）。
 *
 * 默认 standalone：不创建任何监听。用户主动开阵后才绑定 HTTP 与 UDP，
 * 散阵或退出时全部关闭。协议基准：docs/lan-formation-contract.md
 */
import { EventEmitter } from 'node:events'
import { hostname } from 'node:os'
import { Room } from './room.js'
import { createLanServer } from './http.js'
import { DiscoveryService } from './discovery.js'
import {
  DEFAULTS,
  errorBody,
  generateToken,
  hashPasscode,
  platformName,
} from './util.js'

function integerOption(value, fallback, min, max, label) {
  if (value === undefined || value === null) return fallback
  const number = Number(value)
  if (!Number.isInteger(number) || number < min || number > max) {
    throw new Error(`${label}必须是 ${min}-${max} 的整数`)
  }
  return number
}

function normalizeOptions(options = {}) {
  const name = typeof options.name === 'string' ? options.name.trim() : '碧游宫'
  if (name.length === 0 || name.length > 64) throw new Error('阵名需为 1-64 个字符')

  const mode = options.mode ?? 'open'
  if (mode !== 'open' && mode !== 'closed') throw new Error('mode 必须是 open 或 closed')

  const passcode = typeof options.passcode === 'string' ? options.passcode : ''
  if (mode === 'closed' && (passcode.length < 4 || passcode.length > 64)) {
    throw new Error('闭阵阵符需为 4-64 个字符')
  }

  return {
    name,
    mode,
    passcode,
    capacity: integerOption(options.capacity, DEFAULTS.capacity, 1, 64, '容量'),
    httpPort: integerOption(options.httpPort, DEFAULTS.httpPort, 0, 65535, 'HTTP 端口'),
    discoveryPort: integerOption(options.discoveryPort, DEFAULTS.discoveryPort, 0, 65535, '发现端口'),
    bind: typeof options.bind === 'string' && options.bind ? options.bind : '0.0.0.0',
    discovery: options.discovery !== false,
    leaseTtlSeconds: integerOption(options.leaseTtlSeconds, DEFAULTS.leaseTtlSeconds, 1, 300, '租约 TTL'),
    heartbeatIntervalSeconds: integerOption(
      options.heartbeatIntervalSeconds,
      DEFAULTS.heartbeatIntervalSeconds,
      1,
      300,
      '心跳间隔',
    ),
    rateLimitAttempts: integerOption(options.rateLimitAttempts, DEFAULTS.rateLimitAttempts, 1, 1000, '限速次数'),
    rateLimitWindowMs: integerOption(options.rateLimitWindowMs, DEFAULTS.rateLimitWindowMs, 1000, 3_600_000, '限速窗口'),
    announceIntervalMs: integerOption(
      options.announceIntervalMs,
      DEFAULTS.announceIntervalMs,
      0,
      60_000,
      '公告间隔',
    ),
    expirySweepMs: integerOption(options.expirySweepMs, DEFAULTS.expirySweepMs, 10, 60_000, '租约巡检间隔'),
    keepAliveMs: integerOption(options.keepAliveMs, DEFAULTS.keepAliveMs, 1000, 120_000, 'SSE keepalive'),
    maxBodyBytes: integerOption(options.maxBodyBytes, DEFAULTS.maxBodyBytes, 1024, 8_000_000, '请求体上限'),
  }
}

function listenWithFallback(server, host, startPort) {
  const candidates = startPort === 0 ? [0] : Array.from({ length: 100 }, (_, index) => startPort + index)
  return new Promise((resolve, reject) => {
    let index = 0
    const tryNext = () => {
      if (index >= candidates.length) {
        reject(new Error(`无法绑定 HTTP 端口 ${startPort}-${startPort + 99}`))
        return
      }
      const port = candidates[index++]
      const onError = (error) => {
        server.off('listening', onListening)
        if (error.code === 'EADDRINUSE') {
          tryNext()
          return
        }
        reject(error)
      }
      const onListening = () => {
        server.off('error', onError)
        resolve(server.address().port)
      }
      server.once('error', onError)
      server.once('listening', onListening)
      server.listen(port, host)
    }
    tryNext()
  })
}

function closeServer(server) {
  return new Promise((resolve) => {
    let done = false
    const finish = () => {
      if (done) return
      done = true
      resolve()
    }
    server.close(finish)
    server.closeIdleConnections?.()
    const timer = setTimeout(() => {
      server.closeAllConnections?.()
      finish()
    }, 300)
    timer.unref?.()
  })
}

export class FormationHost extends EventEmitter {
  constructor({
    logger = () => {},
    hostName = hostname(),
    platform = platformName(),
    appVersion = '0.1.0',
  } = {}) {
    super()
    this.logger = logger
    this.hostName = hostName
    this.platform = platform
    this.appVersion = appVersion
    this.state = 'standalone'
    this.room = null
    this.server = null
    this.discovery = null
    this.discoveryInfo = null
    this.hostToken = null
    this.httpPort = 0
    this.bind = '0.0.0.0'
    this.expiryTimer = null
    this.stopPromise = null
  }

  get hosting() {
    return this.state === 'hosting'
  }

  snapshot({ includeHostToken = false } = {}) {
    const room = this.room
    const httpBase = room ? `http://${this.bind === '0.0.0.0' ? '127.0.0.1' : this.bind}:${this.httpPort}/lan/v1` : null
    return {
      state: this.state,
      host: {
        name: this.hostName,
        platform: this.platform,
        app_version: this.appVersion,
      },
      room: room?.view() ?? null,
      members: room?.memberViews() ?? [],
      endpoints: room
        ? {
            http: httpBase,
            http_port: this.httpPort,
            events: `${httpBase}/events`,
            discovery_port: this.discoveryInfo?.port ?? null,
          }
        : null,
      discovery: this.discoveryInfo,
      ...(includeHostToken && this.hostToken ? { hostToken: this.hostToken } : {}),
    }
  }

  emitStatus() {
    try {
      this.emit('status', this.snapshot())
    } catch (error) {
      this.logger(`[lan] 状态广播失败：${error.message}`)
    }
  }

  async open(options = {}) {
    if (this.state !== 'standalone') throw new Error('联阵已在运行，请先散阵')
    const config = normalizeOptions(options)

    this.state = 'starting'
    this.emitStatus()

    let room = null
    let server = null
    let discovery = null
    try {
      const passcode = config.mode === 'closed' ? await hashPasscode(config.passcode) : null
      room = new Room({
        name: config.name,
        mode: config.mode,
        capacity: config.capacity,
        hostName: this.hostName,
        hostPlatform: this.platform,
        appVersion: this.appVersion,
        passcode,
        leaseTtlSeconds: config.leaseTtlSeconds,
        heartbeatIntervalSeconds: config.heartbeatIntervalSeconds,
        rateLimitAttempts: config.rateLimitAttempts,
        rateLimitWindowMs: config.rateLimitWindowMs,
      })
      const hostToken = generateToken()
      server = createLanServer({
        room,
        hostToken,
        keepAliveMs: config.keepAliveMs,
        maxBodyBytes: config.maxBodyBytes,
        logger: this.logger,
      })
      const port = await listenWithFallback(server, config.bind, config.httpPort)
      room.httpPort = port

      this.room = room
      this.server = server
      this.hostToken = hostToken
      this.httpPort = port
      this.bind = config.bind
      this.discoveryInfo = { enabled: false, ok: false, port: null }

      if (config.discovery) {
        discovery = new DiscoveryService({
          room,
          port: config.discoveryPort,
          announceIntervalMs: config.announceIntervalMs,
          logger: this.logger,
        })
        try {
          const info = await discovery.start()
          this.discovery = discovery
          this.discoveryInfo = { enabled: true, ok: true, port: info.port }
        } catch (error) {
          discovery.close()
          this.discoveryInfo = { enabled: true, ok: false, port: null, error: error.message }
          this.logger(`[lan] UDP 发现不可用，HTTP 入阵仍可工作：${error.message}`)
        }
      }

      this.expiryTimer = setInterval(() => room.expire(), config.expirySweepMs)
      this.expiryTimer.unref?.()
      room.on('change', () => this.emitStatus())
      room.once('disbanded', () => this.onRoomDisbanded())

      this.state = 'hosting'
      this.emitStatus()
      this.logger(
        `[lan] 开阵 name=${room.name} mode=${room.mode} http=:${port} discovery=${
          this.discoveryInfo?.ok ? `:${this.discoveryInfo.port}` : 'off'
        }`,
      )
      return this.snapshot()
    } catch (error) {
      discovery?.close()
      room?.disband()
      if (server) await closeServer(server).catch(() => {})
      this.reset()
      this.state = 'standalone'
      this.emitStatus()
      throw error
    }
  }

  kick(memberId) {
    if (!this.room) return { status: 404, body: errorBody('room_not_found', '阵不存在或已解散') }
    return this.room.kick(memberId)
  }

  async disband() {
    return await this.stop()
  }

  async stop() {
    if (this.stopPromise) return this.stopPromise
    if (this.state === 'standalone') return this.snapshot()
    this.stopPromise = this.shutdown()
    try {
      return await this.stopPromise
    } finally {
      this.stopPromise = null
    }
  }

  async shutdown() {
    this.state = 'stopping'
    const room = this.room
    const server = this.server
    this.room = null
    this.server = null

    room?.disband()
    this.discovery?.close()
    this.discovery = null
    if (this.expiryTimer) {
      clearInterval(this.expiryTimer)
      this.expiryTimer = null
    }
    if (server) await closeServer(server)

    this.hostToken = null
    this.httpPort = 0
    this.discoveryInfo = null
    this.state = 'standalone'
    this.emitStatus()
    this.logger('[lan] 散阵完成')
    return this.snapshot()
  }

  reset() {
    this.room = null
    this.server = null
    this.discovery = null
    this.discoveryInfo = null
    this.hostToken = null
    this.httpPort = 0
    if (this.expiryTimer) {
      clearInterval(this.expiryTimer)
      this.expiryTimer = null
    }
  }

  onRoomDisbanded() {
    if (this.state !== 'hosting') return
    this.state = 'stopping'
    this.emitStatus()
    // 让 disband 的 HTTP 响应先写回客户端，再关闭监听
    const timer = setTimeout(() => {
      this.stop().catch((error) => this.logger(`[lan] 散阵清理失败：${error.message}`))
    }, 150)
    timer.unref?.()
  }
}

export { Room } from './room.js'
export { DiscoveryService } from './discovery.js'
