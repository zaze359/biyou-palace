/**
 * 联阵 HTTP/JSON + SSE 服务。
 *
 * 路由与错误码严格按 docs/lan-formation-contract.md：
 *   GET  /lan/v1/health
 *   GET  /lan/v1/room
 *   POST /lan/v1/join
 *   POST /lan/v1/heartbeat
 *   POST /lan/v1/leave
 *   GET  /lan/v1/events
 *   GET  /lan/v1/members                    （阵主：仅回环 + host token）
 *   POST /lan/v1/members/{id}/kick          （阵主）
 *   POST /lan/v1/disband                    （阵主）
 */
import { createServer } from 'node:http'
import {
  DEFAULTS,
  errorBody,
  isLoopbackAddress,
  isPrivateAddress,
  safeEqual,
} from './util.js'

const BASE = '/lan/v1'

function sendJson(res, status, payload, headers = {}) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
    ...headers,
  })
  res.end(body)
}

function bearerToken(req) {
  const header = req.headers.authorization
  if (typeof header !== 'string' || !header.startsWith('Bearer ')) return ''
  return header.slice(7).trim()
}

function hostAuthorized(req, hostToken) {
  if (!isLoopbackAddress(req.socket.remoteAddress)) return false
  return safeEqual(req.headers['x-biyou-host-token'] ?? '', hostToken)
}

async function readJson(req, limit) {
  const declared = Number(req.headers['content-length'] ?? 0)
  if (Number.isFinite(declared) && declared > limit) return null
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > limit) {
      // 不 destroy：保留连接把剩余字节排空，仍按 invalid_request 回一个 JSON 错误
      req.resume()
      return null
    }
    chunks.push(chunk)
  }
  if (size === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    return null
  }
}

function streamEvents(req, res, room, keepAliveMs) {
  const member = room.authenticate(bearerToken(req))
  if (!member) {
    sendJson(res, 401, errorBody('invalid_token', '会话不存在或已失效'))
    return
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  })
  res.flushHeaders?.()

  let closed = false
  let timer = null
  let unsubscribe = () => {}

  const finish = () => {
    if (closed) return
    closed = true
    if (timer) clearInterval(timer)
    unsubscribe()
    if (!res.writableEnded) {
      try {
        res.end()
      } catch {
        /* 连接已断开 */
      }
    }
  }

  unsubscribe = room.onMemberEvent(member.memberId, (event) => {
    if (closed) return
    if (!event) {
      finish()
      return
    }
    try {
      res.write(`event: ${event.type}\ndata: ${JSON.stringify(event.data)}\n\n`)
    } catch {
      finish()
      return
    }
    if (event.type === 'member.kicked' || event.type === 'room.closed') finish()
  })

  timer = setInterval(() => {
    if (closed) return
    try {
      res.write(': keepalive\n\n')
    } catch {
      finish()
    }
  }, keepAliveMs)
  timer.unref?.()

  // 只监听 response 的 close：IncomingMessage 的 close 在请求体读完时就可能触发，
  // 会把 SSE 刚建立就关掉。
  res.on('close', finish)
}

async function handleRequest(req, res, { room, hostToken, keepAliveMs, maxBodyBytes }) {
  const remote = req.socket.remoteAddress
  if (!isPrivateAddress(remote)) {
    sendJson(res, 403, errorBody('forbidden', '联阵仅限局域网访问'))
    return
  }

  let path
  try {
    path = new URL(req.url, 'http://localhost').pathname.replace(/\/+$/, '') || '/'
  } catch {
    sendJson(res, 400, errorBody('invalid_request', '请求路径非法'))
    return
  }

  if (req.method === 'GET') {
    if (path === '/') {
      sendJson(res, 200, {
        ok: true,
        service: 'biyou-lan-formation',
        protocol: 1,
        endpoints: [
          'GET /lan/v1/health',
          'GET /lan/v1/room',
          'POST /lan/v1/join',
          'POST /lan/v1/heartbeat',
          'POST /lan/v1/leave',
          'GET /lan/v1/events',
          'GET /lan/v1/members',
          'POST /lan/v1/members/{member_id}/kick',
          'POST /lan/v1/disband',
        ],
      })
      return
    }
    if (path === `${BASE}/health`) {
      sendJson(res, 200, { ok: true, service: 'biyou-lan-formation', protocol: 1 })
      return
    }
    if (path === `${BASE}/room`) {
      sendJson(res, 200, { ok: true, room: room.view() })
      return
    }
    if (path === `${BASE}/members`) {
      if (!hostAuthorized(req, hostToken)) {
        sendJson(res, 403, errorBody('forbidden', '阵主接口仅限本机'))
        return
      }
      sendJson(res, 200, { ok: true, members: room.memberViews() })
      return
    }
    if (path === `${BASE}/events`) {
      streamEvents(req, res, room, keepAliveMs)
      return
    }
    sendJson(res, 404, errorBody('not_found', '端点不存在'))
    return
  }

  if (req.method === 'POST') {
    if (path === `${BASE}/join`) {
      // 非法 JSON 也交给 room.join：先计限速，再按 invalid_request 拒绝，
      // 与契约「先限速再校验」的校验顺序一致。
      const payload = await readJson(req, maxBodyBytes)
      const result = await room.join(payload, remote ?? '')
      sendJson(res, result.status, result.body, result.headers)
      return
    }

    if (path === `${BASE}/heartbeat`) {
      const member = room.authenticate(bearerToken(req))
      if (!member) {
        sendJson(res, 401, errorBody('invalid_token', '会话不存在或已失效'))
        return
      }
      const result = room.heartbeat(member)
      sendJson(res, result.status, result.body)
      return
    }

    if (path === `${BASE}/leave`) {
      const member = room.authenticate(bearerToken(req))
      if (!member) {
        sendJson(res, 401, errorBody('invalid_token', '会话不存在或已失效'))
        return
      }
      const result = room.leave(member)
      sendJson(res, result.status, result.body)
      return
    }

    if (path === `${BASE}/disband`) {
      if (!hostAuthorized(req, hostToken)) {
        sendJson(res, 403, errorBody('forbidden', '阵主接口仅限本机'))
        return
      }
      const closed = room.disband()
      sendJson(res, 200, { ok: true, closed })
      return
    }

    const kickMatch = path.match(/^\/lan\/v1\/members\/([^/]+)\/kick$/)
    if (kickMatch) {
      if (!hostAuthorized(req, hostToken)) {
        sendJson(res, 403, errorBody('forbidden', '阵主接口仅限本机'))
        return
      }
      let memberId
      try {
        memberId = decodeURIComponent(kickMatch[1])
      } catch {
        sendJson(res, 400, errorBody('invalid_request', '成员 ID 非法'))
        return
      }
      const result = room.kick(memberId)
      sendJson(res, result.status, result.body)
      return
    }

    sendJson(res, 404, errorBody('not_found', '端点不存在'))
    return
  }

  sendJson(res, 404, errorBody('not_found', '端点不存在'))
}

export function createLanServer({ room, hostToken, keepAliveMs = DEFAULTS.keepAliveMs, maxBodyBytes = DEFAULTS.maxBodyBytes, logger = () => {} }) {
  const server = createServer((req, res) => {
    handleRequest(req, res, { room, hostToken, keepAliveMs, maxBodyBytes }).catch((error) => {
      logger(`[lan] 请求处理失败：${error.message}`)
      if (!res.headersSent) {
        sendJson(res, 500, errorBody('internal_error', '阵主内部错误'))
      } else {
        res.destroy()
      }
    })
  })

  server.on('clientError', (error, socket) => {
    logger(`[lan] 客户端请求非法：${error.message}`)
    if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\n\r\n')
  })

  return server
}
