#!/usr/bin/env bash
#
# 小手机（smallphone）Release 构建脚本
#
# 职责：
#   1. 重建 nativelib（libllama_jni.so，含 llama.cpp/ggml）
#   2. 打包 :composeApp:assembleRelease
#   3. 把产物统一收集到工作区根目录的 outputs/
#
# 用法：
#   ./scripts/build-release.sh              # 增量构建（默认）
#   ./scripts/build-release.sh --clean      # 先 clean 再全量构建
#   ./scripts/build-release.sh --no-so      # 只收集 APK，不额外导出 .so
#   ./scripts/build-release.sh --help       # 查看帮助
#
# 可通过环境变量覆盖工具链路径：JAVA_HOME / ANDROID_SDK_ROOT / ANDROID_NDK_ROOT
#
set -euo pipefail

# ---------------- 路径推导（以脚本自身位置为锚，任意目录调用均可） ----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
OUTPUTS_DIR="$WORKSPACE_ROOT/outputs"

# ---------------- 构建环境 ----------------
export JAVA_HOME="${JAVA_HOME:-/usr/local/Cellar/openjdk@17/17.0.20/libexec/openjdk.jdk/Contents/Home}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
export ANDROID_NDK_ROOT="${ANDROID_NDK_ROOT:-$ANDROID_SDK_ROOT/ndk/27.0.12077973}"

APK_REL="composeApp/build/outputs/apk/release/composeApp-release-unsigned.apk"
GRADLEW="$PROJECT_ROOT/gradlew"

# ---------------- 参数解析 ----------------
DO_CLEAN="false"
COLLECT_SO="true"
for arg in "$@"; do
  case "$arg" in
    --clean)   DO_CLEAN="true" ;;
    --no-so)   COLLECT_SO="false" ;;
    -h|--help)
      sed -n '2,17p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "未知参数: ${arg}（用 --help 查看用法）" >&2
      exit 1
      ;;
  esac
done

# ---------------- 工具函数 ----------------
info()  { printf '\033[36m[build]\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
die()   { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------- 前置检查 ----------------
[[ -f "$GRADLEW" ]]      || die "未找到 gradlew: $GRADLEW"
[[ -x "$GRADLEW" ]]      || chmod +x "$GRADLEW"
[[ -d "$JAVA_HOME" ]]    || die "JAVA_HOME 不存在: $JAVA_HOME"
[[ -d "$ANDROID_SDK_ROOT" ]] || die "ANDROID_SDK_ROOT 不存在: $ANDROID_SDK_ROOT"

info "项目根    : $PROJECT_ROOT"
info "工作区根  : $WORKSPACE_ROOT"
info "产物目录  : $OUTPUTS_DIR"
info "JDK       : $JAVA_HOME"
info "Android SDK: $ANDROID_SDK_ROOT"

# ---------------- 1. 构建 ----------------
cd "$PROJECT_ROOT"

if [[ "$DO_CLEAN" == "true" ]]; then
  info "执行 clean（全量重建，耗时较长）..."
  ./gradlew clean --no-daemon || die "clean 失败"
fi

info "开始构建 :composeApp:assembleRelease ..."
BUILD_LOG="$(mktemp -t smallphone-build)"
set +e
./gradlew :composeApp:assembleRelease --no-daemon 2>&1 | tee "$BUILD_LOG"
BUILD_RC="${PIPESTATUS[0]}"
set -e
[[ "$BUILD_RC" -eq 0 ]] || die "Gradle 构建失败（退出码 ${BUILD_RC}），日志: $BUILD_LOG"

grep -q "BUILD SUCCESSFUL" "$BUILD_LOG" || die "未检测到 BUILD SUCCESSFUL，日志: $BUILD_LOG"
ok "构建成功"

# ---------------- 2. 收集产物 ----------------
APK_SRC="$PROJECT_ROOT/$APK_REL"
[[ -f "$APK_SRC" ]] || die "未找到 APK 产物: $APK_SRC"

mkdir -p "$OUTPUTS_DIR"

# 时间戳由系统 date 生成（勿手工推算）
STAMP="$(date +%Y%m%d-%H%M%S)"
APK_DST="$OUTPUTS_DIR/smallphone-release-$STAMP.apk"
APK_LATEST="$OUTPUTS_DIR/smallphone-release-latest.apk"

cp "$APK_SRC" "$APK_DST"
cp "$APK_DST" "$APK_LATEST"
ok "APK  -> $APK_DST"
ok "APK  -> ${APK_LATEST}（最新副本）"

# APK 内 ABI 自检
info "APK 内原生库："
unzip -l "$APK_DST" 2>/dev/null | grep -E "lib/(arm64-v8a|x86_64|armeabi-v7a)/libllama_jni\.so" || info "  （未匹配到 libllama_jni.so，请检查 abiFilters 配置）"

# ---------------- 3. 导出 .so（便于单独分发/核对） ----------------
if [[ "$COLLECT_SO" == "true" ]]; then
  NATIVE_OUT="$OUTPUTS_DIR/native"
  SO_COUNT=0
  for abi in arm64-v8a x86_64; do
    SO_SRC="$(find "$PROJECT_ROOT/nativelib/build" -path "*$abi*libllama_jni.so" -type f 2>/dev/null | head -1)"
    if [[ -n "$SO_SRC" && -f "$SO_SRC" ]]; then
      mkdir -p "$NATIVE_OUT/$abi"
      cp "$SO_SRC" "$NATIVE_OUT/$abi/libllama_jni.so"
      ok "SO   -> $NATIVE_OUT/$abi/libllama_jni.so"
      SO_COUNT=$((SO_COUNT + 1))
    fi
  done
  [[ "$SO_COUNT" -gt 0 ]] || info "未导出 .so（nativelib 构建产物未就绪，可先跑一次完整构建）"
fi

# ---------------- 4. 构建信息清单 ----------------
INFO_FILE="$OUTPUTS_DIR/build-info.txt"
{
  echo "smallphone Release 构建信息"
  echo "=========================="
  echo "构建时间 : $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "构建方式 : $0 $*"
  echo "项目根   : $PROJECT_ROOT"
  echo "JDK      : $JAVA_HOME"
  echo "SDK      : $ANDROID_SDK_ROOT"
  echo "NDK      : $ANDROID_NDK_ROOT"
  echo "APK      : $(basename "$APK_DST")"
  echo "APK 大小 : $(du -h "$APK_DST" | cut -f1 | tr -d ' ')"
  echo "Gradle 结果: BUILD SUCCESSFUL"
} > "$INFO_FILE"
ok "清单 -> $INFO_FILE"

echo
ok "全部完成，产物位于: $OUTPUTS_DIR"
ls -lh "$OUTPUTS_DIR"
