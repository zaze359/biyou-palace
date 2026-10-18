cmake_minimum_required(VERSION 3.22.1)
project(llama_jni CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(LLAMA_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/llama.cpp)

# ---------------------------------------------------------------------------
# llama.cpp 构建选项：只构建核心库，关闭所有可执行程序/工具/测试/服务端
# ---------------------------------------------------------------------------
set(LLAMA_BUILD_COMMON             OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS              OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS              OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES           OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER             OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP                OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_MTMD               OFF CACHE BOOL "" FORCE)
set(LLAMA_ALL_WARNINGS             OFF CACHE BOOL "" FORCE)
set(LLAMA_ALL_WARNINGS_3RD_PARTY   OFF CACHE BOOL "" FORCE)
# 使用 OpenSSL 会引入额外的链接依赖，端侧推理用不到，显式关闭
set(LLAMA_OPENSSL                  OFF CACHE BOOL "" FORCE)

# ---------------------------------------------------------------------------
# ggml 构建选项
# ---------------------------------------------------------------------------
# 把 llama/ggml 全部静态链接进单一的 libllama_jni.so，避免 App 需要携带多个 .so
set(BUILD_SHARED_LIBS    OFF CACHE BOOL "" FORCE)
set(GGML_STATIC          ON  CACHE BOOL "" FORCE)
# NDK 不带 OpenMP，必须关闭，否则链接期找不到 -fopenmp
set(GGML_OPENMP          OFF CACHE BOOL "" FORCE)
set(GGML_BUILD_TESTS     OFF CACHE BOOL "" FORCE)
set(GGML_BUILD_EXAMPLES  OFF CACHE BOOL "" FORCE)
# CPU 端加速内核（ARM NEON / x86 AVX 的量化算子实现），保持开启
set(GGML_LLAMAFILE       ON  CACHE BOOL "" FORCE)

# TODO(Phase 3.3): 按需开启 GPU 后端，例如
#   -DGGML_VULKAN=ON   (Android Vulkan)
#   -DGGML_OPENCL=ON   (Android OpenCL)

add_subdirectory(${LLAMA_DIR} ${CMAKE_CURRENT_BINARY_DIR}/llama_build)

add_library(llama_jni SHARED jni_bridge.cpp)

target_include_directories(llama_jni PRIVATE
    ${LLAMA_DIR}/include
    ${LLAMA_DIR}/ggml/include
)

find_library(log-lib log)

# llama 为静态库，其 PRIVATE 依赖（ggml / ggml-base / ggml-cpu 等）会由 CMake
# 自动追加到本共享库的链接行，因此这里只需显式链接 llama 本身。
target_link_libraries(llama_jni
    llama
    ${log-lib}
)
