/**
 * 品牌壳渲染层：顶栏、dsh 启动状态、联阵页。
 *
 * 对话页由主进程用 WebContentsView 承载；切到「联阵」时隐藏该视图，
 * 让壳自己的联阵页占用主区。这里不 iframe、不往 dsh 页面注入脚本。
 */
const api = window.biyou

const els = {
  chrome: document.getElementById('chrome'),
  statusPill: document.getElementById('status-pill'),
  statusText: document.getElementById('status-text'),
  lanPill: document.getElementById('lan-pill'),
  lanText: document.getElementById('lan-text'),
  restart: document.getElementById('btn-restart'),
  logs: document.getElementById('btn-logs'),
  retry: document.getElementById('btn-retry'),
  openLogs: document.getElementById('btn-open-logs'),
  panels: {
    starting: document.getElementById('state-starting'),
    failed: document.getElementById('state-failed'),
    ready: document.getElementById('state-ready'),
    lan: document.getElementById('state-lan'),
  },
  failedMessage: document.getElementById('failed-message'),
  failedTail: document.getElementById('failed-tail'),
  bootMeta: document.getElementById('boot-meta'),
  steps: Array.from(document.querySelectorAll('.steps li')),
  toast: document.getElementById('toast'),

  lanBadge: document.getElementById('lan-badge'),
  lanForm: document.getElementById('lan-form'),
  lanName: document.getElementById('lan-name'),
  lanCapacity: document.getElementById('lan-capacity'),
  lanPasscode: document.getElementById('lan-passcode'),
  lanPasscodeField: document.getElementById('lan-passcode-field'),
  lanOpen: document.getElementById('lan-open'),
  lanError: document.getElementById('lan-error'),
  lanHosting: document.getElementById('lan-hosting'),
  lanRoomName: document.getElementById('lan-room-name'),
  lanRoomMode: document.getElementById('lan-room-mode'),
  lanRoomPlayers: document.getElementById('lan-room-players'),
  lanRoomPort: document.getElementById('lan-room-port'),
  lanRoomDiscovery: document.getElementById('lan-room-discovery'),
  lanMembers: document.getElementById('lan-members'),
  lanMembersCount: document.getElementById('lan-members-count'),
  lanEmpty: document.getElementById('lan-empty'),
  lanRefresh: document.getElementById('lan-refresh'),
  lanDisband: document.getElementById('lan-disband'),
}

const STATUS_TEXT = {
  starting: '启动中',
  ready: '就绪',
  failed: '启动失败',
  stopped: '已停止',
}

let toastTimer = null
let currentView = 'dialog'
let lastDshStatus = null
let lanSnapshot = null

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

// ---------------------------------------------------------------- dsh 状态

function renderDsh(status) {
  if (!status) return
  lastDshStatus = status
  const state = status.state ?? 'starting'
  els.statusPill.dataset.state = state
  els.statusText.textContent = STATUS_TEXT[state] ?? state
  els.restart.disabled = state === 'starting'

  // 联阵页在前台时不抢主区，只更新顶栏状态
  if (currentView === 'lan') return

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
    // S7：自动恢复中的 FAILED 是暂态，显示恢复提示而不是错误堆栈
    if (status.recovering) {
      els.failedMessage.textContent = status.error ?? 'dsh 意外退出，正在恢复…'
      els.failedTail.classList.add('is-hidden')
      els.restart.disabled = true
      return
    }
    els.failedMessage.textContent = status.error ?? '未知错误'
    els.failedTail.textContent = (status.tail ?? []).slice(-12).join('\n') || '（无输出，详见 dsh.log）'
    els.failedTail.classList.toggle('is-hidden', !(status.tail ?? []).length)
    return
  }

  showPanel('starting')
  setSteps('profile')
}

// ---------------------------------------------------------------- 联阵

function showLanError(message) {
  els.lanError.textContent = message ?? ''
  els.lanError.classList.toggle('is-hidden', !message)
}

function syncLanMode() {
  const mode = document.querySelector('input[name="lan-mode"]:checked')?.value ?? 'open'
  els.lanPasscodeField.hidden = mode !== 'closed'
}

function lanOptions() {
  const name = els.lanName.value.trim()
  if (!name) throw new Error('请填写阵名')
  const mode = document.querySelector('input[name="lan-mode"]:checked')?.value ?? 'open'
  const capacity = Number(els.lanCapacity.value)
  if (!Number.isInteger(capacity) || capacity < 1 || capacity > 64) {
    throw new Error('容量需为 1-64 的整数')
  }
  const options = { name, mode, capacity }
  if (mode === 'closed') {
    const passcode = els.lanPasscode.value
    if (passcode.length < 4 || passcode.length > 64) throw new Error('阵符需为 4-64 个字符')
    options.passcode = passcode
  }
  return options
}

function renderMembers(members) {
  els.lanMembers.replaceChildren()
  els.lanMembersCount.textContent = `${members.length} 人`
  els.lanEmpty.classList.toggle('is-hidden', members.length > 0)

  for (const member of members) {
    const item = document.createElement('li')
    item.className = 'lan-member'

    const main = document.createElement('div')
    main.className = 'lan-member-main'
    const name = document.createElement('strong')
    name.textContent = member.name || member.member_id
    const meta = document.createElement('span')
    meta.className = 'lan-member-meta'
    const role = member.role === 'inner' ? '内门' : '外门'
    meta.textContent = `${member.platform || '未知平台'} · ${role} · ${member.device_id ?? ''}`
    main.append(name, meta)

    const status = document.createElement('span')
    status.className = 'lan-member-status'
    status.dataset.status = member.status ?? 'active'
    status.textContent = member.status === 'offline' ? '离线' : '在线'

    const kick = document.createElement('button')
    kick.type = 'button'
    kick.className = 'text-button'
    kick.dataset.kick = member.member_id
    kick.textContent = '移出'

    item.append(main, status, kick)
    els.lanMembers.append(item)
  }
}

function renderLan(snapshot) {
  if (!snapshot) return
  lanSnapshot = snapshot
  const state = snapshot.state ?? 'standalone'
  const hosting = state === 'hosting' && Boolean(snapshot.room)
  const players = snapshot.room?.players ?? 0

  els.lanPill.dataset.state = hosting ? 'hosting' : state
  els.lanText.textContent = hosting
    ? `阵眼 · ${players} 人`
    : state === 'starting'
      ? '开阵中'
      : state === 'stopping'
        ? '散阵中'
        : '单机'
  els.lanBadge.textContent = hosting ? '阵眼' : state === 'starting' ? '开阵中' : state === 'stopping' ? '散阵中' : '单机'
  els.lanBadge.classList.toggle('is-ok', hosting)

  els.lanForm.classList.toggle('is-hidden', hosting)
  els.lanHosting.classList.toggle('is-hidden', !hosting)
  els.lanOpen.disabled = state !== 'standalone'
  if (!hosting) return

  const room = snapshot.room
  els.lanRoomName.textContent = room.name
  els.lanRoomMode.textContent = room.mode === 'closed' ? '闭阵' : '开放阵'
  els.lanRoomPlayers.textContent = `${room.players} / ${room.capacity}`
  els.lanRoomPort.textContent = `:${snapshot.endpoints?.http_port ?? '—'}`
  const discovery = snapshot.discovery
  els.lanRoomDiscovery.textContent = discovery?.ok
    ? `UDP :${discovery.port}`
    : discovery?.enabled
      ? '不可用'
      : '已关闭'
  renderMembers(snapshot.members ?? [])
}

async function refreshLan() {
  if (!api?.lan) return
  try {
    renderLan(await api.lan.status())
  } catch (error) {
    showLanError(`读取联阵状态失败：${error.message}`)
  }
}

function activateView(view) {
  currentView = view
  for (const item of document.querySelectorAll('.nav-item')) {
    item.classList.toggle('is-active', item.dataset.entry === view)
  }
  if (view === 'lan') {
    api?.shell.setDshVisible(false)
    showPanel('lan')
    if (lanSnapshot) renderLan(lanSnapshot)
    else refreshLan()
    return
  }
  api?.shell.setDshVisible(true)
  if (lastDshStatus) renderDsh(lastDshStatus)
  else showPanel('starting')
}

// ---------------------------------------------------------------- 启动

async function bootstrap() {
  if (!api) {
    showPanel('failed')
    els.failedMessage.textContent = '未注入 preload 桥接，页面可能被直接打开了。'
    return
  }

  document.body.classList.add(`platform-${api.platform}`)

  const info = await api.app.info()
  if (info) {
    els.bootMeta.textContent = `Electron ${info.versions.electron} · 壳 Node ${info.versions.node} · dsh ${info.pinned.dsh}`
    // 顶栏左上角留空：不显示品牌与版本号（macOS 原生标题栏为 hiddenInset，也不显示标题）
  }

  renderDsh(await api.dsh.status())
  api.dsh.onStatus(renderDsh)

  if (api.lan) {
    api.lan.onStatus(renderLan)
    try {
      renderLan(await api.lan.status())
    } catch (error) {
      toast(`读取联阵状态失败：${error.message}`)
    }
  }
  syncLanMode()
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
  if (status) renderDsh(status)
})

els.logs.addEventListener('click', () => api.shell.openLogs())
els.openLogs.addEventListener('click', () => api.shell.openLogs())

els.retry.addEventListener('click', async () => {
  showPanel('starting')
  setSteps('runtime')
  const status = await api.dsh.restart()
  if (status) renderDsh(status)
})

for (const item of document.querySelectorAll('.nav-item')) {
  item.addEventListener('click', () => {
    const entry = item.dataset.entry
    if (entry === 'dialog' || entry === 'lan') {
      activateView(entry)
      return
    }
    toast(`${item.textContent ?? entry}：后置阶段交付`)
  })
}

for (const radio of document.querySelectorAll('input[name="lan-mode"]')) {
  radio.addEventListener('change', syncLanMode)
}

els.lanForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  if (!api?.lan) {
    showLanError('联阵接口未就绪')
    return
  }
  showLanError('')
  els.lanOpen.disabled = true
  try {
    const result = await api.lan.open(lanOptions())
    if (result?.ok === false) {
      showLanError(result.error?.message ?? '开阵失败')
      return
    }
    renderLan(result)
    toast('已开阵')
  } catch (error) {
    showLanError(error.message)
  } finally {
    els.lanOpen.disabled = lanSnapshot?.state !== 'standalone'
  }
})

els.lanRefresh.addEventListener('click', () => refreshLan())

els.lanDisband.addEventListener('click', async () => {
  if (!api?.lan) return
  if (!window.confirm('确定散阵？所有仙徒会回到单机。')) return
  try {
    renderLan(await api.lan.disband())
    toast('已散阵')
  } catch (error) {
    toast(`散阵失败：${error.message}`)
  }
})

els.lanMembers.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-kick]')
  if (!button || !api?.lan) return
  button.disabled = true
  try {
    const result = await api.lan.kick(button.dataset.kick)
    if (result?.status !== 200) toast(result?.error?.message ?? '移出失败')
    else toast('已移出')
    await refreshLan()
  } catch (error) {
    toast(`移出失败：${error.message}`)
    await refreshLan()
  }
})

// 双击顶栏空白处不应触发导航；hiddenInset 下系统已处理缩放，这里只挡住意外选择
els.chrome.addEventListener('dblclick', (event) => {
  if (event.target === els.chrome) event.preventDefault()
})

bootstrap().catch((error) => {
  if (currentView !== 'lan') showPanel('failed')
  els.failedMessage.textContent = `壳初始化失败：${error.message}`
})
