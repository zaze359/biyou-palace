/**
 * 同源 JSON bridge：设置页只通过这两个精确路由访问 Host。
 *
 * - 先过 composition 的 `connection.requestRejection()`：Host/Origin 栅栏 +
 *   浏览器登录校验由连接服务统一负责；
 * - 只接受白名单 action，不接受前端传 URL/命令/任意路径；
 * - 请求体上限 256KB，返回值统一 `{ok, value | error}`。
 */
export const STATUS_PATH = '/api/biyou-local/status'
export const ACTION_PATH = '/api/biyou-local/action'
const MAX_BODY_BYTES = 256 * 1024

/**
 * @param {{webServer: object, connection: object, status: () => Promise<object>, actions: Record<string, (args: object) => Promise<unknown>>, logger?: object}} options
 */
export function registerBridge({ webServer, connection, status, actions, logger = console }) {
  const disposers = []
  disposers.push(webServer.register({
    kind: 'exact',
    path: STATUS_PATH,
    handler: async (req, res) => {
      if (rejected(req, res, connection)) return
      if (req.method !== 'GET') return methodNotAllowed(res, 'GET')
      try {
        sendJson(res, 200, { ok: true, value: await status() })
      } catch (error) {
        logger.error?.(`[biyou-local-models] status 失败：${error.message}`)
        sendJson(res, 500, { ok: false, error: { code: error.code ?? 'STATUS_FAILED', message: error.message } })
      }
    },
  }))

  disposers.push(webServer.register({
    kind: 'exact',
    path: ACTION_PATH,
    handler: async (req, res) => {
      if (rejected(req, res, connection)) return
      if (req.method !== 'POST') return methodNotAllowed(res, 'POST')
      if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
        return sendJson(res, 415, { ok: false, error: { code: 'INVALID_CONTENT_TYPE', message: '只接受 application/json' } })
      }
      const body = await readBoundedBody(req)
      if (body === null) {
        return sendJson(res, 413, { ok: false, error: { code: 'BODY_TOO_LARGE', message: '请求体超过 256KB' } })
      }
      let payload
      try {
        payload = JSON.parse(body)
      } catch {
        return sendJson(res, 400, { ok: false, error: { code: 'INVALID_JSON', message: '请求体不是合法 JSON' } })
      }
      const action = payload?.action
      const args = payload?.args ?? {}
      if (typeof action !== 'string' || !Object.hasOwn(actions, action)) {
        return sendJson(res, 200, { ok: false, error: { code: 'UNKNOWN_ACTION', message: `不支持的操作：${String(action)}` } })
      }
      if (typeof args !== 'object' || args === null || Array.isArray(args)) {
        return sendJson(res, 400, { ok: false, error: { code: 'INVALID_ARGS', message: 'args 必须是对象' } })
      }
      try {
        const value = await actions[action](args)
        sendJson(res, 200, { ok: true, value: value ?? null })
      } catch (error) {
        logger.warn?.(`[biyou-local-models] ${action} 失败：${error.message}`)
        sendJson(res, 200, { ok: false, error: { code: error.code ?? 'ACTION_FAILED', message: error.message } })
      }
    },
  }))

  return () => {
    for (const dispose of disposers) dispose()
  }
}

function rejected(req, res, connection) {
  const rejection = connection?.requestRejection?.(req)
  if (rejection === undefined) return false
  res.statusCode = rejection
  res.end()
  return true
}

function sendJson(res, status, payload) {
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(JSON.stringify(payload))
}

function methodNotAllowed(res, allow) {
  res.statusCode = 405
  res.setHeader('allow', allow)
  res.end()
}

function readBoundedBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        req.resume()
        resolve(null)
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', () => resolve(null))
  })
}
