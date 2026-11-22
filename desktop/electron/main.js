/**
 * 碧游宫桌面客户端 · 主进程
 *
 * 职责（S1）：
 *   - 单实例锁、窗口生命周期、退出清理
 *   - 用锁定的 Node 启动 dsh web profile，拿到本地 URL
 *   - 品牌壳（本进程加载 ui/shell）+ dsh UI（WebContentsView 独立渲染进程）
 *   - 受控 IPC：渲染层只能通过 preload 白名单拿状态、重启、改布局
 */
import { app, BrowserWindow, Menu, WebContentsView, ipcMain, nativeImage, nativeTheme, shell } from 'electron'
import { createSocket } from 'node:dgram'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { DESKTOP_ROOT, defaultUserDataDir, layoutPaths } from './paths.js'
import { PINNED } from './versions.js'
import { resolveNode } from './dsh/resolve-node.js'
import { provisionProfile } from './dsh/provision.js'
import { DSH_STATE, DshServer } from './dsh/server.js'
import { FormationHost } from './lan-host/index.js'

const IS_DEV = !app.isPackaged
const IS_SMOKE = process.argv.includes('--smoke')
const IS_SMOKE_QUIT = process.argv.includes('--smoke-quit')
const OPEN_DEVTOOLS = process.argv.includes('--devtools')

const USER_DATA = defaultUserDataDir()
app.setName('碧游宫')
app.setPath('userData', USER_DATA)

const paths = layoutPaths(USER_DATA)
mkdirSync(paths.logs, { recursive: true })

/** 单实例锁：第二个实例直接把已有窗口顶到前面 */
const hasSingleInstanceLock = app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) app.exit(0)

let mainWindow = null
let dshView = null
let dshServer = null
let lanHost = null
// 与 dsh 的工具栏节奏对齐（14px 字号、28/32px 控件、12px 圆角），壳顶栏取 52px
let chromeHeight = 52
let cleanupDone = false
// 对话页与联阵页共用主区：联阵页在壳里，需要先隐藏 dsh 视图
let dshVisible = true

// ---------------------------------------------------------------- 日志

function log(message) {
  const line = `${new Date().toISOString()} [main] ${message}\n`
  process.stdout.write(line)
  try {
    writeFileSync(paths.desktopLog, line, { flag: 'a' })
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------- 配置

function readConfig() {
  try {
    return JSON.parse(readFileSync(paths.configFile, 'utf8'))
  } catch {
    return {}
  }
}

function writeConfig(config) {
  try {
    writeFileSync(paths.configFile, `${JSON.stringify(config, null, 2)}\n`)
  } catch (error) {
    log(`写配置失败：${error.message}`)
  }
}

const config = readConfig()

// ---------------------------------------------------------------- 窗口

function createWindow() {
  const bounds = config.window ?? {}
  const window = new BrowserWindow({
    width: bounds.width ?? 1180,
    height: bounds.height ?? 800,
    x: bounds.x,
    y: bounds.y,
    minWidth: 960,
    minHeight: 620,
    show: false,
    backgroundColor: '#0b0c10',
    title: '碧游宫',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    trafficLightPosition: process.platform === 'darwin' ? { x: 16, y: 18 } : undefined,
    webPreferences: {
      preload: join(DESKTOP_ROOT, 'electron', 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  })

  window.once('ready-to-show', () => window.show())

  window.on('resize', () => layoutDshView())
  window.on('enter-full-screen', () => layoutDshView())
  window.on('leave-full-screen', () => layoutDshView())

  window.on('close', () => {
    if (!window.isMinimized() && !window.isFullScreen()) {
      const { width, height, x, y } = window.getBounds()
      writeConfig({ ...readConfig(), window: { width, height, x, y } })
    }
  })

  window.on('closed', () => {
    destroyDshView()
    mainWindow = null
  })

  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file://')) {
      event.preventDefault()
      shell.openExternal(url)
    }
  })

  window.loadFile(join(DESKTOP_ROOT, 'ui', 'shell', 'index.html'))
  mainWindow = window
  return window
}

function windowIsAlive() {
  return mainWindow !== null && !mainWindow.isDestroyed()
}

// ---------------------------------------------------------------- dsh 视图

function ensureDshView() {
  if (dshView || !windowIsAlive()) return dshView

  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      partition: 'persist:dsh',
    },
  })
  view.setBackgroundColor('#0b0c10')
  view.setVisible(false)

  // dsh UI 里的外链一律交给系统浏览器，不在壳内开新窗口
  view.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  mainWindow.contentView.addChildView(view)
  dshView = view
  layoutDshView()
  return view
}

function destroyDshView() {
  if (!dshView) return
  try {
    if (windowIsAlive()) mainWindow.contentView.removeChildView(dshView)
    dshView.webContents.close()
  } catch (error) {
    log(`销毁 dsh 视图失败：${error.message}`)
  }
  dshView = null
}

function layoutDshView() {
  if (!dshView || !windowIsAlive()) return
  const [width, height] = mainWindow.getContentSize()
  dshView.setBounds({
    x: 0,
    y: chromeHeight,
    width,
    height: Math.max(0, height - chromeHeight),
  })
}

// ---------------------------------------------------------------- 状态广播

function broadcastStatus(status) {
  if (windowIsAlive()) mainWindow.webContents.send('dsh:status', status)
}

// ---------------------------------------------------------------- 联阵

/**
 * 联阵服务默认不创建：只有用户主动开阵（或 UI 查询状态）时才实例化。
 * 实例本身在 standalone 状态下不监听任何端口。
 */
function ensureLanHost() {
  if (lanHost) return lanHost
  lanHost = new FormationHost({ logger: log, appVersion: app.getVersion() })
  lanHost.on('status', (snapshot) => {
    if (windowIsAlive()) mainWindow.webContents.send('lan:status', snapshot)
  })
  return lanHost
}

function attachServer(server) {
  server.on('status', (status) => {
    broadcastStatus(status)
    if (status.state === DSH_STATE.READY) {
      const view = ensureDshView()
      if (view && view.webContents.getURL() !== status.url) {
        view.setVisible(dshVisible)
        view.webContents.loadURL(status.url).catch((error) => log(`载入 dsh UI 失败：${error.message}`))
      }
      if (OPEN_DEVTOOLS && view) view.webContents.openDevTools({ mode: 'detach' })
    } else if (status.state === DSH_STATE.FAILED || status.state === DSH_STATE.STOPPED) {
      destroyDshView()
    }
  })
}

async function startDsh() {
  const node = resolveNode({ desktopRoot: DESKTOP_ROOT })
  const provisioning = provisionProfile({
    userDataDir: USER_DATA,
    profile: PINNED.profile,
    runtimeRoot: join(DESKTOP_ROOT, 'runtime'),
  })
  log(
    `node=${node.version}(${node.source}) dsh=${provisioning.dshVersion} profile=${PINNED.profile} ` +
      `exact=${node.exact ? 'yes' : 'no'}`,
  )

  dshServer = new DshServer({
    nodePath: node.execPath,
    nodeVersion: node.version,
    nodeEnv: node.env,
    dshBin: provisioning.dshBin,
    profile: PINNED.profile,
    host: '127.0.0.1',
    // 每次让系统分配空闲端口，避免和用户自己的 dsh web 抢 3080 之类的常见端口
    port: 0,
    cwd: paths.root,
    dshHome: provisioning.dshHome,
    logFile: paths.dshLog,
    readyTimeoutMs: IS_SMOKE ? 300_000 : 240_000,
  })
  attachServer(dshServer)

  const status = await dshServer.start()
  if (status.state !== DSH_STATE.READY) {
    log(`dsh 启动失败：${status.error ?? 'unknown'}`)
  }
  return status
}

async function stopDsh() {
  if (!dshServer) return
  try {
    await dshServer.stop()
  } catch (error) {
    log(`停止 dsh 失败：${error.message}`)
  }
}

// ---------------------------------------------------------------- IPC

function senderIsShell(event) {
  return windowIsAlive() && event.sender.id === mainWindow.webContents.id
}

function registerIpc() {
  ipcMain.handle('app:info', (event) => {
    if (!senderIsShell(event)) return null
    return {
      name: app.getName(),
      version: app.getVersion(),
      platform: process.platform,
      isDev: IS_DEV,
      versions: {
        electron: process.versions.electron,
        chrome: process.versions.chrome,
        node: process.versions.node,
      },
      pinned: PINNED,
      paths: { userData: USER_DATA, logs: paths.logs, dshLog: paths.dshLog },
    }
  })

  ipcMain.handle('dsh:status', (event) => (senderIsShell(event) ? (dshServer?.status ?? { state: DSH_STATE.STOPPED }) : null))

  ipcMain.handle('dsh:restart', async (event) => {
    if (!senderIsShell(event)) return null
    if (!dshServer) return await startDsh()
    return await dshServer.restart()
  })

  ipcMain.handle('dsh:log-tail', (event) => (senderIsShell(event) ? (dshServer?.tail ?? []) : []))

  ipcMain.handle('lan:status', (event) => (senderIsShell(event) ? ensureLanHost().snapshot() : null))

  ipcMain.handle('lan:open', async (event, options) => {
    if (!senderIsShell(event)) return null
    try {
      return { ok: true, ...(await ensureLanHost().open(options ?? {})) }
    } catch (error) {
      log(`开阵失败：${error.message}`)
      return { ok: false, error: { code: 'open_failed', message: error.message } }
    }
  })

  ipcMain.handle('lan:disband', async (event) => {
    if (!senderIsShell(event)) return null
    return { ok: true, ...(await ensureLanHost().stop()) }
  })

  ipcMain.handle('lan:kick', (event, memberId) => {
    if (!senderIsShell(event)) return null
    const result = ensureLanHost().kick(String(memberId ?? ''))
    return { status: result.status, ...result.body }
  })

  ipcMain.handle('shell:open-logs', async (event) => {
    if (!senderIsShell(event)) return false
    await shell.openPath(paths.logs)
    return true
  })

  ipcMain.on('shell:chrome-height', (event, height) => {
    if (!senderIsShell(event)) return
    const next = Number(height)
    if (!Number.isFinite(next)) return
    const clamped = Math.min(120, Math.max(40, Math.round(next)))
    if (clamped === chromeHeight) return
    chromeHeight = clamped
    layoutDshView()
  })

  ipcMain.on('shell:devtools', (event) => {
    if (!senderIsShell(event)) return
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  })

  ipcMain.on('shell:dsh-visible', (event, visible) => {
    if (!senderIsShell(event)) return
    dshVisible = Boolean(visible)
    dshView?.setVisible(dshVisible)
  })
}

// ---------------------------------------------------------------- 菜单

function buildMenu() {
  const isMac = process.platform === 'darwin'
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载壳' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '窗口',
      submenu: [{ role: 'minimize', label: '最小化' }, { role: 'close', label: '关闭' }],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '打开日志目录',
          click: () => shell.openPath(paths.logs),
        },
        {
          label: '关于碧游宫',
          click: () => app.showAboutPanel(),
        },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ---------------------------------------------------------------- 生命周期

app.on('second-instance', () => {
  if (!windowIsAlive()) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
})

app.on('window-all-closed', () => {
  // 桌面客户端关窗即退出，顺带清理 dsh 子进程；不做 macOS 常驻
  app.quit()
})

app.on('before-quit', (event) => {
  const hosting = Boolean(lanHost && lanHost.state !== 'standalone')
  if (cleanupDone || (!dshServer && !hosting)) return
  event.preventDefault()
  Promise.allSettled([stopDsh(), hosting ? lanHost.stop() : Promise.resolve()]).finally(() => {
    cleanupDone = true
    app.quit()
  })
})

app.on('will-quit', () => {
  destroyDshView()
})

// 主进程被强杀前的最后一道兜底：同步发信号，避免留下孤儿 dsh
process.on('exit', () => {
  if (!dshServer) return
  if (dshServer.status.state === DSH_STATE.READY || dshServer.status.state === DSH_STATE.STARTING) {
    dshServer.killNow()
  }
})

app.whenReady().then(async () => {
  if (!hasSingleInstanceLock) return
  // 默认浅色（与 dsh 白底 #ffffff / 侧栏 #f9fafb 一致）；BIYOU_THEME=light|dark|system 可覆盖。
  // dsh 的主题偏好默认「跟随系统」，所以这里定基调，dsh UI 会跟着落到同一套。
  const theme = process.env.BIYOU_THEME ?? 'light'
  nativeTheme.themeSource = ['light', 'dark', 'system'].includes(theme) ? theme : 'light'
  if (process.platform === 'darwin') {
    const icon = nativeImage.createFromPath(join(DESKTOP_ROOT, 'assets', 'icon.png'))
    if (!icon.isEmpty()) app.dock?.setIcon(icon)
  }
  app.setAboutPanelOptions({
    applicationName: '碧游宫',
    applicationVersion: app.getVersion(),
    copyright: '碧游宫 · 内网异构推理集群',
    icon: nativeImage.createFromPath(join(DESKTOP_ROOT, 'assets', 'icon.png')),
  })

  buildMenu()
  registerIpc()
  const window = createWindow()
  if (OPEN_DEVTOOLS) window.webContents.openDevTools({ mode: 'detach' })

  let status
  try {
    status = await startDsh()
  } catch (error) {
    log(`dsh 启动前置失败：${error.message}`)
    // 前置失败（Node 版本不达标、profile 依赖没装）也要让壳显示原因，而不是卡在「起阵中」
    status = { state: DSH_STATE.FAILED, error: error.message, tail: [] }
    broadcastStatus(status)
  }

  if (IS_SMOKE) {
    window.show()
    window.focus()
    app.focus({ steal: true })
    await runSmoke(window, status)
  }

  if (IS_SMOKE_QUIT) {
    // 验证「关窗即退出 → 子进程被清理」这条真实路径
    await sleep(1500)
    window.close()
  }

  app.on('activate', () => {
    if (!windowIsAlive()) createWindow()
  })
})

// ---------------------------------------------------------------- 冒烟（--smoke）

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 等 dsh 视图真正加载完（SPA 首屏渲染完还要再等一拍） */
async function waitForDshViewLoad(timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const view = dshView
    if (view && view.webContents.getURL().startsWith('http') && !view.webContents.isLoading()) return true
    await sleep(300)
  }
  return false
}

/**
 * 冒烟探针：碧游宫插件到底有没有被装进浏览器 roster、pill 有没有真的渲染出来。
 * 这是「插件路线」D1 的验收点——只看 dsh 起得来不够，得证明我们的 occupant 上了屏。
 */
async function probeBiyouPlugin() {
  const script = `(() => {
    let boot = ''
    try { boot = JSON.stringify(window.__DSH_BOOT__ ?? null) } catch {}
    const pill = document.querySelector('[data-biyou="status"]')
    const marks = document.querySelectorAll('[data-biyou="brand-mark"]')
    const name = document.querySelector('[data-biyou="brand-name"]')
    return {
      inBoot: boot.includes('@biyou/dsh-plugin'),
      localModelsInBoot: boot.includes('@biyou/dsh-local-models'),
      pill: Boolean(pill),
      text: pill ? pill.textContent : null,
      state: pill ? pill.getAttribute('data-state') : null,
      brandMarkCount: marks.length,
      brandName: name ? name.textContent : null,
    }
  })()`

  const deadline = Date.now() + 8_000
  let last = null
  while (Date.now() < deadline) {
    last = await dshView.webContents.executeJavaScript(script).catch((error) => ({ error: error.message }))
    if (last?.pill && last.brandMarkCount > 0 && last.state === 'connected') return last
    await sleep(400)
  }
  return last
}

/** S6 壳 UI：联阵入口存在、切换时 dsh 视图让位、preload 能拿到联阵状态 */
async function probeShellUi(window) {
  const info = await window.webContents.executeJavaScript(`(() => {
    const nav = document.querySelector('.nav-item[data-entry="lan"]')
    const panel = document.getElementById('state-lan')
    const form = document.getElementById('lan-form')
    const chip = document.getElementById('lan-pill')
    return {
      nav: Boolean(nav),
      panel: Boolean(panel),
      form: Boolean(form),
      chip: Boolean(chip),
      chipText: chip ? chip.textContent.trim() : null,
    }
  })()`)

  const ipcState = await window.webContents
    .executeJavaScript('window.biyou.lan.status().then((snapshot) => snapshot?.state ?? null)')
    .catch(() => null)

  await window.webContents.executeJavaScript(`document.querySelector('.nav-item[data-entry="lan"]')?.click()`)
  await sleep(200)
  const dshHidden = dshView ? !dshView.getVisible() : false
  const lanVisible = await window.webContents.executeJavaScript(
    `!document.getElementById('state-lan')?.classList.contains('is-hidden')`,
  )
  try {
    const shot = await window.webContents.capturePage()
    const { writeFile } = await import('node:fs/promises')
    await writeFile(join(paths.smokeDir, 'lan.png'), shot.toPNG())
  } catch (error) {
    info.captureError = error.message
  }

  let uiOpened = null
  let hostingVisible = false
  let chipHosting = null
  let memberRows = 0
  let kickButtons = 0
  let kicked = false
  let uiStopped = false
  try {
    const opened = await window.webContents.executeJavaScript(
      `window.biyou.lan.open({name:'壳冒烟阵',mode:'open',capacity:2,httpPort:0,discoveryPort:0,announceIntervalMs:0})`,
    )
    uiOpened = opened?.state ?? null
    await sleep(300)
    hostingVisible = await window.webContents.executeJavaScript(
      `!document.getElementById('lan-hosting')?.classList.contains('is-hidden')`,
    )
    chipHosting = await window.webContents.executeJavaScript(
      `document.getElementById('lan-text')?.textContent?.trim() ?? null`,
    )
    try {
      const shot = await window.webContents.capturePage()
      const { writeFile } = await import('node:fs/promises')
      await writeFile(join(paths.smokeDir, 'lan-hosting.png'), shot.toPNG())
    } catch (error) {
      info.captureError = error.message
    }

    const httpPort = opened?.endpoints?.http_port
    const roomId = opened?.room?.room_id
    if (httpPort && roomId) {
      const joined = await fetch(`http://127.0.0.1:${httpPort}/lan/v1/join`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          protocol: 1,
          room_id: roomId,
          device: {
            device_id: 'ui-smoke-device',
            name: '壳冒烟仙徒',
            platform: 'smoke',
            app_version: app.getVersion(),
          },
        }),
      }).then((response) => response.json())
      if (joined.ok === true) {
        await sleep(350)
        memberRows = await window.webContents.executeJavaScript(
          `document.querySelectorAll('.lan-member').length`,
        )
        kickButtons = await window.webContents.executeJavaScript(
          `document.querySelectorAll('button[data-kick]').length`,
        )
        try {
          const shot = await window.webContents.capturePage()
          const { writeFile } = await import('node:fs/promises')
          await writeFile(join(paths.smokeDir, 'lan-members.png'), shot.toPNG())
        } catch (error) {
          info.captureError = error.message
        }
        if (kickButtons > 0) {
          await window.webContents.executeJavaScript(`document.querySelector('button[data-kick]')?.click()`)
          await sleep(450)
          kicked =
            (await window.webContents.executeJavaScript(`document.querySelectorAll('.lan-member').length`)) === 0
        }
      }
    }

    await window.webContents.executeJavaScript('window.biyou.lan.disband()')
    await sleep(350)
    uiStopped = await window.webContents.executeJavaScript(
      `!document.getElementById('lan-form')?.classList.contains('is-hidden')`,
    )
  } catch (error) {
    info.uiError = error.message
  }

  await window.webContents.executeJavaScript(`document.querySelector('.nav-item[data-entry="dialog"]')?.click()`)
  await sleep(200)
  const dshShown = dshView ? dshView.getVisible() : false

  return {
    ...info,
    ipcState,
    dshHidden,
    lanVisible,
    uiOpened,
    hostingVisible,
    chipHosting,
    memberRows,
    kickButtons,
    kicked,
    uiStopped,
    dshShown,
    ok: Boolean(
      info.nav &&
        info.panel &&
        info.form &&
        info.chip &&
        info.chipText === '单机' &&
        ipcState === 'standalone' &&
        dshHidden &&
        lanVisible &&
        uiOpened === 'hosting' &&
        hostingVisible &&
        chipHosting?.includes('阵眼') &&
        memberRows === 1 &&
        kickButtons === 1 &&
        kicked &&
        uiStopped &&
        dshShown,
    ),
  }
}

async function discoverRoomUdp(port, timeoutMs = 1500) {
  if (!port) return null
  return await new Promise((resolve) => {
    const socket = createSocket('udp4')
    let settled = false
    let timer = null
    const done = (value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      try {
        socket.close()
      } catch {
        /* 已关闭 */
      }
      resolve(value)
    }
    timer = setTimeout(() => done(null), timeoutMs)
    socket.on('error', () => done(null))
    socket.on('message', (data) => {
      try {
        done(JSON.parse(data.toString('utf8')))
      } catch {
        done(null)
      }
    })
    socket.bind(0, '127.0.0.1', () => {
      const packet = Buffer.from(JSON.stringify({ t: 'discover', v: 1 }), 'utf8')
      socket.send(packet, port, '127.0.0.1', () => {})
    })
  })
}

/**
 * S5 冒烟：默认单机 → 开阵 → HTTP 入阵/心跳 → UDP 发现 → 散阵 → 端口关闭。
 * 这里不经过 preload，只验证主进程能完整拉起并回收联阵服务。
 */
async function probeLanHost() {
  const host = ensureLanHost()
  const before = host.snapshot()
  const report = {
    defaultState: before.state,
    defaultRoom: before.room === null,
    defaultEndpoints: before.endpoints === null,
  }
  let base = null
  try {
    const snapshot = await host.open({
      name: '冒烟阵',
      mode: 'open',
      capacity: 2,
      httpPort: 0,
      discoveryPort: 0,
      announceIntervalMs: 0,
    })
    base = `http://127.0.0.1:${snapshot.endpoints.http_port}`
    const health = await fetch(`${base}/lan/v1/health`).then((response) => response.json())
    const room = await fetch(`${base}/lan/v1/room`).then((response) => response.json())
    const device = {
      device_id: 'smoke-desktop',
      name: '冒烟设备',
      platform: 'smoke',
      app_version: app.getVersion(),
    }
    const joined = await fetch(`${base}/lan/v1/join`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ protocol: 1, room_id: snapshot.room.room_id, device }),
    }).then((response) => response.json())
    const discovered = await discoverRoomUdp(snapshot.endpoints.discovery_port)

    report.opened = {
      state: snapshot.state,
      httpPort: snapshot.endpoints.http_port,
      discoveryPort: snapshot.endpoints.discovery_port,
      health: health.ok === true,
      roomId: room.room?.room_id === snapshot.room.room_id,
      joined: joined.ok === true && joined.room?.players === 1,
      discovered: discovered?.t === 'room' && discovered?.room_id === snapshot.room.room_id,
    }
    report.ok =
      report.defaultState === 'standalone' &&
      report.defaultRoom &&
      report.defaultEndpoints &&
      report.opened.state === 'hosting' &&
      report.opened.health &&
      report.opened.roomId &&
      report.opened.joined &&
      report.opened.discovered
  } catch (error) {
    report.error = error.message
    report.ok = false
  } finally {
    await host.stop().catch((error) => log(`冒烟散阵失败：${error.message}`))
  }

  const after = host.snapshot()
  report.afterState = after.state
  report.afterRoom = after.room
  let httpClosed = false
  if (base) {
    try {
      await fetch(`${base}/lan/v1/health`, { signal: AbortSignal.timeout(500) })
    } catch {
      httpClosed = true
    }
  }
  report.httpClosed = httpClosed
  report.ok = Boolean(report.ok && after.state === 'standalone' && after.room === null && httpClosed)
  return report
}

async function runSmoke(window, status) {
  const { writeFile } = await import('node:fs/promises')
  mkdirSync(paths.smokeDir, { recursive: true })
  const report = {
    ok: status.state === DSH_STATE.READY,
    status,
    app: { version: app.getVersion(), electron: process.versions.electron, node: process.versions.node },
  }

  try {
    await sleep(600)
    const shellShot = await window.webContents.capturePage()
    await writeFile(join(paths.smokeDir, 'shell.png'), shellShot.toPNG())

    if (dshView) {
      report.dshView = { loaded: await waitForDshViewLoad(), url: dshView.webContents.getURL() }
      report.dshView.colorScheme = await dshView.webContents
        .executeJavaScript("({ dark: matchMedia('(prefers-color-scheme: dark)').matches, bg: getComputedStyle(document.body).backgroundColor })")
        .catch((error) => ({ error: error.message }))
      await sleep(800)
      try {
        const dshShot = await dshView.webContents.capturePage()
        await writeFile(join(paths.smokeDir, 'dsh.png'), dshShot.toPNG())
        report.dshView.blank = dshShot.isEmpty()
      } catch (error) {
        // 窗口被遮挡 / 无合成表面时 capturePage 会失败，退回 printToPDF 仍可证明页面渲染成功
        report.dshView.captureError = error.message
        const pdf = await dshView.webContents.printToPDF({ printBackground: true })
        await writeFile(join(paths.smokeDir, 'dsh.pdf'), pdf)
      }
      report.dshView.size = dshView.getBounds()
      report.dshView.biyou = await probeBiyouPlugin()
      const biyou = report.dshView.biyou
      report.dshView.biyouOk = Boolean(
        biyou?.inBoot &&
          biyou.pill &&
          biyou.state === 'connected' &&
          biyou.brandMarkCount > 0 &&
          biyou.brandName === '碧游宫',
      )
      if (!report.dshView.biyouOk) report.ok = false
      // 本地模型插件的 browser half 也必须进 roster；页面渲染由设置页人工验收。
      report.dshView.localModelsOk = Boolean(biyou?.localModelsInBoot)
      if (!report.dshView.localModelsOk) report.ok = false
    }

    report.shell = await probeShellUi(window)
    if (!report.shell.ok) report.ok = false

    report.lan = await probeLanHost()
    if (!report.lan.ok) report.ok = false
  } catch (error) {
    report.captureError = error.message
  }

  await writeFile(join(paths.smokeDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`)

  process.stdout.write(`SMOKE ${JSON.stringify(report)}\n`)
  cleanupDone = true
  await stopDsh()
  app.exit(report.ok ? 0 : 1)
}
