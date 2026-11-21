#!/usr/bin/env node
/**
 * 把 desktop/packages/biyou-plugin 链进 profile 的 node_modules。
 *
 * 为什么不写成 package.json 里的 `file:` 依赖再 npm install：
 *   - profile 的 node_modules 有 1.2 GB，install 一次要联网且很慢；
 *   - 这里只需要一个符号链接，且必须幂等、离线可跑。
 * 相对路径由本脚本计算，换机器/换目录都不用改。
 *
 * 用法：node scripts/link-plugin.mjs [--check]
 *   --check 只校验链接是否存在且指向正确目标，不修改文件系统。
 */
import { existsSync, lstatSync, mkdirSync, readlinkSync, rmSync, symlinkSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const DESKTOP_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const PLUGIN_DIR = join(DESKTOP_ROOT, 'packages', 'biyou-plugin')
const PROFILE_DIR = join(DESKTOP_ROOT, 'runtime', 'home', 'profiles', 'biyou')
const SCOPE_DIR = join(PROFILE_DIR, 'node_modules', '@biyou')
const LINK_PATH = join(SCOPE_DIR, 'dsh-plugin')

const checkOnly = process.argv.includes('--check')

function fail(message) {
  console.error(`✗ ${message}`)
  process.exit(1)
}

if (!existsSync(join(PLUGIN_DIR, 'package.json'))) fail(`插件包缺失：${PLUGIN_DIR}`)
if (!existsSync(join(PROFILE_DIR, 'node_modules'))) fail(`profile 依赖未安装：${PROFILE_DIR}/node_modules`)

const target = relative(SCOPE_DIR, PLUGIN_DIR)

if (existsSync(LINK_PATH) || isBrokenLink(LINK_PATH)) {
  const current = safeReadlink(LINK_PATH)
  if (current === target) {
    console.log(`✓ 插件已链接：@biyou/dsh-plugin -> ${target}`)
    process.exit(0)
  }
  if (checkOnly) fail(`链接指向不对：当前 ${current ?? '(不是符号链接)'}，期望 ${target}`)
  if (current === null) fail(`${LINK_PATH} 已存在且不是符号链接，请先手动处理`)
  rmSync(LINK_PATH)
}

if (checkOnly) fail(`插件未链接：缺少 ${LINK_PATH}`)

mkdirSync(SCOPE_DIR, { recursive: true })
symlinkSync(target, LINK_PATH, 'dir')
console.log(`✓ 已链接：@biyou/dsh-plugin -> ${target}`)

function safeReadlink(path) {
  try {
    return readlinkSync(path)
  } catch {
    return null
  }
}

function isBrokenLink(path) {
  try {
    lstatSync(path)
    return !existsSync(path)
  } catch {
    return false
  }
}
