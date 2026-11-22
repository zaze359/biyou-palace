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
 * 输出 macOS 友好的 RGBA 图标：透明背景 + 圆角遮罩 + 内容内缩。
 * 满幅不透明方图在 Dock（app.dock.setIcon）里会显示成直角大方块、比系统
 * 图标大一圈；加 alpha 与内边距后 macOS 才能按应用图标规范呈现。
 *
 * 参数：<源路径> <输出 png> <边长> [inset] [radius]
 *   inset  : 内容四周留白比例（默认 0.10）
 *   radius : 圆角半径比例（默认 0.22，接近 macOS squircle）
 */
const { app, BrowserWindow } = require('electron')
const { readFileSync, writeFileSync } = require('node:fs')
const { extname } = require('node:path')

const [srcPath, outPath, sizeArg, insetArg, radiusArg] = process.argv.slice(2)
const size = Number(sizeArg || 1024)
const inset = Number(insetArg ?? 0.1)
const radius = Number(radiusArg ?? 0.22)

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
    body = `<img src="data:image/png;base64,${b64}" width="100%" height="100%" style="display:block">`
  } else {
    const svg = readFileSync(srcPath, 'utf8').replace('<svg', '<svg width="100%" height="100%"')
    body = svg
  }

  const html = `<!doctype html><html><head><style>
    html,body{margin:0;padding:0;width:100%;height:100%;background:transparent;overflow:hidden}
    #frame{
      position:absolute;
      left:${inset * 100}%; top:${inset * 100}%;
      width:${(1 - 2 * inset) * 100}%; height:${(1 - 2 * inset) * 100}%;
      border-radius:${radius * 100}%;
      overflow:hidden;
      -webkit-mask-image:-webkit-linear-gradient(#000,#000);
    }
    #frame img,#frame svg{display:block;width:100%;height:100%}
  </style></head><body><div id="frame">${body}</div></body></html>`

  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  await new Promise((resolve) => setTimeout(resolve, 350))

  const shot = await win.webContents.capturePage()
  writeFileSync(outPath, shot.toPNG())
  app.exit(0)
})
