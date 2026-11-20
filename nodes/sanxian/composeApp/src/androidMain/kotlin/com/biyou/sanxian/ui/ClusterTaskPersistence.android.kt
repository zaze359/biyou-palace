package com.biyou.sanxian.ui

import android.content.Context
import com.biyou.sanxian.SanXianApplication
import com.biyou.sanxian.node.NodeConfigStore

actual fun defaultClusterTaskStore(): ClusterTaskStore = AndroidClusterTaskStore()

private class AndroidClusterTaskStore : ClusterTaskStore {

    private fun context(): Context = SanXianApplication.appContext

    override suspend fun load(): PersistedClusterTask? = runCatching {
        NodeConfigStore.clusterTask(context())?.let {
            PersistedClusterTask(
                taskId = it.taskId,
                lastEventId = it.lastEventId,
                status = it.status,
                answer = it.answer,
            )
        }
    }.getOrNull()

    override suspend fun save(task: PersistedClusterTask) {
        runCatching {
            NodeConfigStore.saveClusterTask(
                context(),
                com.biyou.sanxian.node.StoredClusterTask(
                    taskId = task.taskId,
                    lastEventId = task.lastEventId,
                    status = task.status,
                    answer = task.answer,
                ),
            )
        }
    }

    override suspend fun clear() {
        runCatching { NodeConfigStore.clearClusterTask(context()) }
    }
}
