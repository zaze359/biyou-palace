/**
 * 图标渲染的 Electron 侧入口（由 scripts/render-icon.mjs 拉起）。
 *
 * 为什么走 Electron 而不是别的转换器：
 *   - 本机没有 rsvg / ImageMagick，`qlmanage` 在受限环境会 sandbox 失败；
 *   - Electron 本来就是这个项目的依赖，用它离屏渲染 SVG 不新增任何东西。
 *
 * 参数：<svg 路径> <输出 png> <边长>
 */
const { app, BrowserWindow } = require('electron')
const { readFileSync, writeFileSync } = require('node:fs')

const [svgPath, outPath, sizeArg] = process.argv.slice(2)
const size = Number(sizeArg || 1024)

app.disableHardwareAcceleration()
app.commandLine.appendSwitch('no-sandbox')

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: size,
    height: size,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    webPreferences: { offscreen: true, contextIsolation: true, sandbox: true, nodeIntegration: false },
  })

  // SVG 直接内联进文档：data: 页面里引 file:// 会被拦掉，<img> 会渲染成裂图。
  const svg = readFileSync(svgPath, 'utf8').replace(
    '<svg',
    `<svg width="${size}" height="${size}"`,
  )
  await win.loadURL(
    'data:text/html;charset=utf-8,' +
      encodeURIComponent(
        `<!doctype html><html><body style="margin:0;background:transparent">${svg}</body></html>`,
      ),
  )
  await new Promise((resolve) => setTimeout(resolve, 350))

  const shot = await win.webContents.capturePage()
  writeFileSync(outPath, shot.toPNG())
  app.exit(0)
})
