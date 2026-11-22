#!/usr/bin/env node
/**
 * 集成测试用的假 llama-server（router 模式）。
 *
 * 只实现插件实际用到的端点：/props、/v1/models、/models/load、
 * /models/unload、/v1/chat/completions（流式与非流式）。它不是产品代码，
 * 只负责把 supervisor/provider 的行为稳定地跑起来。
 */
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

export async function main(argv, options = {}) {
  const args = parseArgs(argv)
  const host = args.host ?? '127.0.0.1'
  const port = Number(args.port) || 0
  const keyFile = args['api-key-file']
  const key = keyFile ? (await readFile(keyFile, 'utf8')).trim() : null
  const models = await readPreset(args['models-preset'])
  const loaded = new Set()
  const server = createServer((req, res) => {
    handle(req, res, { key, models, loaded, log: options.log ?? (() => {}) }).catch((error) => {
      res.statusCode = 500
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({ error: { message: error.message } }))
    })
  })
  await new Promise((resolve) => server.listen(port, host, resolve))
  const address = server.address()
  const base = `http://${host}:${address.port}`
  console.log(`fake llama-server listening on ${base}`)
  const close = () => new Promise((resolve) => server.close(resolve))
  process.on('SIGTERM', () => { void close().then(() => process.exit(0)) })
  process.on('SIGINT', () => { void close().then(() => process.exit(0)) })
  return { server, base, close }
}

async function handle(req, res, state) {
  const url = new URL(req.url, 'http://127.0.0.1')
  if (state.key && req.headers.authorization !== `Bearer ${state.key}`) {
    res.statusCode = 401
    res.end()
    return
  }
  if (req.method === 'GET' && url.pathname === '/props') {
    return json(res, { role: 'router', build_info: 'fake-b1', model_path: 'none', max_instances: 1 })
  }
  if (req.method === 'GET' && url.pathname === '/v1/models') {
    return json(res, {
      object: 'list',
      data: state.models.map((model) => ({
        id: model.id,
        aliases: [],
        object: 'model',
        owned_by: 'llamacpp',
        status: { value: state.loaded.has(model.id) ? 'loaded' : 'unloaded' },
        context_length: model.ctx,
      })),
    })
  }
  if (req.method === 'POST' && url.pathname === '/models/load') {
    const body = await readJsonBody(req)
    const id = body.model
    if (!state.models.some((model) => model.id === id)) return json(res, { error: { message: 'unknown model' } }, 404)
    setTimeout(() => state.loaded.add(id), 80)
    return json(res, { success: true })
  }
  if (req.method === 'POST' && url.pathname === '/models/unload') {
    const body = await readJsonBody(req)
    state.loaded.delete(body.model)
    return json(res, { success: true })
  }
  if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
    const body = await readJsonBody(req)
    if (!state.models.some((model) => model.id === body.model)) {
      return json(res, { error: { message: 'unknown model' } }, 404)
    }
    state.loaded.add(body.model)
    const lastUser = [...(body.messages ?? [])].reverse().find((message) => message.role === 'user')
    const text = typeof lastUser?.content === 'string' ? lastUser.content : lastUser?.content?.[0]?.text ?? ''
    if (!body.stream) {
      const wantsTool = Array.isArray(body.tools) && /天气/.test(text)
      return json(res, {
        choices: [{
          finish_reason: wantsTool ? 'tool_calls' : 'stop',
          message: wantsTool
            ? { role: 'assistant', content: '', tool_calls: [{ id: 'call_fake', type: 'function', function: { name: 'get_weather', arguments: '{"city":"北京"}' } }] }
            : { role: 'assistant', content: /OK/.test(text) ? 'OK' : '你好' },
        }],
        usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
      })
    }
    return await streamChat(req, res, { text, tools: body.tools, model: body.model })
  }
  res.statusCode = 404
  res.end()
}

async function streamChat(req, res, { text, tools, model }) {
  res.writeHead(200, {
    'content-type': 'text/event-stream',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  })
  const wantsTool = Array.isArray(tools) && /天气/.test(text)
  const write = (payload) => res.write(`data: ${JSON.stringify(payload)}\n\n`)
  let closed = false
  res.on('close', () => { closed = true })
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
  write({ model, choices: [{ index: 0, delta: { role: 'assistant' } }] })
  if (wantsTool) {
    write({ model, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_fake', function: { name: 'get_weather', arguments: '{"city"' } }] } }] })
    await wait(10)
    write({ model, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: ':"北京"}' } }] } }] })
    await wait(10)
    write({ model, choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] })
  } else {
    for (const part of ['你', '好']) {
      if (closed) return
      write({ model, choices: [{ index: 0, delta: { content: part } }] })
      await wait(10)
    }
    write({ model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })
  }
  write({ model, choices: [], usage: { prompt_tokens: 10, completion_tokens: 3, total_tokens: 13, prompt_tokens_details: { cached_tokens: 2 } } })
  res.write('data: [DONE]\n\n')
  res.end()
}

function parseArgs(argv) {
  const out = {}
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (!arg.startsWith('--')) continue
    const name = arg.slice(2)
    const next = argv[index + 1]
    if (next && !next.startsWith('--')) {
      out[name] = next
      index += 1
    } else {
      out[name] = true
    }
  }
  return out
}

async function readPreset(file) {
  if (!file) return []
  const text = await readFile(file, 'utf8')
  const models = []
  const lines = text.split(/\r?\n/)
  let current = null
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const section = /^\[(.+)]$/.exec(line)
    if (section) {
      current = { id: section[1], ctx: 4096, model: null }
      models.push(current)
      continue
    }
    if (!current) continue
    const pair = /^([\w-]+)\s*=\s*(.*)$/.exec(line)
    if (!pair) continue
    if (pair[1] === 'ctx-size') current.ctx = Number(pair[2]) || 4096
    if (pair[1] === 'model') current.model = pair[2]
  }
  return models
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (chunk) => chunks.push(chunk))
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'))
      } catch (error) {
        reject(error)
      }
    })
    req.on('error', reject)
  })
}

function json(res, payload, status = 200) {
  res.statusCode = status
  res.setHeader('content-type', 'application/json')
  res.end(JSON.stringify(payload))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch((error) => {
    console.error(error)
    process.exit(1)
  })
}
