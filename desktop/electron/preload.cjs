/**
 * preload：渲染层唯一的权力边界。
 *
 * sandbox: true + contextIsolation: true，这里的 API 是白名单，不暴露 ipcRenderer、
 * require、fs 等任何原始能力。后续 S3/S5 的主进程能力也按同样方式逐个开口子。
 * 注意：sandbox 下 preload 只能是 CommonJS。
 */
const { contextBridge, ipcRenderer } = require('electron')

const subscribe = (channel, callback) => {
  const handler = (_event, payload) => callback(payload)
  ipcRenderer.on(channel, handler)
  return () => ipcRenderer.off(channel, handler)
}

contextBridge.exposeInMainWorld('biyou', {
  platform: process.platform,

  app: {
    info: () => ipcRenderer.invoke('app:info'),
  },

  dsh: {
    status: () => ipcRenderer.invoke('dsh:status'),
    restart: () => ipcRenderer.invoke('dsh:restart'),
    logTail: () => ipcRenderer.invoke('dsh:log-tail'),
    onStatus: (callback) => subscribe('dsh:status', callback),
  },

  shell: {
    /** 告诉主进程品牌壳顶部占多高，dsh 视图据此让位 */
    setChromeHeight: (height) => ipcRenderer.send('shell:chrome-height', height),
    openLogs: () => ipcRenderer.invoke('shell:open-logs'),
    devtools: () => ipcRenderer.send('shell:devtools'),
    /** 切到联阵页时隐藏 dsh 视图，回对话页再显示 */
    setDshVisible: (visible) => ipcRenderer.send('shell:dsh-visible', visible),
  },

  lan: {
    status: () => ipcRenderer.invoke('lan:status'),
    /** options: { name, mode: 'open'|'closed', passcode, capacity } */
    open: (options) => ipcRenderer.invoke('lan:open', options),
    disband: () => ipcRenderer.invoke('lan:disband'),
    kick: (memberId) => ipcRenderer.invoke('lan:kick', memberId),
    onStatus: (callback) => subscribe('lan:status', callback),
  },
})
