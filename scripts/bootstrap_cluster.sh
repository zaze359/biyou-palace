#!/usr/bin/env bash
#
# 碧游宫 · 中枢机初始化
#
# 幂等生成本地运行文件（不覆盖既有配置）：
#   .env                                   中枢机编排变量
#   deploy/newapi/conf/new-api.env         NewAPI 会话/加密密钥
#   monitoring/prometheus/zhentu_token     灵脉抓取阵图指标的 Bearer token
#
# 用法：
#   bash scripts/bootstrap_cluster.sh           只生成/补齐本地文件
#   bash scripts/bootstrap_cluster.sh --check   只检查，不写任何文件
#   bash scripts/bootstrap_cluster.sh --up      生成后执行 docker compose up -d
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
NEWAPI_ENV="$REPO_ROOT/deploy/newapi/conf/new-api.env"
TOKEN_FILE="$REPO_ROOT/monitoring/prometheus/zhentu_token"

MODE="prepare"
UP=0
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --up) UP=1 ;;
    --help|-h) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "[ERR] 未知参数: $arg" >&2; exit 1 ;;
  esac
done

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  else
    echo "[ERR] 需要 openssl 或 python3 生成随机密钥" >&2
    return 1
  fi
}

env_get() {
  local key="$1" file="$2"
  [ -f "$file" ] || return 0
  grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- || true
}

set_env_value() {
  local key="$1" value="$2" file="$3"
  if grep -qE "^${key}=" "$file"; then
    KEY="$key" VALUE="$value" perl -0pi -e \
      's/^\Q$ENV{KEY}\E=[^\n]*/$ENV{KEY}."=".$ENV{VALUE}/me' "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

report() {
  local path="$1" label="$2"
  if [ -e "$path" ]; then
    echo "  [OK]   $label"
  else
    echo "  [MISS] ${label}（将自动生成）"
  fi
}

echo "==> 仓库根目录: $REPO_ROOT"
echo "==> 本地文件状态"
report "$ENV_FILE" ".env"
report "$NEWAPI_ENV" "deploy/newapi/conf/new-api.env"
report "$TOKEN_FILE" "monitoring/prometheus/zhentu_token"

if [ "$MODE" = "check" ]; then
  echo "==> 校验编排配置"
  docker compose config -q
  echo "[OK] docker compose config"
  echo "==> 仅检查模式：未写入任何文件。"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERR] 未找到 docker，请先安装 Docker Desktop 或 Docker Engine + Compose 插件。" >&2
  exit 1
fi

# ---------- .env ----------
if [ ! -f "$ENV_FILE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "==> 已由 .env.example 生成 .env"
fi

TOKEN="$(env_get ZHENTU_REGISTRY_TOKEN "$ENV_FILE")"
if [ -z "$TOKEN" ]; then
  TOKEN="$(gen_secret)"
  set_env_value ZHENTU_REGISTRY_TOKEN "$TOKEN" "$ENV_FILE"
  echo "==> 已生成 ZHENTU_REGISTRY_TOKEN（写入 .env）"
fi

ADMIN_USER="$(env_get NEWAPI_ADMIN_USERNAME "$ENV_FILE")"
if [ -z "$ADMIN_USER" ]; then
  ADMIN_USER="root"
  set_env_value NEWAPI_ADMIN_USERNAME "$ADMIN_USER" "$ENV_FILE"
fi

ADMIN_PASS="$(env_get NEWAPI_ADMIN_PASSWORD "$ENV_FILE")"
if [ -z "$ADMIN_PASS" ]; then
  ADMIN_PASS="$(gen_secret)"
  set_env_value NEWAPI_ADMIN_PASSWORD "$ADMIN_PASS" "$ENV_FILE"
  echo "==> 已生成 NEWAPI_ADMIN_PASSWORD（用户名 ${ADMIN_USER}，写入 .env）"
  echo "    首次登录 NewAPI 网页时用该密码注册管理员账号。"
fi

chmod 600 "$ENV_FILE"

# ---------- NewAPI 运行环境 ----------
mkdir -p "$(dirname "$NEWAPI_ENV")"
if [ ! -f "$NEWAPI_ENV" ]; then
  {
    echo "# 由 scripts/bootstrap_cluster.sh 生成，禁止入库"
    echo "PORT=3000"
    echo "TZ=Asia/Shanghai"
    echo "SESSION_SECRET=$(gen_secret)"
    echo "CRYPTO_SECRET=$(gen_secret)"
  } > "$NEWAPI_ENV"
  echo "==> 已生成 deploy/newapi/conf/new-api.env"
fi
chmod 600 "$NEWAPI_ENV"

# ---------- 灵脉抓取 token ----------
mkdir -p "$(dirname "$TOKEN_FILE")"
if [ -f "$TOKEN_FILE" ] && [ "$(cat "$TOKEN_FILE")" != "$TOKEN" ]; then
  echo "==> 灵脉 token 与 .env 不一致，已同步为当前 ZHENTU_REGISTRY_TOKEN"
fi
printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

echo "==> 校验编排配置"
docker compose config -q
echo "[OK] docker compose config"

if [ "$UP" -eq 1 ]; then
  echo "==> 启动中枢机服务"
  docker compose up -d
  docker compose ps
else
  cat <<'EOF'

下一步：
  1. docker compose up -d
  2. 打开 http://127.0.0.1:3000 注册管理员（用户名/密码见 .env）
  3. 同步渠道：python3 deploy/newapi/gateway_admin.py channel sync
  4. 按 docs/deployment.md 上线准圣 / 散仙节点并注册到阵图
EOF
fi
