/**
 * dsh 运行时生命周期：以子进程方式启动 dsh 的 web profile，解析它打印的
 * URL 行（`dsh web: http://127.0.0.1:<port>/?token=...`），探活后交给渲染层。
 *
 * 为什么用子进程而不是在主进程里 import dsh：
 *   - dsh 要求 Node >= 24.9，Electron 内置 Node 只有 24.21（且版本不受我们控制）；
 *   - dsh 会自己起 shell/工具子进程，独立进程组更便于「退出即清理」；
 *   - 崩了只影响 dsh，不会带走整个 Electron 主进程。
 *
 * 本文件不 import electron，便于用普通 Node 直接跑（scripts/smoke-dsh.mjs）。
 */
import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { appendFileSync, mkdirSync, statSync, renameSync } from 'node:fs'
import { dirname } from 'node:path'

const ANSI = /\u001b\[[0-9;]*m/g
const MAX_LOG_BYTES = 4 * 1024 * 1024
const TAIL_LINES = 60

export const DSH_STATE = Object.freeze({
  STOPPED: 'stopped',
  STARTING: 'starting',
  READY: 'ready',
  FAILED: 'failed',
})

export class DshServer extends EventEmitter {
  #options
  #child = null
  #status = { state: DSH_STATE.STOPPED }
  #tail = []
  #stopping = false

  /**
   * @param {{
   *   nodePath: string,
   *   nodeVersion: string,
   *   nodeEnv?: Record<string,string>,
   *   dshBin: string,
   *   profile: string,
   *   host?: string,
   *   port?: number,
   *   cwd: string,
   *   dshHome: string,
   *   logFile: string,
   *   readyTimeoutMs?: number,
   *   extraArgs?: string[],
   * }} options
   */
  constructor(options) {
    super()
    this.#options = {
      host: '127.0.0.1',
      port: 0,
      // 冷启动通常 5~10 秒（要加载几百个 ESM 插件）；首次建存储会更久，留足余量
      readyTimeoutMs: 180_000,
      extraArgs: [],
      ...options,
    }
  }

  get status() {
    return { ...this.#status }
  }

  get tail() {
    return this.#tail.slice(-TAIL_LINES)
  }

  async start() {
    if (this.#status.state === DSH_STATE.STARTING || this.#status.state === DSH_STATE.READY) {
      return this.status
    }

    const options = this.#options
    mkdirSync(dirname(options.logFile), { recursive: true })
    this.#rotateLog(options.logFile)

    const args = [
      options.dshBin,
      '--profile',
      options.profile,
      '--host',
      options.host,
      '--port',
      String(options.port),
      '--no-open',
      ...options.extraArgs,
    ]

    this.#update({
      state: DSH_STATE.STARTING,
      profile: options.profile,
      node: options.nodeVersion,
      nodePath: options.nodePath,
      startedAt: new Date().toISOString(),
      error: undefined,
      url: undefined,
    })

    const env = {
      ...process.env,
      ...options.nodeEnv,
      DSH_HOME: options.dshHome,
      DSH_TELEMETRY_DISABLED: process.env.DSH_TELEMETRY_DISABLED ?? '1',
    }
    // 外部 shell 的 NODE_OPTIONS 常被各种工具注入 --require 垫片（沙箱、APM、TS 加载器等），
    // 这些 CJS 垫片会劫持 fs.rename 之类的原语，直接把 dsh 的原子写/存储搞坏。
    delete env.NODE_OPTIONS
    if (options.nodeEnv?.ELECTRON_RUN_AS_NODE) env.ELECTRON_RUN_AS_NODE = '1'
    else delete env.ELECTRON_RUN_AS_NODE

    this.#writeLog(`[desktop] spawn ${options.nodePath} ${args.join(' ')}`)

    return await new Promise((resolve) => {
      let settled = false
      const finish = (status) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve(status)
      }

      const child = spawn(options.nodePath, args, {
        cwd: options.cwd,
        env,
        // POSIX 上单独进程组，退出时按组发信号，连带清掉 dsh 自己起的 shell 子进程
        detached: process.platform !== 'win32',
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      this.#child = child

      const timer = setTimeout(() => {
        this.#writeLog('[desktop] dsh 启动超时')
        this.#terminate('SIGTERM')
        const status = this.#update({
          state: DSH_STATE.FAILED,
          error: `dsh 在 ${Math.round(options.readyTimeoutMs / 1000)} 秒内没有输出可用地址`,
          tail: this.tail,
        })
        this.#child = null
        finish(status)
      }, options.readyTimeoutMs)

      const onLine = (source) => (chunk) => {
        for (const raw of chunk.toString('utf8').split(/\r?\n/)) {
          const line = raw.replace(ANSI, '').trimEnd()
          if (!line) continue
          this.#pushTail(`[${source}] ${line}`)
          this.#writeLog(`[${source}] ${line}`)
          const match = /^dsh web:\s+(https?:\/\/\S+)/.exec(line.trim())
          if (match && !settled) this.#confirmUrl(match[1]).then(finish)
        }
      }

      child.stdout.on('data', onLine('dsh'))
      child.stderr.on('data', onLine('dsh:err'))

      child.on('error', (error) => {
        this.#writeLog(`[desktop] dsh 进程错误：${error.message}`)
        this.#child = null
        finish(this.#update({ state: DSH_STATE.FAILED, error: error.message, tail: this.tail }))
      })

      child.on('exit', (code, signal) => {
        this.#child = null
        if (this.#stopping) return
        if (this.#status.state === DSH_STATE.READY || this.#status.state === DSH_STATE.STARTING) {
          this.#writeLog(`[desktop] dsh 意外退出 code=${code} signal=${signal}`)
          finish(
            this.#update({
              state: DSH_STATE.FAILED,
              error: `dsh 进程退出（code=${code ?? 'null'} signal=${signal ?? 'null'}）`,
              tail: this.tail,
            }),
          )
        }
      })
    })
  }

  async stop() {
    const child = this.#child
    this.#stopping = true
    if (!child || child.exitCode !== null) {
      this.#child = null
      this.#stopping = false
      return this.#update({ state: DSH_STATE.STOPPED, url: undefined, pid: undefined })
    }

    this.#writeLog('[desktop] 停止 dsh')
    const exited = new Promise((resolve) => child.once('exit', resolve))
    this.#terminate('SIGTERM')

    const timeout = new Promise((resolve) => setTimeout(() => resolve('timeout'), 5000))
    const result = await Promise.race([exited, timeout])
    if (result === 'timeout') {
      this.#writeLog('[desktop] dsh 未在 5 秒内退出，强制结束')
      this.#terminate('SIGKILL')
      await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 2000))])
    }

    this.#child = null
    this.#stopping = false
    return this.#update({ state: DSH_STATE.STOPPED, url: undefined, pid: undefined })
  }

  async restart() {
    await this.stop()
    return await this.start()
  }

  /** 同步兜底：主进程即将退出、来不及 await 时用 */
  killNow() {
    const child = this.#child
    if (!child?.pid) return
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', detached: true }).unref()
      } else {
        process.kill(-child.pid, 'SIGKILL')
      }
    } catch {
      /* 进程已经没了 */
    }
  }

  #terminate(signal) {
    const child = this.#child
    if (!child?.pid) return
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
      } else {
        process.kill(-child.pid, signal)
      }
    } catch {
      try {
        child.kill(signal)
      } catch {
        /* 进程已经没了 */
      }
    }
  }

  async #confirmUrl(url) {
    const deadline = Date.now() + 20_000
    while (Date.now() < deadline) {
      try {
        const response = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(3000) })
        if (response.status < 500) {
          this.#writeLog(`[desktop] dsh 就绪 ${url}（HTTP ${response.status}）`)
          return this.#update({
            state: DSH_STATE.READY,
            url,
            pid: this.#child?.pid,
            tail: undefined,
          })
        }
      } catch {
        /* 还没监听，继续等 */
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    this.#writeLog('[desktop] dsh 打印了地址但未通过探活')
    return this.#update({
      state: DSH_STATE.FAILED,
      error: 'dsh 打印了访问地址，但 HTTP 探活失败',
      tail: this.tail,
    })
  }

  #update(patch) {
    this.#status = { ...this.#status, ...patch }
    this.emit('status', this.status)
    return this.status
  }

  #pushTail(line) {
    this.#tail.push(line)
    if (this.#tail.length > TAIL_LINES * 2) this.#tail.splice(0, this.#tail.length - TAIL_LINES)
  }

  #writeLog(line) {
    try {
      appendFileSync(this.#options.logFile, `${new Date().toISOString()} ${line}\n`)
    } catch {
      /* 日志失败不影响主流程 */
    }
  }

  #rotateLog(file) {
    try {
      if (statSync(file).size > MAX_LOG_BYTES) renameSync(file, `${file}.1`)
    } catch {
      /* 文件不存在 */
    }
  }
}
