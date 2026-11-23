import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { hashDirectory, hashFile } from '../artifact-hash.mjs'

function makeTmp() {
  return mkdtempSync(join(tmpdir(), 'artifact-hash-'))
}

test('文件哈希：流式读取，内容一致哈希稳定', async () => {
  const dir = makeTmp()
  try {
    const f = join(dir, 'a.zip')
    writeFileSync(f, 'hello world')
    const h1 = await hashFile(f)
    const h2 = await hashFile(f)
    assert.equal(h1, h2)
    assert.match(h1, /^[0-9a-f]{64}$/)
    // 与已知 sha256 对照（hello world）
    assert.equal(h1, 'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('文件哈希：内容变化则哈希变化', async () => {
  const dir = makeTmp()
  try {
    const f = join(dir, 'a.zip')
    writeFileSync(f, 'v1')
    const before = await hashFile(f)
    writeFileSync(f, 'v2')
    const after = await hashFile(f)
    assert.notEqual(before, after)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('目录哈希：相同结构内容哈希稳定', async () => {
  const dir = makeTmp()
  try {
    const app = join(dir, 'Test.app')
    mkdirSync(join(app, 'Contents', 'MacOS'), { recursive: true })
    writeFileSync(join(app, 'Contents', 'Info.plist'), 'plist-v1')
    writeFileSync(join(app, 'Contents', 'MacOS', 'bin'), 'binary')
    const h1 = await hashDirectory(app)
    const h2 = await hashDirectory(app)
    assert.equal(h1, h2)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('目录哈希：文件内容变化则哈希变化（旧实现在这里失效）', async () => {
  const dir = makeTmp()
  try {
    const app = join(dir, 'Test.app')
    mkdirSync(join(app, 'Contents'), { recursive: true })
    writeFileSync(join(app, 'Contents', 'Info.plist'), 'v1')
    const before = await hashDirectory(app)
    writeFileSync(join(app, 'Contents', 'Info.plist'), 'v2')
    const after = await hashDirectory(app)
    assert.notEqual(before, after)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('目录哈希：增删文件则哈希变化', async () => {
  const dir = makeTmp()
  try {
    const app = join(dir, 'Test.app')
    mkdirSync(app, { recursive: true })
    writeFileSync(join(app, 'a'), '1')
    const before = await hashDirectory(app)
    writeFileSync(join(app, 'b'), '2')
    const afterAdd = await hashDirectory(app)
    assert.notEqual(before, afterAdd)
    rmSync(join(app, 'a'))
    const afterRemove = await hashDirectory(app)
    assert.notEqual(afterAdd, afterRemove)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('目录哈希：与「路径字符串哈希」不同（证明不是退化实现）', async () => {
  const dir = makeTmp()
  try {
    const app = join(dir, 'Test.app')
    mkdirSync(app, { recursive: true })
    writeFileSync(join(app, 'f'), 'x'.repeat(100))
    const real = await hashDirectory(app)
    const pathOnly = (await import('node:crypto')).createHash('sha256').update('Test.app').digest('hex')
    assert.notEqual(real, pathOnly)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
