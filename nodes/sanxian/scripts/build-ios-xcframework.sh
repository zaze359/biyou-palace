#!/usr/bin/env bash
#
# build-ios-xcframework.sh — 交叉编译 llama.cpp 为 iOS xcframework（Phase 2.1）
#
# 产物：nativelib/ios/llama.xcframework
#   - ios-arm64                      真机（arm64，Metal 加速）
#   - ios-arm64_x86_64-simulator     模拟器（arm64 + x86_64 通用切片）
#
# 前置：
#   - macOS + Xcode（xcodebuild / xcrun 可用）
#   - CMake >= 3.22
#   - iOS SDK（随 Xcode 提供）
#
# 设计要点（与 Android nativelib 对齐）：
#   - Release 构建（推理性能）
#   - 静态链接（BUILD_SHARED_LIBS=OFF / GGML_STATIC=ON），打包为单一 libllama.a
#   - 关闭 OpenMP（iOS 工具链不带，避免链接 -fopenmp 失败）
#   - 开启 Metal（GGML_METAL=ON），Metal 库由 GGML_METAL_EMBED_LIBRARY 默认内嵌进
#     静态库，xcframework 无需单独携带 .metallib
#   - 关闭 tests / tools / examples / server / common / mtmd / app
#
# 用法：
#   ./scripts/build-ios-xcframework.sh            # 构建 device + simulator
#   ./scripts/build-ios-xcframework.sh --clean    # 清理中间产物后重建
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LLAMA_DIR="$ROOT_DIR/nativelib/src/main/cpp/third_party/llama.cpp"
OUT_DIR="$ROOT_DIR/nativelib/ios"
DEVICE_DIR="$OUT_DIR/build-device"
SIM_DIR="$OUT_DIR/build-sim"
STAGE_DIR="$OUT_DIR/staging"
DEPLOY_TARGET="15.0"

die() { echo "❌ $*" >&2; exit 1; }

# —— 环境探测（无环境则明确失败，绝不静默产出残缺产物）——
command -v xcodebuild >/dev/null 2>&1 || die "未找到 xcodebuild，请先安装 Xcode。"
command -v xcrun      >/dev/null 2>&1 || die "未找到 xcrun（Xcode 命令行工具）。"
command -v cmake      >/dev/null 2>&1 || die "未找到 cmake（需 >= 3.22）。"
[ -d "$LLAMA_DIR" ] || die "未找到 llama.cpp 源码目录：$LLAMA_DIR"

IPHONEOS_SDK="$(xcrun --sdk iphoneos --show-sdk-path)"       || die "未找到 iPhoneOS SDK。"
SIM_SDK="$(xcrun --sdk iphonesimulator --show-sdk-path)"     || die "未找到 iPhoneSimulator SDK。"
NCPU="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

echo "📦 iPhoneOS SDK      : $IPHONEOS_SDK"
echo "📦 iPhoneSimulator SDK: $SIM_SDK"
echo "📦 部署目标           : iOS $DEPLOY_TARGET"
echo "📦 并行作业数         : $NCPU"

if [ "${1:-}" = "--clean" ]; then
  echo "🧹 清理 $OUT_DIR"
  rm -rf "$OUT_DIR"
fi
mkdir -p "$STAGE_DIR/device" "$STAGE_DIR/sim"

# —— 配置 + 编译单个切片 ——
configure_and_build() {
  local sdk="$1" arch="$2" build_dir="$3" label="$4"
  echo ""
  echo "🔧 配置 $label (sysroot=$sdk, arch=$arch)"
  cmake -S "$LLAMA_DIR" -B "$build_dir" \
    -G "Unix Makefiles" \
    -DCMAKE_SYSTEM_NAME=iOS \
    -DCMAKE_OSX_SYSROOT="$sdk" \
    -DCMAKE_OSX_ARCHITECTURES="$arch" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$DEPLOY_TARGET" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_STATIC=ON \
    -DGGML_OPENMP=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_BUILD_TESTS=OFF \
    -DGGML_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_TOOLS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=OFF \
    -DLLAMA_BUILD_COMMON=OFF \
    -DLLAMA_BUILD_MTMD=OFF \
    -DLLAMA_BUILD_APP=OFF

  echo "🏗 编译 $label"
  cmake --build "$build_dir" -j "$NCPU"
}

configure_and_build "$IPHONEOS_SDK" "arm64"            "$DEVICE_DIR" "device(arm64)"
configure_and_build "$SIM_SDK"      "arm64;x86_64"     "$SIM_DIR"     "simulator(arm64+x86_64)"

# —— 合并各切片内的静态库为单一 libllama.a（便于 KMP/链接引用）——
combine_libs() {
  local src_dir="$1" dst="$2" label="$3"
  local libs=()
  for f in "$src_dir"/libllama.a "$src_dir"/libggml"".a "$src_dir"/libggml-*.a; do
    [ -f "$f" ] && libs+=("$f")
  done
  [ "${#libs[@]}" -gt 0 ] || die "$label 未找到任何 llama/ggml 静态库（编译是否成功？）"
  echo "🔗 合并 $label: ${libs[*]}"
  libtool -static -o "$dst" "${libs[@]}"
}

combine_libs "$DEVICE_DIR" "$STAGE_DIR/device/libllama.a" "device"
combine_libs "$SIM_DIR"     "$STAGE_DIR/sim/libllama.a"    "simulator"

# —— 打包 xcframework ——
echo ""
echo "📦 生成 llama.xcframework"
rm -rf "$OUT_DIR/llama.xcframework"
xcodebuild -create-xcframework \
  -library "$STAGE_DIR/device/libllama.a" \
  -library "$STAGE_DIR/sim/libllama.a" \
  -output "$OUT_DIR/llama.xcframework"

echo ""
echo "✅ 完成：$OUT_DIR/llama.xcframework"
find "$OUT_DIR/llama.xcframework" -name "*.a" -exec ls -lh {} \;
