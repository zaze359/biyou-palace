/**
 * 版本锁定：单一事实来源。
 *
 * 三处必须一致，`npm run check` 会校验：
 *   - 本文件 PINNED
 *   - package.json 的 engines.node / dsh.version / devDependencies.electron
 *   - .node-version
 *
 * dsh 自带 engines `>=24.9.0`；这里锁到具体的 24.22.3，避免不同机器
 * 因为 Node 小版本差异出现原生模块或 ESM 行为漂移。
 */
export const PINNED = Object.freeze({
  node: '24.22.3',
  electron: '44.7.0',
  dsh: '0.2.2-rc.5',
  /** dsh profile 名，对应 $DSH_HOME/profiles/<profile> */
  profile: 'biyou',
})

/** dsh 自己的最低 Node 要求，用于「非锁定版本」的兜底校验 */
export const DSH_MIN_NODE = '24.9.0'

/** 把 '24.22.3' / 'v24.22.3-rc.1' 解析成可比较的元组 */
export function parseVersion(version) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(version ?? '').trim())
  if (!match) return null
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

/** a >= b */
export function gte(a, b) {
  const left = parseVersion(a)
  const right = parseVersion(b)
  if (!left || !right) return false
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index]
  }
  return true
}
