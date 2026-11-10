package com.biyou.sanxian

import android.app.Application
import com.biyou.sanxian.llm.LlmEngineProvider

/**
 * Application 入口。
 * 后续 Phase 1.5/1.7 会把 [androidLlmEngine] 与 Application 生命周期绑定，
 * 保证模型在应用存活期间只加载一次、统一释放。
 */
class SanXianApplication : Application() {

    override fun onCreate() {
        super.onCreate()
        appContext = this
        // DI(Phase 1.7)：应用启动即完成依赖初始化——创建进程级单例引擎，
        // 使模型在应用存活期间只加载一次，Activity / ViewModel 重建不重复加载。
        LlmEngineProvider.init()
        // 散仙节点前台服务改由 MainActivity（前台态）拉起，避免 Android 12+
        // 在 Application.onCreate 后台启动前台服务被拒（ForegroundServiceStartNotAllowedException）。
        // 服务被系统停掉后没有任何机会自救，这里注册周期探活兜底（失败只会提醒用户，
        // 能力边界见 NodeWatchdogWorker 注释）。
        NodeWatchdogWorker.schedule(this)
    }

    companion object {
        /** 应用上下文单例，供 shared 端 expect/actual（如 [com.biyou.sanxian.llm.getModelsDirectory]）取存储目录 */
        lateinit var appContext: SanXianApplication
            private set
    }
}
