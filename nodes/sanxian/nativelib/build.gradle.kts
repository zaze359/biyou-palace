plugins {
    alias(libs.plugins.androidLibrary)
}

android {
    namespace = "com.smallphone.nativelib"
    compileSdk = 35

    defaultConfig {
        minSdk = 24

        externalNativeBuild {
            cmake {
                arguments += "-DANDROID_STL=c++_static"
                // AGP 对 debug 变体会传 CMAKE_BUILD_TYPE=Debug，但 llama.cpp 在未开启优化时
                // 推理速度会慢一个数量级（实测可达 10x 以上），端侧大模型在此模式下基本不可用。
                // 因此原生库始终以 Release 构建；Java/Kotlin 与 Compose 仍走 debug 变体。
                arguments += "-DCMAKE_BUILD_TYPE=Release"
                // 关闭所有非必需构建项，llama.cpp 只编译 llama + ggml，大幅缩短编译时间
                arguments += "-DLLAMA_BUILD_TESTS=OFF"
                arguments += "-DLLAMA_BUILD_EXAMPLES=OFF"
                arguments += "-DLLAMA_BUILD_SERVER=OFF"
                arguments += "-DLLAMA_BUILD_TOOLS=OFF"
                // Android NDK 不带 OpenMP，必须关闭
                arguments += "-DGGML_OPENMP=OFF"
                // 静态链接：把 llama/ggml 全部打进单个 libllama_jni.so，避免打包多个 .so
                arguments += "-DBUILD_SHARED_LIBS=OFF"
                arguments += "-DGGML_STATIC=ON"
                // LTO 会显著拖慢编译，Phase 3 性能调优阶段再打开
                arguments += "-DGGML_LTO=OFF"
                cppFlags += "-std=c++17"
                // TODO(Phase 3): GPU 后端开关，例如 -DGGML_VULKAN=ON（需 Vulkan 头文件与设备支持）
            }
        }

        ndk {
            // arm64-v8a：真机主流（绝大多数现代 Android）
            // x86_64：模拟器验证用
            // 暂不含 armeabi-v7a：32 位 ARM 对 llama.cpp 支持有限，且会多一轮完整编译
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
