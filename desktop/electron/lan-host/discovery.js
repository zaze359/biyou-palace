/**
 * UDP 广播发现（mDNS 的兜底路径）。
 *
 * - 每 2 秒向 255.255.255.255 与各局域网接口定向广播地址公告一次；
 * - 收到 {"t":"discover","v":1} 时单播回一条 room 报文；
 * - 只发房间公开信息，不含阵符、令牌或任何秘密。
 */
import { createSocket } from 'node:dgram'
import { DEFAULTS, PROTOCOL, broadcastAddresses } from './util.js'

export class DiscoveryService {
  constructor({ room, port = DEFAULTS.discoveryPort, announceIntervalMs = DEFAULTS.announceIntervalMs, logger = () => {} }) {
    this.room = room
    this.port = port
    this.announceIntervalMs = announceIntervalMs
    this.logger = logger
    this.socket = null
    this.timer = null
    this.actualPort = 0
    this.addresses = []
  }

  async start() {
    if (this.socket) return { port: this.actualPort }
    const socket = createSocket({ type: 'udp4', reuseAddr: true })
    this.socket = socket
    this.addresses = broadcastAddresses()

    await new Promise((resolve, reject) => {
      const onError = (error) => {
        socket.off('listening', onListening)
        reject(error)
      }
      const onListening = () => {
        socket.off('error', onError)
        resolve()
      }
      socket.once('error', onError)
      socket.once('listening', onListening)
      socket.bind({ address: '0.0.0.0', port: this.port, exclusive: false })
    })

    socket.on('error', (error) => this.logger(`[lan] UDP 发现错误：${error.message}`))
    socket.on('message', (message, rinfo) => this.onMessage(message, rinfo))
    try {
      socket.setBroadcast(true)
      socket.setTTL(1)
    } catch (error) {
      this.logger(`[lan] UDP 广播配置失败：${error.message}`)
    }

    this.actualPort = socket.address().port
    if (this.announceIntervalMs > 0) {
      this.timer = setInterval(() => this.announce(), this.announceIntervalMs)
      this.timer.unref?.()
      this.announce()
    }
    return { port: this.actualPort }
  }

  onMessage(message, rinfo) {
    let payload
    try {
      payload = JSON.parse(Buffer.from(message).toString('utf8'))
    } catch {
      return
    }
    if (payload?.t !== 'discover' || payload?.v !== PROTOCOL) return
    if (!this.room.active) return
    this.send(this.encodeRoom(), rinfo.address, rinfo.port)
  }

  announce() {
    if (!this.socket || !this.room.active) return
    const body = this.encodeRoom()
    for (const address of this.addresses) this.send(body, address, this.actualPort)
  }

  encodeRoom() {
    return Buffer.from(JSON.stringify(this.room.discoveryView()), 'utf8')
  }

  send(body, address, port) {
    if (!this.socket) return
    try {
      this.socket.send(body, port, address, () => {})
    } catch {
      /* 某些平台不允许全局广播，定向广播仍会发出 */
    }
  }

  close() {
    if (this.timer) {
      clearInterval(this.timer)
      this.timer = null
    }
    const socket = this.socket
    this.socket = null
    if (!socket) return
    try {
      socket.close()
    } catch {
      /* 已关闭 */
    }
  }
}
