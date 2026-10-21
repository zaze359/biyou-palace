package com.biyou.sanxian.llm

/**
 * 进程级 LLM 引擎单例（KMP commonMain）。
 *
 * 保证同一进程内只创建一个引擎、模型仅加载一次；Activity / ViewModel 重建
 * （旋转、进程恢复、多窗口、不保留活动等）不触发重复加载。
 *
 * 生命周期约定：
 *  - 应用启动时由 [com.biyou.sanxian.SanXianApplication.onCreate] 调用 [init] 完成 DI 初始化；
 *  - 业务层统一经 [engine] 取用，不要自行 `createLlmEngine`；
 *  - 卸载模型请调用 [LlmEngine.close]（释放模型、保留单例对象），不要销毁单例本身。
 *
 * 放在 commonMain 是为了 Android 与后续 iOS 共享同一生命周期抽象——
 * 各端只需提供 `createLlmEngine` 的 actual 实现，即可自动获得进程级单例行为。
 */
object LlmEngineProvider {

    /**
     * 惰性创建：首次访问时构造平台引擎。
     * Android 实现为 [AndroidLlmEngine]——构造廉价（仅存 config，不加载模型、不触发
     * `System.loadLibrary`），真正的模型加载由 [LlmEngine.loadModel] 显式触发。
     * `by lazy` 默认 SYNCHRONIZED，线程安全。
     */
    val engine: LlmEngine by lazy { createLlmEngine(LlmConfig()) }

    /** 幂等触发单例创建；供 Application 完成 DI 初始化。 */
    fun init(): LlmEngine = engine
}
