/**
 * 产物 SHA-256 计算（给 build.mjs 与 CI 校验用）。
 *
 * 文件产物：整文件流式哈希，不一次性读进内存。
 * 目录产物（.app）：按相对路径排序，逐文件把「相对路径 + 大小 + 内容」喂进同一个
 * hash。这样内容或文件结构任一变化，哈希都会变——满足「同 commit 两次构建
 * 校验和一致」的验收，也能发现产物被篡改。
 *
 * 注意：不能用「目录路径字符串」当哈希（旧实现），那会让任何内容变化的哈希都不变。
 */
import { createHash } from 'node:crypto'
import { createReadStream, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

/** 收集目录下所有文件的相对路径（用 / 分隔，排序保证跨平台稳定）。 */
function listFiles(dir, root = dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) listFiles(full, root, out)
    else if (entry.isFile()) out.push(relative(root, full).split(sep).join('/'))
  }
  return out
}

/** 把单个文件的内容流式喂进 hash（避免大文件整读进内存）。 */
function feedFile(hash, file) {
  return new Promise((resolve, reject) => {
    const stream = createReadStream(file)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('end', resolve)
    stream.on('error', reject)
  })
}

/** 目录内容哈希：相对路径 + 字节数 + 内容，顺序固定。 */
export async function hashDirectory(dir) {
  const hash = createHash('sha256')
  const files = listFiles(dir).sort()
  for (const rel of files) {
    const full = join(dir, rel)
    hash.update(rel)
    hash.update('\0')
    hash.update(String(statSync(full).size))
    hash.update('\0')
    await feedFile(hash, full)
    hash.update('\0')
  }
  return hash.digest('hex')
}

/** 文件哈希：流式读取。 */
export async function hashFile(file) {
  const hash = createHash('sha256')
  await feedFile(hash, file)
  return hash.digest('hex')
}
