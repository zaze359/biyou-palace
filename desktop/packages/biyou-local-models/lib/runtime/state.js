/**
 * 本地模型插件的可写目录。
 *
 * 所有数据落在 dsh 用户数据目录（`$DSH_HOME/local-models`），不写仓库目录：
 *
 *   local-models/
 *   ├── runtimes/<tag>-<backend>/       受管 llama.cpp 运行时
 *   ├── models/<owner>/<repo>/...       GGUF 与 mmap 需要的附属文件
 *   ├── cache/partials/<job>/           未完成下载
 *   ├── cache/extract-<id>/                解压中的临时目录
 *   ├── state/                          active-runtime / registry / downloads / server
 *   └── logs/llama-server.log
 */
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'

/** 解析插件数据根；`BIYOU_LOCAL_MODELS_DIR` 只用于测试与排障。 */
export function resolveModelsRoot(env = process.env) {
  if (env.BIYOU_LOCAL_MODELS_DIR) return resolve(env.BIYOU_LOCAL_MODELS_DIR)
  const home = env.DSH_HOME?.trim() ? env.DSH_HOME : join(homedir(), '.dsh')
  return resolve(home, 'local-models')
}

/** 由数据根派生全部固定路径。 */
export function createModelPaths(root = resolveModelsRoot()) {
  const state = join(root, 'state')
  return {
    root,
    runtimes: join(root, 'runtimes'),
    models: join(root, 'models'),
    cache: join(root, 'cache'),
    partials: join(root, 'cache', 'partials'),
    extraction: join(root, 'cache', 'extract'),
    logs: join(root, 'logs'),
    state,
    activeRuntime: join(state, 'active-runtime.json'),
    registry: join(state, 'registry.json'),
    downloads: join(state, 'downloads.json'),
    server: join(state, 'server.json'),
    apiKey: join(state, '.api_key'),
    serverLog: join(root, 'logs', 'llama-server.log'),
    modelsPreset: join(state, 'models.ini'),
  }
}

/** 读取 JSON；文件缺失或损坏都返回 fallback，不把坏状态带上启动路径。 */
export async function readJson(file, fallback = null) {
  try {
    return JSON.parse(await readFile(file, 'utf8'))
  } catch {
    return fallback
  }
}

/** 读取文本；文件缺失返回 fallback。 */
export async function readText(file, fallback = null) {
  try {
    return await readFile(file, 'utf8')
  } catch {
    return fallback
  }
}

/** 临时文件 + rename 的原子写；写入期间崩溃不会留下半个 JSON。 */
export async function writeJsonAtomic(file, value, mode = 0o600) {
  await mkdir(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`
  await writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { mode })
  await rename(tmp, file)
}

/** 临时文件 + rename 的原子写文本。 */
export async function writeTextAtomic(file, text, mode = 0o600) {
  await mkdir(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`
  await writeFile(tmp, text, { mode })
  await rename(tmp, file)
}

/** 确保一组目录存在。 */
export async function ensureDirs(...dirs) {
  await Promise.all(dirs.map((dir) => mkdir(dir, { recursive: true })))
}

/** 删除文件/目录，忽略不存在。 */
export async function removeQuietly(path) {
  await rm(path, { recursive: true, force: true })
}
