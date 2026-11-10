#!/usr/bin/env bash
# -----------------------------------------------------------------------------
#  locale 守护：bash 在「UTF-8 类 locale（含 C.UTF-8）」下，遇到「非 ASCII 字节字符
#  （→ （） ⚠ ✗ 等）紧邻 $VAR」的 echo 行，会把 $VAR 误判为未定义变量（set -u 报
#  unbound variable）。C.UTF-8 同样带 UTF-8 编码也会触发，故【无条件】切到纯 C locale，
#  让 bash 始终按单字节解析。脚本输出的中文仍以 UTF-8 字节原样写出，UTF-8 终端正常显示。
# -----------------------------------------------------------------------------
export LC_ALL=C
export LANG=C
# =============================================================================
#  散仙节点 · 一键编译 / 安装 / 启动脚本
#  (BiYouPalace · sanxian Android node agent)
#
#  本工程已【自包含】：散仙 底座 + 散仙增量源码 + Ktor/前台服务补丁，
#  已全部并入 nodes/sanxian 本身（不再依赖外部 散仙 仓库，也无运行时 patch 步骤）。
#  本脚本仅负责：补 local.properties → 编译 → 安装 → 拉起节点 HTTP 服务 → 探活。
#
#  前置（本机/Mac/CI 必须）：
#   - JDK 17
#   - Android SDK 35 + Build-Tools 35.0.0 + NDK 27.x + CMake 3.22.1
#   - adb 在 PATH 或 $ANDROID_HOME/platform-tools/adb
#   - ANDROID_HOME 指向 SDK（用于生成 local.properties）
#   - Gradle 由工程自带 wrapper 驱动（无需全局安装）
#
#  embeddings：/v1/embeddings 已落地真实向量化（Plan A 独立 embedding 句柄）。
#     需在 sanxian.json 配置 embedding_model 并推送对应 GGUF（见 --push-model），
#     否则该端点返回 503（能力已声明但后端未就绪；未声明能力时为 501）。
#  ⚠️ 模型：sanxian.json 只写文件名（如 Qwen3-0.6B-Q8_0.gguf），运行时解析到 App 私有目录
#     /sdcard/Android/data/com.biyou.sanxian/files/models/（也可在 App「模型管理」用 SAF 导入）。
#     需自行 adb push（见 --push-model），否则 health=loading、chat 503。
#  ⚠️ API 级别：NodeServer 用 java.time，需 API 26+；V1 面向现代设备。
#
#  用法：
#   ./build_and_run_sanxian.sh                 # 原位编译→安装→启动
#   ./build_and_run_sanxian.sh --no-install    # 只编译
#   ./build_and_run_sanxian.sh --no-build      # 跳过编译（用已有 APK）
#   ./build_and_run_sanxian.sh --clean         # 清理构建产物（gradlew clean）
#   ./build_and_run_sanxian.sh --push-model ~/x.gguf
# =============================================================================
set -euo pipefail

# ----------------------------- 参数解析 -----------------------------
DO_INSTALL=1
DO_BUILD=1
DO_CLEAN=0
PUSH_MODEL=""
for arg in "$@"; do
  case "$arg" in
    --no-install) DO_INSTALL=0 ;;
    --no-build)   DO_BUILD=0 ;;
    --clean)      DO_CLEAN=1 ;;
    --push-model) echo "用法：--push-model <本地gguf路径>" >&2; exit 2 ;;
    --push-model=*) PUSH_MODEL="${arg#*=}" ;;
    -*) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# ----------------------------- 路径解析 -----------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR"   # 节点工程自身（自包含，原位构建）

echo "==> 路径"
echo "    节点工程目录 : $BUILD_DIR"

# ----------------------------- 前置检查 -----------------------------
echo "==> 前置检查"
if [ ! -f "$BUILD_DIR/gradlew" ]; then
  echo "✗ 未找到 gradlew：$BUILD_DIR/gradlew" >&2
  exit 1
fi

# JDK 17
JAVA_BIN="${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(command -v java 2>/dev/null)" 2>/dev/null)" 2>/dev/null)")}/bin/java"
if [ -z "${JAVA_HOME:-}" ] && command -v java >/dev/null 2>&1; then
  JAVA_BIN="$(command -v java)"
fi
if [ -n "${JAVA_HOME:-}" ]; then JAVA_BIN="$JAVA_HOME/bin/java"; fi
if [ ! -x "$JAVA_BIN" ]; then
  echo "✗ 未找到 java，请设置 JAVA_HOME（指向 JDK 17）。" >&2
  exit 1
fi
JAVA_VER="$("$JAVA_BIN" -version 2>&1 | head -1)"
echo "    java : $JAVA_VER"
if ! echo "$JAVA_VER" | grep -q "17"; then
  echo "✗ 需要 JDK 17；当前不是 17。请设置 JAVA_HOME 指向 JDK 17。" >&2
  exit 1
fi

# adb
ADB=""
if [ -n "${ANDROID_HOME:-}" ] && [ -x "$ANDROID_HOME/platform-tools/adb" ]; then
  ADB="$ANDROID_HOME/platform-tools/adb"
elif command -v adb >/dev/null 2>&1; then
  ADB="$(command -v adb)"
fi
if [ "$DO_INSTALL" -eq 1 ] && [ -z "$ADB" ]; then
  echo "✗ 需要 adb（安装/启动步骤）；请安装 platform-tools 或设置 ANDROID_HOME。" >&2
  exit 1
fi
[ -n "$ADB" ] && echo "    adb  : $ADB"

# NDK 提示（非阻断；缺失会在 cmake 阶段才暴露）
if [ -n "${ANDROID_HOME:-}" ] && [ ! -d "$ANDROID_HOME/ndk" ]; then
  echo "    ⚠ 未检测到 $ANDROID_HOME/ndk —— 原生 llama.cpp 编译需要 NDK，请先安装。" >&2
fi

# ----------------------------- local.properties -----------------------------
if [ ! -f "$BUILD_DIR/local.properties" ]; then
  if [ -n "${ANDROID_HOME:-}" ]; then
    printf 'sdk.dir=%s\n' "$ANDROID_HOME" > "$BUILD_DIR/local.properties"
    echo "==> 生成 local.properties（来自 ANDROID_HOME）"
  else
    echo "✗ 缺少 local.properties 且未设置 ANDROID_HOME，无法定位 SDK。请设置 ANDROID_HOME。" >&2
    exit 1
  fi
fi

# ----------------------------- 可选：清理构建产物 -----------------------------
if [ "$DO_CLEAN" -eq 1 ]; then
  echo "==> --clean：清理构建产物（gradlew clean）"
  ( cd "$BUILD_DIR" && ./gradlew clean --no-daemon ) \
    || rm -rf "$BUILD_DIR/shared/build" "$BUILD_DIR/composeApp/build" \
             "$BUILD_DIR/nativelib/build" "$BUILD_DIR/.gradle"
fi

# ----------------------------- 可选：push 模型 -----------------------------
if [ -n "$PUSH_MODEL" ]; then
  if [ ! -f "$PUSH_MODEL" ]; then
    echo "✗ --push-model 文件不存在：$PUSH_MODEL" >&2
    exit 1
  fi
  MODEL_DIR="/sdcard/Android/data/com.biyou.sanxian/files/models"
  echo "==> push 模型到设备：$MODEL_DIR/$(basename "$PUSH_MODEL")"
  "$ADB" shell mkdir -p "$MODEL_DIR"
  "$ADB" push "$PUSH_MODEL" "$MODEL_DIR/$(basename "$PUSH_MODEL")"
  echo "    ⚠ 若文件名不同于 sanxian.json 的 preload_models，请同步修改 assets/sanxian.json。"
fi

# ----------------------------- 编译 -----------------------------
APK="$BUILD_DIR/composeApp/build/outputs/apk/debug/composeApp-debug.apk"
if [ "$DO_BUILD" -eq 1 ]; then
  echo "==> 编译 :composeApp:assembleDebug（首次含 llama.cpp 原生编译，可能 10+ 分钟）"
  ( cd "$BUILD_DIR" && JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")" ./gradlew :composeApp:assembleDebug --no-daemon --stacktrace )
  if [ ! -f "$APK" ]; then
    echo "✗ 编译未产出 APK：$APK" >&2
    exit 1
  fi
  echo "    APK: $APK"
else
  if [ ! -f "$APK" ]; then
    echo "✗ --no-build 但找不到已有 APK：$APK" >&2
    exit 1
  fi
  echo "==> 跳过编译，使用已有 APK：$APK"
fi

# ----------------------------- 安装 -----------------------------
if [ "$DO_INSTALL" -eq 1 ]; then
  echo "==> 设备安装（覆盖安装）"
  "$ADB" install -r -t "$APK"

  echo "==> 拉起 App（触发 Application.onCreate → 节点前台服务自动启动）"
  "$ADB" shell am start -n com.biyou.sanxian/.MainActivity

  echo "==> 端口转发 + 冒烟探活（/v1/health，5s 后重试）"
  "$ADB" forward tcp:4011 tcp:4011
  # 本地流量不走代理（避免 HTTP_PROXY 把 127.0.0.1 当成远程路径）
  ( sleep 5
    unset HTTP_PROXY HTTPS_PROXY
    export NO_PROXY='*'
    curl -s --noproxy '*' http://127.0.0.1:4011/v1/health \
      && echo "" && echo "    ✅ 节点已就绪（health 见上）。" \
      || echo "    ⚠ 探活失败：确认设备已亮屏/App 在前台，且模型已就位（否则 health=loading）。" )
fi

echo ""
echo "==> 完成。"
echo "   节点工程 : $BUILD_DIR"
echo "   节点地址 : http://127.0.0.1:4011  （已 adb forward；手机端 0.0.0.0:4011）"
echo "   契约端点 : /v1/health  /v1/models  /v1/chat/completions  /v1/embeddings(需配置 embedding_model)"
echo "   停止节点 : adb shell am force-stop com.biyou.sanxian"
echo "   清理构建 : ./build_and_run_sanxian.sh --clean"
