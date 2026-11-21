/**
 * 把仓库里的 dsh profile 模板同步到 userData 下的 DSH_HOME。
 *
 * 为什么同步而不是直接在仓库里跑：需求要求配置/会话/任务/产物都不落在仓库目录。
 * 所以仓库里的 profile 只是「模板 + 依赖」，运行时副本在
 *   <userData>/dsh/profiles/biyou
 * 其中 node_modules 用符号链接指回模板目录，避免每次启动复制上百 MB。
 */
import { existsSync, lstatSync, mkdirSync, readFileSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { profileSourceDir } from '../paths.js'

/** profile 里由本仓库管理的文件；node_modules 单独链接 */
const PROFILE_FILES = ['package.json', 'cordis.yml', 'cordis.patch.yml', 'pnpm-workspace.yaml']

function writeIfChanged(file, content) {
  if (existsSync(file) && readFileSync(file, 'utf8') === content) return false
  mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, content)
  return true
}

function ensureNodeModulesLink(profileDir, sourceModules) {
  const linkPath = join(profileDir, 'node_modules')
  if (existsSync(sourceModules) === false) {
    throw new Error(
      `dsh 依赖缺失：${sourceModules}。先在 desktop/runtime/home/profiles/biyou 执行 \`npm install\`（或 npm run dsh:install）。`,
    )
  }

  if (existsSync(linkPath) || isBrokenLink(linkPath)) {
    const stat = lstatSync(linkPath)
    if (stat.isSymbolicLink()) {
      if (readlinkSync(linkPath) === sourceModules) return false
      rmSync(linkPath, { force: true })
    } else {
      // 有人在这个目录里真装了依赖，尊重现状，不再动它
      return false
    }
  }

  symlinkSync(sourceModules, linkPath, process.platform === 'win32' ? 'junction' : 'dir')
  return true
}

function isBrokenLink(path) {
  try {
    return lstatSync(path).isSymbolicLink()
  } catch {
    return false
  }
}

/**
 * @param {{ userDataDir: string, profile: string, runtimeRoot?: string }} options
 * @returns {{ dshHome: string, profileDir: string, sourceDir: string, dshBin: string, changed: boolean, dshVersion: string }}
 */
export function provisionProfile({ userDataDir, profile, runtimeRoot }) {
  const sourceDir = runtimeRoot
    ? join(runtimeRoot, 'home', 'profiles', profile)
    : profileSourceDir(profile)
  const profileDir = join(userDataDir, 'dsh', 'profiles', profile)

  if (!existsSync(join(sourceDir, 'package.json'))) {
    throw new Error(`找不到 dsh profile 模板：${sourceDir}`)
  }

  mkdirSync(profileDir, { recursive: true })
  let changed = false
  for (const name of PROFILE_FILES) {
    const sourceFile = join(sourceDir, name)
    if (!existsSync(sourceFile)) continue
    changed = writeIfChanged(join(profileDir, name), readFileSync(sourceFile, 'utf8')) || changed
  }
  changed = ensureNodeModulesLink(profileDir, join(sourceDir, 'node_modules')) || changed

  const dshBin = join(profileDir, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
  if (!existsSync(dshBin)) {
    throw new Error(`dsh 入口不存在：${dshBin}。检查 desktop/runtime/home/profiles/biyou 的依赖是否装好。`)
  }

  const manifest = JSON.parse(readFileSync(join(profileDir, 'node_modules', '@deepseek-ai', 'dsh', 'package.json'), 'utf8'))

  return { dshHome: join(userDataDir, 'dsh'), profileDir, sourceDir, dshBin, changed, dshVersion: manifest.version }
}
