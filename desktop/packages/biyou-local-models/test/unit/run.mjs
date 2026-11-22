#!/usr/bin/env node
/**
 * 本地模型插件单元测试：不联网、不启动 llama-server。
 *
 *   node test/unit/run.mjs
 */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { chmod, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createModelPaths, readJson, resolveModelsRoot, writeJsonAtomic } from '../../lib/runtime/state.js'
import { normalizeManifest, selectTarget, estimateMemory, classifyFit } from '../../lib/runtime/platform.js'
import { shardComplete, ModelManager, fileUrl } from '../../lib/runtime/models.js'
import { readGgufHeader } from '../../lib/runtime/gguf.js'
import { Downloader } from '../../lib/runtime/downloader.js'
import { RuntimeManager } from '../../lib/runtime/runtime.js'
import { buildRequestBody, createLocalAdapter, streamChunks, toOpenAiMessages } from '../../lib/runtime/provider.js'
import { sanitizeStatus } from '../../lib/index.js'

const tests = []
const test = (name, fn) => tests.push({ name, fn })

function ggufBuffer() {
  const parts = []
  const u32 = (value) => {
    const buffer = Buffer.alloc(4)
    buffer.writeUInt32LE(value)
    parts.push(buffer)
  }
  const u64 = (value) => {
    const buffer = Buffer.alloc(8)
    buffer.writeBigUInt64LE(BigInt(value))
    parts.push(buffer)
  }
  const str = (value) => {
    const buffer = Buffer.from(value, 'utf8')
    u64(buffer.length)
    parts.push(buffer)
  }
  const kvString = (key, value) => {
    str(key)
    u32(8)
    str(value)
  }
  const kvU32 = (key, value) => {
    str(key)
    u32(4)
    u32(value)
  }

  u32(0x46554747)
  u32(3)
  u64(0) // tensor count
  u64(4) // metadata count
  kvString('general.architecture', 'qwen3')
  kvU32('qwen3.context_length', 40960)
  kvU32('qwen3.block_count', 36)
  kvU32('general.file_type', 15)
  return Buffer.concat(parts)
}

function makeCatalog(model) {
  return { schema_version: 1, provider: 'biyou-local', models: [model] }
}

function hash(buffer) {
  return createHash('sha256').update(buffer).digest('hex')
}

async function tempDir(prefix = 'biyou-local-unit-') {
  return mkdtemp(join(tmpdir(), prefix))
}

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)))
}

/** 慢速/普通 Range 文件服务器；测试下载器的续传与取消。 */
async function serveBuffer(buffer, { delayMs = 0 } = {}) {
  let rangeRequests = 0
  const server = createServer((req, res) => {
    let start = 0
    const match = /bytes=(\d+)-/.exec(String(req.headers.range ?? ''))
    if (match) {
      rangeRequests += 1
      start = Number(match[1])
    }
    const body = buffer.subarray(start)
    res.writeHead(start > 0 ? 206 : 200, {
      'content-type': 'application/octet-stream',
      'content-length': String(body.length),
      'accept-ranges': 'bytes',
    })
    if (delayMs === 0) {
      res.end(body)
      return
    }
    let offset = 0
    const chunkSize = 64 * 1024
    let timer = null
    const finish = () => {
      if (timer) clearTimeout(timer)
      timer = null
      res.end()
    }
    const pump = () => {
      if (offset >= body.length) return finish()
      res.write(body.subarray(offset, offset + chunkSize))
      offset += chunkSize
      timer = setTimeout(pump, delayMs)
    }
    res.on('close', () => {
      if (timer) clearTimeout(timer)
      timer = null
    })
    pump()
  })
  const port = await listen(server)
  return {
    url: `http://127.0.0.1:${port}`,
    get rangeRequests() {
      return rangeRequests
    },
    close: () => new Promise((resolve) => server.close(() => resolve())),
  }
}

test('list/manifest 校验与平台选择', async () => {
  const manifest = normalizeManifest(JSON.parse(await readFile(new URL('../../lib/runtime/manifest.json', import.meta.url), 'utf8')))
  assert.equal(manifest.tag, 'b12620')
  assert.equal(manifest.targets.length, 5)
  assert.equal(selectTarget({ manifest, platform: 'darwin', arch: 'x64' }).target.backend, 'metal')
  assert.equal(selectTarget({ manifest, platform: 'darwin', arch: 'arm64' }).target.backend, 'metal')
  assert.equal(selectTarget({ manifest, platform: 'win32', arch: 'x64', gpu: 'nvidia' }).target.backend, 'cuda')
  assert.equal(selectTarget({ manifest, platform: 'win32', arch: 'x64', gpu: 'amd' }).target.backend, 'vulkan')
  assert.equal(selectTarget({ manifest, platform: 'win32', arch: 'x64', gpu: 'unknown' }).target.backend, 'cpu')
  const unsupported = selectTarget({ manifest, platform: 'linux', arch: 'x64' })
  assert.equal(unsupported.supported, false)
  assert.match(unsupported.reason, /暂不支持/)
  assert.throws(() => normalizeManifest({ schema_version: 1, engine: 'x', tag: 'y', targets: {} }), /targets 为空|缺少/)
})

test('内存估算给出保守可运行性', () => {
  const estimate = estimateMemory({ memory: { weights_bytes: 5_000_000_000, kv_bytes_per_1k_ctx: 150_000_000, compute_bytes: 500_000_000, overhead_bytes: 400_000_000 } }, 65536)
  assert.equal(estimate.totalBytes, 5_000_000_000 + 9_830_400_000 + 900_000_000)
  assert.equal(classifyFit(estimate, 32 * 1024 ** 3).fit, 'ram')
  assert.equal(classifyFit(estimate, 8 * 1024 ** 3).fit, 'no')
})

test('分片完整性：缺一片不可服务', () => {
  assert.equal(shardComplete([{ path: 'a-00001-of-00002.gguf' }, { path: 'a-00002-of-00002.gguf' }]), true)
  assert.equal(shardComplete([{ path: 'a-00001-of-00002.gguf' }]), false)
  assert.equal(shardComplete([{ path: 'a-00001-of-00003.gguf' }, { path: 'a-00002-of-00002.gguf' }]), false)
  assert.equal(shardComplete([{ path: 'plain.gguf' }]), true)
})

test('GGUF 头部：架构、量化、上下文', async () => {
  const dir = await tempDir()
  const file = join(dir, 'demo.gguf')
  await writeFile(file, ggufBuffer())
  const header = await readGgufHeader(file)
  assert.equal(header.architecture, 'qwen3')
  assert.equal(header.contextLength, 40960)
  assert.equal(header.blockCount, 36)
  assert.equal(header.quantization, 'Q4_K_M')
  assert.equal(header.version, 3)

  const bad = join(dir, 'bad.gguf')
  await writeFile(bad, Buffer.from('NOPE'))
  await assert.rejects(() => readGgufHeader(bad), (error) => error.code === 'INVALID_MAGIC')
})

test('状态原子写与 JSON 容错', async () => {
  const dir = await tempDir()
  const file = join(dir, 'state.json')
  await writeJsonAtomic(file, { a: 1 })
  assert.deepEqual(await readJson(file), { a: 1 })
  await writeFile(file, '{broken')
  assert.deepEqual(await readJson(file, { fallback: true }), { fallback: true })
  assert.equal(resolveModelsRoot({ BIYOU_LOCAL_MODELS_DIR: dir }), dir)
  const paths = createModelPaths(dir)
  assert.equal(paths.registry, join(dir, 'state', 'registry.json'))
})

test('下载器：完整下载、Range 续传、校验失败与取消', async () => {
  const buffer = Buffer.alloc(1024 * 1024, 7)
  const sha = hash(buffer)
  const server = await serveBuffer(buffer)
  const dir = await tempDir()
  const paths = createModelPaths(dir)
  const downloader = new Downloader({ paths })
  await downloader.init()

  const dest = join(dir, 'models', 'full.bin')
  downloader.start({ id: 'full', kind: 'model', files: [{ url: `${server.url}/full.bin`, dest, size_bytes: buffer.length, sha256: sha }] })
  let record = await downloader.wait('full')
  assert.equal(record.status, 'completed')
  assert.equal(hash(await readFile(dest)), sha)
  assert.equal(JSON.parse(await readFile(`${dest}.sha256`, 'utf8')).size, buffer.length)

  const slow = await serveBuffer(buffer, { delayMs: 10 })
  const slowDest = join(dir, 'models', 'slow.bin')
  downloader.start({ id: 'slow', kind: 'model', files: [{ url: `${slow.url}/slow.bin`, dest: slowDest, size_bytes: buffer.length, sha256: sha }] })
  await new Promise((resolve) => setTimeout(resolve, 80))
  assert.equal(downloader.pause('slow'), true)
  record = await downloader.wait('slow')
  assert.equal(record.status, 'paused')
  downloader.resume('slow')
  record = await downloader.wait('slow')
  assert.equal(record.status, 'completed')
  assert.ok(slow.rangeRequests > 0, '续传应发送 Range')
  assert.equal(hash(await readFile(slowDest)), sha)
  await slow.close()

  const badDest = join(dir, 'models', 'bad.bin')
  downloader.start({ id: 'bad', kind: 'model', files: [{ url: `${server.url}/bad.bin`, dest: badDest, size_bytes: buffer.length, sha256: '0'.repeat(64) }] })
  record = await downloader.wait('bad')
  assert.equal(record.status, 'failed')
  assert.equal(record.error.code, 'CHECKSUM_MISMATCH')
  await assert.rejects(() => readFile(badDest))

  const cancelDest = join(dir, 'models', 'cancel.bin')
  const cancelServer = await serveBuffer(buffer, { delayMs: 10 })
  downloader.start({ id: 'cancel', kind: 'model', files: [{ url: `${cancelServer.url}/cancel.bin`, dest: cancelDest, size_bytes: buffer.length, sha256: sha }] })
  await new Promise((resolve) => setTimeout(resolve, 60))
  await downloader.cancel('cancel')
  record = await downloader.wait('cancel')
  assert.equal(record.status, 'cancelled')
  await assert.rejects(() => readFile(cancelDest))
  await cancelServer.close()
  await server.close()
})

test('模型导入：链接不复制、读取头部、可移除', async () => {
  const dir = await tempDir()
  const root = join(dir, 'local-models')
  const paths = createModelPaths(root)
  const source = join(dir, 'my-model.gguf')
  await writeFile(source, ggufBuffer())
  const catalogFile = join(dir, 'catalog.json')
  await writeFile(catalogFile, JSON.stringify(makeCatalog({
    id: 'demo',
    display_name: 'Demo',
    repo: 'acme/demo',
    revision: 'main',
    license: 'mit',
    tool_calling: true,
    context: { train: 40960, default: 40960, max: 40960, yarn: false },
    memory: { weights_bytes: 100, kv_bytes_per_1k_ctx: 100, compute_bytes: 10, overhead_bytes: 10 },
    files: [{ path: 'demo.gguf', size_bytes: 100, sha256: '0'.repeat(64) }],
  })))
  const downloader = new Downloader({ paths })
  await downloader.init()
  const models = new ModelManager({ paths, downloader, catalogUrl: pathToFileURL(catalogFile) })
  const entry = await models.importPath(source)
  assert.equal(entry.source, 'imported')
  assert.match(entry.id, /^imported-/)
  const status = await models.status()
  const imported = status.models.find((model) => model.id === entry.id)
  assert.equal(imported.servable, true)
  assert.equal(imported.gguf.context_length, 40960)
  assert.equal(imported.gguf.quantization, 'Q4_K_M')
  await models.remove(entry.id)
  assert.equal((await models.status()).models.some((model) => model.id === entry.id), false)
  assert.equal((await readFile(source)).length, ggufBuffer().length)
})

test('模型下载：镜像 URL、完成后 registry 可服务', async () => {
  const buffer = Buffer.alloc(256 * 1024, 3)
  const server = await serveBuffer(buffer)
  const dir = await tempDir()
  const paths = createModelPaths(join(dir, 'local-models'))
  const catalogFile = join(dir, 'catalog.json')
  await writeFile(catalogFile, JSON.stringify(makeCatalog({
    id: 'demo',
    display_name: 'Demo',
    repo: 'acme/demo',
    revision: 'rev1',
    license: 'mit',
    tool_calling: true,
    context: { train: 4096, default: 4096, max: 4096, yarn: false },
    memory: { weights_bytes: buffer.length, kv_bytes_per_1k_ctx: 100, compute_bytes: 10, overhead_bytes: 10 },
    files: [{ path: 'demo.gguf', size_bytes: buffer.length, sha256: hash(buffer) }],
  })))
  const downloader = new Downloader({ paths })
  await downloader.init()
  const models = new ModelManager({ paths, downloader, catalogUrl: pathToFileURL(catalogFile), mirror: server.url })
  const record = await models.download('demo')
  assert.match(record.id, /^model-/)
  const done = await downloader.wait(record.id)
  assert.equal(done.status, 'completed')
  const status = await models.status()
  assert.equal(status.models[0].servable, true)
  assert.equal(status.models[0].state, 'ready')
  assert.equal(fileUrl({ repo: 'acme/demo', revision: 'rev1' }, { path: 'demo.gguf' }, server.url), `${server.url}/acme/demo/resolve/rev1/demo.gguf`)
  await server.close()
})

test('运行时安装：下载 → 校验 → 解压 → 激活', async () => {
  const execFileAsync = promisify(execFile)
  const dir = await tempDir()
  const payload = join(dir, 'payload')
  await mkdir(join(payload, 'llama-fake'), { recursive: true })
  const binary = join(payload, 'llama-fake', 'llama-server')
  await writeFile(binary, '#!/bin/sh\nexit 0\n')
  await chmod(binary, 0o755)
  const archive = join(dir, 'runtime.tar.gz')
  await execFileAsync('tar', ['-czf', archive, '-C', payload, 'llama-fake'])
  const buffer = await readFile(archive)
  const server = await serveBuffer(buffer)
  const manifest = join(dir, 'manifest.json')
  await writeFile(manifest, JSON.stringify({
    schema_version: 1,
    engine: 'llama.cpp',
    tag: 'testbuild',
    targets: {
      'testos-x64': {
        platform: 'testos',
        arch: 'x64',
        backend: 'metal',
        url: `${server.url}/runtime.tar.gz`,
        sha256: hash(buffer),
        size_bytes: buffer.length,
        archive: 'tar.gz',
        binary: 'llama-server',
      },
    },
  }))
  const paths = createModelPaths(join(dir, 'local-models'))
  const downloader = new Downloader({ paths })
  await downloader.init()
  const runtime = new RuntimeManager({ paths, downloader, manifestUrl: pathToFileURL(manifest), platform: 'testos', arch: 'x64', gpu: 'unknown' })
  const installed = await runtime.install()
  assert.equal(installed.tag, 'testbuild')
  const resolved = await runtime.resolve()
  assert.equal(resolved.kind, 'managed')
  assert.equal((await runtime.status()).active.backend, 'metal')
  await assert.rejects(() => readFile(join(paths.runtimes, 'missing')), /ENOENT/)
  await server.close()
})

test('provider adapter 具备 dsh 注册所需的完整方法', () => {
  const adapter = createLocalAdapter({
    models: { status: async () => ({ models: [] }), catalogModel: async () => null, registryEntry: async () => null },
    supervisor: {},
  })
  assert.deepEqual(adapter.providerInfo('biyou-local'), { id: 'biyou-local', name: '碧游宫 · 本地模型' })
  assert.equal(adapter.providerRetryPolicy(), undefined)
  assert.equal(adapter.imageRequestPricing(), undefined)
  assert.equal(typeof adapter.listModels, 'function')
  assert.equal(typeof adapter.resolveModel, 'function')
  assert.equal(typeof adapter.prepareCall, 'function')
  assert.equal(typeof adapter.stream, 'function')
  assert.equal(typeof adapter.imageRequestTarget, 'function')
})

test('provider 消息转换与 SSE 解析', async () => {
  const dshMessages = [
    { role: 'system', content: [{ type: 'text', text: 'sys' }] },
    { role: 'user', content: [{ type: 'text', text: 'hi' }] },
    {
      role: 'assistant',
      content: [
        { type: 'reasoning', text: 'think' },
        { type: 'text', text: 'call' },
        { type: 'tool-call', id: 'call_1', name: 'weather', arguments: '{"city":"北京"}' },
      ],
    },
    { role: 'tool', toolCallId: 'call_1', content: [{ type: 'text', text: '晴' }] },
  ]
  const messages = toOpenAiMessages(dshMessages)
  assert.deepEqual(messages[0], { role: 'system', content: 'sys' })
  assert.equal(messages[2].tool_calls[0].function.name, 'weather')
  assert.equal(messages[3].tool_call_id, 'call_1')
  const body = buildRequestBody(
    { model: 'demo', messages: dshMessages, tools: [{ name: 'weather', description: 'd', parameters: { type: 'object' } }], maxTokens: 99 },
    { defaultMaxTokens: 100, inputModalities: ['text'] },
  )
  assert.equal(body.max_tokens, 99)
  assert.equal(body.stream, true)
  assert.equal(body.tools[0].function.name, 'weather')

  const sse = [
    'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}',
    '',
    'data: {"choices":[{"delta":{"content":"你"}}]}',
    '',
    'data: {"choices":[{"delta":{"content":"好"}}]}',
    '',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"weather","arguments":"{\\"city\\""}}]}}]}',
    '',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"北京\\"}"}}]}}]}',
    '',
    'data: {"choices":[{"finish_reason":"tool_calls"}]}',
    '',
    'data: {"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14,"prompt_tokens_details":{"cached_tokens":2}}}',
    '',
    'data: [DONE]',
    '',
  ].join('\n')
  const chunks = []
  for await (const chunk of streamChunks(new Response(sse).body)) chunks.push(chunk)
  const types = chunks.map((chunk) => chunk.type)
  assert.ok(types.includes('reasoning-delta'))
  assert.ok(types.includes('text-delta'))
  assert.ok(types.includes('tool-call-delta'))
  const toolEnd = chunks.find((chunk) => chunk.type === 'block-end' && chunk.block.type === 'tool-call')
  assert.equal(toolEnd.block.name, 'weather')
  assert.equal(JSON.parse(toolEnd.block.arguments).city, '北京')
  const usage = chunks.find((chunk) => chunk.type === 'usage')
  assert.equal(usage.usage.inputTokens, 8)
  assert.equal(usage.usage.cacheReadTokens, 2)
  assert.equal(chunks.at(-1).type, 'finish')
  assert.equal(chunks.at(-1).reason.kind, 'tool-calls')
})

test('status 去敏：不返回 key、绝对二进制路径与下载 URL', async () => {
  const status = {
    runtime: {
      platform: 'darwin',
      arch: 'x64',
      active: { tag: 'b1', backend: 'metal', binary: '/Users/me/secret/llama-server', installedAt: 'now' },
      installed: [],
    },
    catalog: {
      provider: 'biyou-local',
      models: [{
        id: 'demo',
        download: { id: 'model-demo', files: [{ dest: '/Users/me/model.gguf', url: 'https://secret', size_bytes: 1 }] },
      }],
    },
    downloads: [{
      id: 'model-demo',
      kind: 'model',
      files: [{ dest: '/Users/me/model.gguf', url: 'https://secret', size_bytes: 1 }],
      error: { code: 'X', message: 'failed /Users/me/local-models/model.gguf' },
    }],
    server: { state: 'stopped', log_tail: ['/Users/me/local-models/llama-server.log'] },
    external: null,
  }
  const sanitized = sanitizeStatus(status, { root: '/Users/me/local-models' })
  assert.equal(sanitized.runtime.active.binary, undefined)
  assert.equal(sanitized.downloads[0].files[0].url, undefined)
  assert.equal(sanitized.downloads[0].files[0].dest, undefined)
  assert.equal(sanitized.downloads[0].error.message.includes('/Users/me'), false)
  assert.equal(sanitized.server.log_tail[0].includes('/Users/me/local-models'), false)
})

let failed = 0
for (const item of tests) {
  try {
    await item.fn()
    console.log(`✓ ${item.name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${item.name}`)
    console.error(error)
  }
}

console.log(`\n${failed === 0 ? '✓' : '✗'} 单元测试：${tests.length - failed}/${tests.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
