package com.biyou.sanxian.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.biyou.sanxian.llm.DownloadState
import com.biyou.sanxian.llm.RemoteModel
import com.biyou.sanxian.llm.createModelDownloadManager
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * 模型管理界面 ViewModel（对接 #12 的 [com.biyou.sanxian.llm.ModelDownloadManager]）。
 *
 * 职责：
 *  - 维护内置 HuggingFace GGUF 模型目录（[catalog]）；
 *  - 按 [RemoteModel.fileName] 跟踪每个模型的下载态（[downloads]）；
 *  - 扫描 [getModelsDirectory] 生成「已下载」列表（[localModels]），忽略 `.part`；
 *  - 删除本地模型、刷新存储占用（[storage]）。
 */
data class CatalogEntry(
    val repoId: String,
    val fileName: String,
    val displayName: String,
    val sizeBytes: Long?,
    val branch: String = "main",
)

data class LocalModel(
    val fileName: String,
    val path: String,
    val sizeBytes: Long,
)

data class ModelManagerUiState(
    val catalog: List<CatalogEntry> = emptyList(),
    val downloads: Map<String, DownloadState> = emptyMap(),
    val localModels: List<LocalModel> = emptyList(),
    val storage: ModelsStorageInfo = ModelsStorageInfo(freeBytes = 0, totalBytes = 0),
    val modelsDir: String = "",
    val switching: Boolean = false,
    val statusMessage: String? = null,
)

class ModelManagerViewModel : ViewModel() {

    private val downloader = createModelDownloadManager()
    private val activeJobs = mutableMapOf<String, Job>()

    private val _state = MutableStateFlow(ModelManagerUiState())
    val state: StateFlow<ModelManagerUiState> = _state.asStateFlow()

    init {
        _state.update { it.copy(catalog = CATALOG) }
        refreshLocal()
    }

    /** 开始/恢复下载某个目录项；若正在下载则视为「取消」。 */
    fun toggleDownload(entry: CatalogEntry) {
        val key = entry.fileName
        val running = activeJobs[key]?.isActive == true
        if (running) {
            activeJobs[key]?.cancel()
            activeJobs.remove(key)
            _state.update { it.copy(downloads = it.downloads - key) }
            return
        }
        val localPath = "${getModelsDirectory()}/$key"
        val remote = RemoteModel.huggingFace(
            repoId = entry.repoId,
            fileName = entry.fileName,
            localPath = localPath,
            branch = entry.branch,
        )
        val job = viewModelScope.launch {
            downloader.download(remote).collect { ds ->
                _state.update { it.copy(downloads = it.downloads + (key to ds)) }
                if (ds is DownloadState.Completed) {
                    activeJobs.remove(key)
                    refreshLocal()
                } else if (ds is DownloadState.Error) {
                    activeJobs.remove(key)
                }
            }
        }
        activeJobs[key] = job
    }

    /** 删除本地模型文件并刷新列表与存储。 */
    fun deleteLocal(model: LocalModel) {
        deleteModelFile(model.path)
        refreshLocal()
    }

    /**
     * 受控切换节点 chat 模型：写 DataStore 并重启前台服务（由服务重新加载模型），
     * 不直接操作引擎句柄，因此不会出现「聊天里换模型把对外服务的模型关掉」。
     */
    fun useAsNodeModel(fileName: String) {
        if (_state.value.switching) return
        _state.update {
            it.copy(switching = true, statusMessage = "正在切换节点模型：$fileName…")
        }
        viewModelScope.launch {
            try {
                val restarted = activateNodeChatModel(fileName)
                _state.update {
                    it.copy(
                        switching = false,
                        statusMessage = if (restarted) {
                            "已设为节点 chat 模型，服务正在重启（模型加载需数秒）"
                        } else {
                            "已保存模型配置，但服务重启请求被系统拒绝；请回到首页重试"
                        },
                    )
                }
            } catch (e: Throwable) {
                _state.update {
                    it.copy(switching = false, statusMessage = "切换失败：${e.message}")
                }
            }
        }
    }

    /** 扫描模型目录：已下载列表（忽略 .part）+ 存储占用。 */
    fun refreshLocal() {
        val dir = getModelsDirectory()
        val local = scanLocalModels()
        val info = runCatching { getModelsStorageInfo() }.getOrDefault(ModelsStorageInfo(0, 0))
        _state.update { it.copy(localModels = local, storage = info, modelsDir = dir) }
    }

    override fun onCleared() {
        super.onCleared()
        activeJobs.values.forEach { it.cancel() }
        activeJobs.clear()
    }
}

/**
 * 内置模型目录（示例清单，URL 经 [RemoteModel.huggingFace] 构造）。
 * 具体大小以 HuggingFace 为准；sizeBytes 用于 UI 预估，缺失时进度退化为不确定态。
 */
private val CATALOG = listOf(
    CatalogEntry(
        repoId = "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        fileName = "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        displayName = "Qwen2.5 0.5B Instruct（Q4_K_M）",
        sizeBytes = 526_900_000L,
    ),
    CatalogEntry(
        repoId = "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        fileName = "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        displayName = "Qwen2.5 1.5B Instruct（Q4_K_M）",
        sizeBytes = 1_210_000_000L,
    ),
    CatalogEntry(
        repoId = "unsloth/Llama-3.2-1B-Instruct-GGUF",
        fileName = "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        displayName = "Llama 3.2 1B Instruct（Q4_K_M）",
        sizeBytes = 887_000_000L,
    ),
    CatalogEntry(
        repoId = "HuggingFaceTB/SmolLM2-360M-Instruct-GGUF",
        fileName = "smollm2-360m-instruct-q4_k_m.gguf",
        displayName = "SmolLM2 360M Instruct（Q4_K_M）",
        sizeBytes = 251_000_000L,
    ),
)
