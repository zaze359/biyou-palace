import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseSha256, verifySha256 } from '../shasums.mjs'

const H1 = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const SAMPLE = `${H1}  node-v24.22.3-darwin-x64.tar.gz\n${H2}  node-v24.22.3-linux-x64.tar.gz\n`

test('parseSha256：按文件名取期望哈希', () => {
  assert.equal(parseSha256(SAMPLE, 'node-v24.22.3-darwin-x64.tar.gz'), H1)
  assert.equal(parseSha256(SAMPLE, 'node-v24.22.3-linux-x64.tar.gz'), H2)
})

test('parseSha256：带路径前缀的清单也能按 basename 匹配', () => {
  const withPath = `${H1}  dist/v24.22.3/node-v24.22.3-darwin-x64.tar.gz\n`
  assert.equal(parseSha256(withPath, 'node-v24.22.3-darwin-x64.tar.gz'), H1)
})

test('parseSha256：找不到返回 null', () => {
  assert.equal(parseSha256(SAMPLE, 'missing.tar.gz'), null)
})

test('parseSha256：容忍 CRLF 与大小写 hex', () => {
  const crlf = `ABCDEF0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789  node-v1.tar.gz\r\n`
  assert.equal(
    parseSha256(crlf, 'node-v1.tar.gz'),
    'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
  )
})

test('verifySha256：一致时通过', () => {
  assert.deepEqual(verifySha256(SAMPLE, 'node-v24.22.3-darwin-x64.tar.gz', H1), {
    ok: true,
    expected: H1,
  })
})

test('verifySha256：实际哈希大小写不敏感', () => {
  assert.equal(verifySha256(SAMPLE, 'node-v24.22.3-darwin-x64.tar.gz', H1.toUpperCase()).ok, true)
})

test('verifySha256：不一致时抛错并含期望/实际', () => {
  assert.throws(
    () => verifySha256(SAMPLE, 'node-v24.22.3-darwin-x64.tar.gz', H2),
    /校验和不一致[\s\S]*期望[\s\S]*实际/,
  )
})

test('verifySha256：清单缺条目时抛错', () => {
  assert.throws(
    () => verifySha256(SAMPLE, 'missing.tar.gz', H1),
    /找不到.*校验和/,
  )
})
