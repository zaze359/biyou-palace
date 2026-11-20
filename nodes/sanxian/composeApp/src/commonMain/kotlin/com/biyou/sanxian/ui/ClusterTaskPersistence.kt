package com.biyou.sanxian.ui

/**
 * 断线重连所需的最小集群任务状态：只存 `task_id`、事件游标、状态与最终答案摘要，
 * 完整事件流留在阵图，不在本地复制。
 */
data class PersistedClusterTask(
    val taskId: String,
    val lastEventId: String?,
    val status: String,
    val answer: String? = null,
)

interface ClusterTaskStore {
    suspend fun load(): PersistedClusterTask?
    suspend fun save(task: PersistedClusterTask)
    suspend fun clear()
}

/** 设备端实现（DataStore）；JVM 单测可注入内存实现。 */
expect fun defaultClusterTaskStore(): ClusterTaskStore
