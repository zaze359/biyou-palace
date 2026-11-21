/**
 * 碧游宫插件 · node half。
 *
 * 这一半目前是空的：它的存在是为了让 `@biyou/dsh-plugin` 成为一条正常的 Cordis row，
 * 从而进 Loader、进 profile 组合。浏览器那一半走 `exports["./client"]`，由
 * `@deepseek-ai/dsh-client-modules` 扫描 `dsh.client` 声明后放进 `window.__DSH_BOOT__`。
 *
 * 将来需要给 browser half 提供数据时（观星台的阵图状态、功德殿的任务板），
 * 在这里注册 service，浏览器侧通过 remote 调用拿到——不给渲染进程开 Electron IPC。
 */

/** Host 插件体：暂无 host 侧行为。 */
export function apply() {}
