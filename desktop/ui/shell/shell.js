/**
 * 品牌壳渲染层：只做三件事——报顶栏高度、渲染 dsh 启动状态、接白名单 IPC。
 * dsh 的界面在本视图之下由主进程用 WebContentsView 承载，这里不 iframe、不注入。
 */
const api = window.biyou

const els = {
  chrome: document.getElementById('chrome'),
  versionChip: document.getElementById('version-chip'),
  statusPill: document.getElementById('status-pill'),
  statusText: document.getElementById('status-text'),
  restart: document.getElementById('btn-restart'),
  logs: document.getElementById('btn-logs'),
  retry: document.getElementById('btn-retry'),
  openLogs: document.getElementById('btn-open-logs'),
  panels: {
    starting: document.getElementById('state-starting'),
    failed: document.getElementById('state-failed'),
    ready: document.getElementById('state-ready'),
  },
  failedMessage: document.getElementById('failed-message'),
  failedTail: document.getElementById('failed-tail'),
  bootMeta: document.getElementById('boot-meta'),
  steps: Array.from(document.querySelectorAll('.steps li')),
  toast: document.getElementById('toast'),
}

const STATUS_TEXT = {
  starting: '启动中',
  ready: '单机 · 就绪',
  failed: '启动失败',
  stopped: '已停止',
}

let toastTimer = null

function toast(message) {
  els.toast.textContent = message
  els.toast.classList.add('is-visible')
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => els.toast.classList.remove('is-visible'), 2200)
}

function showPanel(name) {
  for (const [key, node] of Object.entries(els.panels)) {
    node.classList.toggle('is-hidden', key !== name)
  }
}

function setSteps(activeStep) {
  const order = ['profile', 'runtime', 'ui']
  const activeIndex = order.indexOf(activeStep)
  for (const item of els.steps) {
    const index = order.indexOf(item.dataset.step)
    item.classList.toggle('is-done', activeIndex > index || activeStep === 'done')
    item.classList.toggle('is-active', index === activeIndex)
  }
}

function render(status) {
  if (!status) return
  const state = status.state ?? 'starting'
  els.statusPill.dataset.state = state
  els.statusText.textContent = STATUS_TEXT[state] ?? state
  els.restart.disabled = state === 'starting'

  if (state === 'starting') {
    showPanel('starting')
    setSteps('runtime')
    const bits = []
    if (status.node) bits.push(`Node ${status.node}`)
    bits.push(`profile ${status.profile ?? 'biyou'}`)
    els.bootMeta.textContent = bits.join(' · ')
    return
  }

  if (state === 'ready') {
    setSteps('done')
    showPanel('ready')
    const bits = [`Node ${status.node ?? '?'}`, `pid ${status.pid ?? '?'}`]
    if (status.url) bits.push(new URL(status.url).host)
    els.bootMeta.textContent = bits.join(' · ')
    return
  }

  if (state === 'failed') {
    showPanel('failed')
    setSteps('runtime')
    els.failedMessage.textContent = status.error ?? '未知错误'
    els.failedTail.textContent = (status.tail ?? []).slice(-12).join('\n') || '（无输出，详见 dsh.log）'
    els.failedTail.classList.toggle('is-hidden', !(status.tail ?? []).length)
    return
  }

  showPanel('starting')
  setSteps('profile')
}

async function bootstrap() {
  if (!api) {
    showPanel('failed')
    els.failedMessage.textContent = '未注入 preload 桥接，页面可能被直接打开了。'
    return
  }

  document.body.classList.add(`platform-${api.platform}`)

  const info = await api.app.info()
  if (info) {
    els.versionChip.textContent = `v${info.version}`
    els.bootMeta.textContent = `Electron ${info.versions.electron} · 壳 Node ${info.versions.node} · dsh ${info.pinned.dsh}`
    document.title = `碧游宫 v${info.version}`
  }

  render(await api.dsh.status())
  api.dsh.onStatus(render)
}

// 顶栏高度交给主进程，dsh 视图据此让位（拖动/缩放/全屏都会重算）
function reportChromeHeight() {
  const height = Math.round(els.chrome.getBoundingClientRect().height)
  api?.shell.setChromeHeight(height)
}

new ResizeObserver(reportChromeHeight).observe(els.chrome)
reportChromeHeight()

// ---------------------------------------------------------------- 交互

els.restart.addEventListener('click', async () => {
  toast('正在重启 dsh 运行时…')
  const status = await api.dsh.restart()
  if (status) render(status)
})

els.logs.addEventListener('click', () => api.shell.openLogs())
els.openLogs.addEventListener('click', () => api.shell.openLogs())

els.retry.addEventListener('click', async () => {
  showPanel('starting')
  setSteps('runtime')
  const status = await api.dsh.restart()
  if (status) render(status)
})

for (const item of document.querySelectorAll('.nav-item')) {
  item.addEventListener('click', () => {
    const entry = item.dataset.entry
    if (entry === 'dialog') {
      toast('当前就在对话入口')
      return
    }
    const label = item.textContent ?? entry
    toast(`${label}：S3/S6 阶段交付`)
  })
}

// 双击顶栏空白处不应触发导航；hiddenInset 下系统已处理缩放，这里只挡住意外选择
els.chrome.addEventListener('dblclick', (event) => {
  if (event.target === els.chrome) event.preventDefault()
})

bootstrap().catch((error) => {
  showPanel('failed')
  els.failedMessage.textContent = `壳初始化失败：${error.message}`
})
