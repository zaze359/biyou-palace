/**
 * nodejs.org SHASUMS256.txt 解析与校验（纯函数，便于单测）。
 *
 * 文件格式：每行 "<64位hex>  <文件名>"，文件名可能带路径前缀。
 * 只按 basename 匹配，兼容 SHASUMS256.txt（根目录）与 dist/<arch>/ 子目录清单。
 */
import { basename } from 'node:path'

/** 从 SHASUMS256.txt 文本里取指定文件的期望 sha256（小写 hex）。 */
export function parseSha256(text, filename) {
  const want = basename(filename)
  for (const line of text.split(/\r?\n/)) {
    const m = /^([0-9a-fA-F]{64})\s+(\S+)\s*$/.exec(line)
    if (m && basename(m[2]) === want) return m[1].toLowerCase()
  }
  return null
}

/**
 * 比对实际哈希与清单期望值。
 * @returns {{ ok: true, expected: string }} 或抛错（含文件名与两个哈希，便于排查）。
 */
export function verifySha256(text, filename, actual) {
  const expected = parseSha256(text, filename)
  if (!expected) throw new Error(`SHASUMS256.txt 中找不到 ${filename} 的校验和`)
  const got = String(actual).toLowerCase()
  if (got !== expected) {
    throw new Error(
      `${filename} 校验和不一致（下载被篡改或不完整）\n  期望 ${expected}\n  实际 ${got}`,
    )
  }
  return { ok: true, expected }
}
