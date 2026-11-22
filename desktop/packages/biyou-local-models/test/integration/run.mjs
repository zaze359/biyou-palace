#!/usr/bin/env node
/**
 * 集成测试：假运行时 + 假 llama-server，覆盖
 * 安装目录发现 → 模型导入 → 启动 → 加载 → 对话/工具调用 → 空闲卸载 → 崩溃重启 → 退出清理。
 *
 *   node test/integration/run.mjs
 */
import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createModelPaths, writeJsonAtomic } from '../../lib/runtime/state.js'
import { Downloader } from '../../lib/runtime/downloader.js'
import { ModelManager } from '../../lib/runtime/models.js'
import { RuntimeManager } from '../../lib/runtime/runtime.js'
import { Supervisor, findFreePort } from '../../lib/runtime/supervisor.js'
import { createLocalAdapter } from '../../lib/runtime/provider.js'

const fixture = pathToFileURL(join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', 'fake-llama-server.mjs')).href
const tests = []
const test = (name, fn) => tests.push({ name, fn })

function ggufBuffer() {
  const parts = []
  const u32 = (value) => { const buffer = Buffer.alloc(4); buffer.writeUInt32LE(value); parts.push(buffer) }
  const u64 = (value) => { const buffer = Buffer.alloc(8); buffer.writeBigUInt64LE(BigInt(value)); parts.push(buffer) }
  const str = (value) => { const buffer = Buffer.from(value); u64(buffer.length); parts.push(buffer) }
  const kvString = (key, value) => { str(key); u32(8); str(value) }
  const kvU32 = (key, value) => { str(key); u32(4); u32(value) }
  u32(0x46554747)
  u32(3)
  u64(0)
  u64(3)
  kvString('general.architecture', 'qwen3')
  kvU32('qwen3.context_length', 4096)
  kvU32('general.file_type', 15)
  return Buffer.concat(parts)
}

async function createRuntime(paths, root) {
  const runtimeDir = join(paths.runtimes, 'fake-metal')
  await mkdir(runtimeDir, { recursive: true })
  const launcher = join(runtimeDir, 'llama-server')
  await writeFile(launcher, `#!/bin/sh\nexec "${process.execPath}" "${decodeURIComponent(new URL(fixture).pathname)}" "$@"\n`)
  await chmod(launcher, 0o755)
  const record = {
    schema_version: 1,
    engine: 'llama.cpp',
    tag: 'fake',
    backend: 'metal',
    platform: process.platform,
    arch: process.arch,
    binary: 'llama-server',
    source: 'managed',
    installedAt: new Date().toISOString(),
  }
  await writeJsonAtomic(join(runtimeDir, 'runtime.json'), record)
  await writeJsonAtomic(paths.activeRuntime, { ...record, activatedAt: new Date().toISOString() })
  return record
}

test('本地模型完整链路（导入 → 启动 → 对话/工具 → 空闲卸载 → 崩溃重启 → 停止）', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'biyou-local-integration-'))
  const paths = createModelPaths(join(dir, 'local-models'))
  const downloader = new Downloader({ paths })
  await downloader.init()
  const source = join(dir, 'demo.gguf')
  await writeFile(source, ggufBuffer())
  const models = new ModelManager({ paths, downloader })
  const entry = await models.importPath(source)
  await models.writePreset()

  const runtimeRecord = await createRuntime(paths, dir)
  const runtime = new RuntimeManager({ paths, downloader })
  assert.equal((await runtime.resolve()).kind, 'managed')
  assert.equal((await runtime.resolve()).tag, runtimeRecord.tag)

  const port = await findFreePort(19434)
  const supervisor = new Supervisor({
    paths,
    runtime,
    models,
    config: { port, idleUnloadMs: 300, idleCheckMs: 100 },
  })

  try {
    await supervisor.init()
    await supervisor.start()
    assert.ok(['idle', 'ready'].includes(supervisor.state), `启动后状态：${supervisor.state}`)

    await supervisor.ensureModel(entry.id, AbortSignal.timeout(30_000))
    assert.equal(supervisor.state, 'ready')
    assert.equal((await supervisor.status()).loaded_model, entry.id)

    const adapter = createLocalAdapter({ models, supervisor })
    const listed = await adapter.listModels('biyou-local')
    assert.equal(listed.length, 1)
    assert.equal(listed[0].id, entry.id)

    const textChunks = []
    for await (const chunk of adapter.stream({
      provider: 'biyou-local',
      model: entry.id,
      messages: [{ role: 'user', content: [{ type: 'text', text: '你好' }] }],
    })) textChunks.push(chunk)
    const text = textChunks.filter((chunk) => chunk.type === 'text-delta').map((chunk) => chunk.text).join('')
    assert.equal(text, '你好')
    assert.equal(textChunks.at(-1).type, 'finish')
    assert.equal(textChunks.at(-1).reason.kind, 'stop')
    assert.ok(textChunks.some((chunk) => chunk.type === 'usage'))

    const toolChunks = []
    for await (const chunk of adapter.stream({
      provider: 'biyou-local',
      model: entry.id,
      messages: [{ role: 'user', content: [{ type: 'text', text: '查一下北京天气' }] }],
      tools: [{ name: 'get_weather', description: '天气', parameters: { type: 'object', properties: { city: { type: 'string' } } } }],
    })) toolChunks.push(chunk)
    const call = toolChunks.find((chunk) => chunk.type === 'block-end' && chunk.block.type === 'tool-call')
    assert.ok(call, '应产出 tool-call block')
    assert.equal(call.block.name, 'get_weather')
    assert.deepEqual(JSON.parse(call.block.arguments), { city: '北京' })
    assert.equal(toolChunks.at(-1).reason.kind, 'tool-calls')

    // 空闲卸载：router 还在，模型被卸载。
    await new Promise((resolve) => setTimeout(resolve, 900))
    assert.equal((await supervisor.status()).loaded_model, null)
    assert.equal((await supervisor.status()).state, 'idle')

    // 下一条消息自动重载。
    await supervisor.ensureModel(entry.id, AbortSignal.timeout(30_000))
    assert.equal(supervisor.state, 'ready')

    // 崩溃后按退避重启。
    const oldPid = (await supervisor.status()).pid
    process.kill(oldPid, 'SIGKILL')
    await new Promise((resolve) => setTimeout(resolve, 2200))
    const afterCrash = await supervisor.status()
    assert.notEqual(afterCrash.state, 'error', `崩溃后不应停在 error：${afterCrash.error}`)
    assert.ok(afterCrash.pid, '应已重启')
    assert.notEqual(afterCrash.pid, oldPid)
    await supervisor.ensureModel(entry.id, AbortSignal.timeout(30_000))
    assert.equal(supervisor.state, 'ready')

    const runningPid = (await supervisor.status()).pid
    await supervisor.stop()
    assert.equal((await supervisor.status()).state, 'stopped')
    await new Promise((resolve) => setTimeout(resolve, 200))
    assert.throws(() => process.kill(runningPid, 0), '退出后不应残留 llama-server')
  } finally {
    await supervisor.stop().catch(() => {})
    downloader.dispose()
  }
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

console.log(`\n${failed === 0 ? '✓' : '✗'} 集成测试：${tests.length - failed}/${tests.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
