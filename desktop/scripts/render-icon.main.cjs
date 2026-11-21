/**
 * 图标渲染的 Electron 侧入口（由 scripts/render-icon.mjs 拉起）。
 *
 * 为什么走 Electron 而不是别的转换器：
 *   - 本机没有 rsvg / ImageMagick，`qlmanage` 在受限环境会 sandbox 失败；
 *   - Electron 本来就是这个项目的依赖，用它离屏渲染不新增任何东西。
 *
 * 支持两种事实源：
 *   - PNG（默认，写实青萍剑 logo）：读成 data URL，用 <img> 铺满画布；
 *   - SVG（--geo 几何副版）：内联进文档后截图。
 * data: 页面里引 file:// 会被拦掉，所以两种源都内联成 data URL / 文本。
 *
 * 参数：<源路径> <输出 png> <边长>
 */
const { app, BrowserWindow } = require('electron')
const { readFileSync, writeFileSync } = require('node:fs')
const { extname } = require('node:path')

const [srcPath, outPath, sizeArg] = process.argv.slice(2)
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

  let body
  if (extname(srcPath).toLowerCase() === '.png') {
    const b64 = readFileSync(srcPath).toString('base64')
    body = `<img src="data:image/png;base64,${b64}" width="${size}" height="${size}" style="display:block">`
  } else {
    const svg = readFileSync(srcPath, 'utf8').replace(
      '<svg',
      `<svg width="${size}" height="${size}"`,
    )
    body = svg
  }
  await win.loadURL(
    'data:text/html;charset=utf-8,' +
      encodeURIComponent(
        `<!doctype html><html><body style="margin:0;background:transparent">${body}</body></html>`,
      ),
  )
  await new Promise((resolve) => setTimeout(resolve, 350))

  const shot = await win.webContents.capturePage()
  writeFileSync(outPath, shot.toPNG())
  app.exit(0)
})
