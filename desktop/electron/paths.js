/**
 * 目录布局：桌面端一切可写数据都在 userData 下，不写仓库目录。
 *
 * userData/
 *   config.json         应用配置（S1 只有窗口与 dsh 启动项）
 *   dsh/                DSH_HOME，交给 dsh 运行时
 *     profiles/biyou/   由 runtime/home/profiles/biyou 同步而来
 *   logs/desktop.log    壳自己的日志
 *   logs/dsh.log        dsh 子进程 stdout/stderr
 *
 * S3 会补 tasks.db / artifacts/。
 */
import { homedir, platform } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** desktop/ 的绝对路径（本文件位于 desktop/electron/） */
export const DESKTOP_ROOT = fileURLToPath(new URL('..', import.meta.url))

export function defaultUserDataDir() {
  if (process.env.BIYOU_USER_DATA) return process.env.BIYOU_USER_DATA
  const home = homedir()
  switch (platform()) {
    case 'darwin':
      return join(home, 'Library', 'Application Support', 'biyou-desktop')
    case 'win32':
      return join(process.env.APPDATA ?? join(home, 'AppData', 'Roaming'), 'biyou-desktop')
    default:
      return join(process.env.XDG_CONFIG_HOME ?? join(home, '.config'), 'biyou-desktop')
  }
}

/** 仓库内 dsh profile 模板（只读来源） */
export function profileSourceDir(profile = 'biyou') {
  return join(DESKTOP_ROOT, 'runtime', 'home', 'profiles', profile)
}

/** runtime/ 整体目录，打包时整体拷进 Resources */
export function runtimeSourceRoot() {
  return join(DESKTOP_ROOT, 'runtime')
}

export function layoutPaths(userDataDir) {
  const root = userDataDir
  const dshHome = join(root, 'dsh')
  const logs = join(root, 'logs')
  return {
    root,
    configFile: join(root, 'config.json'),
    dshHome,
    profilesRoot: join(dshHome, 'profiles'),
    logs,
    desktopLog: join(logs, 'desktop.log'),
    dshLog: join(logs, 'dsh.log'),
    smokeDir: join(root, 'smoke'),
  }
}
