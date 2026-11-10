package com.biyou.sanxian.ui

import androidx.compose.runtime.Composable

/**
 * 「导入本地 GGUF」入口（commonMain 契约）。
 *
 * androidMain 用 SAF（ActivityResultContracts.OpenDocument）实现：选文件 → 后台复制到
 * [getModelsDirectory] → 回报进度/结果，完成后回调 [onImported] 供调用方刷新列表。
 */
@Composable
expect fun ImportModelButton(onImported: () -> Unit)
