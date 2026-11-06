#!/usr/bin/env bash
#
# 准圣节点 quasheng 自动化冒烟脚本
#
# 不依赖真实 Ollama：复用 nodes/quasheng/tests/mock_ollama.py 作为上游，
# 端到端跑通「起节点 → 正向端点 → 负向切面」的关键路径。
#
# 用法：
#   bash scripts/smoke_quasheng.sh
#   SMOKE_MOCK_PORT=11499 SMOKE_NODE_PORT=4012 bash scripts/smoke_quasheng.sh
#
# 退出码：全部断言通过 0；任一失败 1；前置条件（端口占用 / 起服务失败）1。
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.venv"
REQ_FILE="$REPO_ROOT/nodes/quasheng/requirements.txt"
MANAGED_PY="/Users/zaze/.workbuddy/binaries/python/versions/3.13.12/bin/python3"

# 端口刻意避开 Ollama(11434) 与节点默认(4010)
MOCK_PORT="${SMOKE_MOCK_PORT:-11499}"
NODE_PORT="${SMOKE_NODE_PORT:-4012}"
TOKEN="smoke-token"
# mock 每两个 chunk 之间的间隔；需要足够长，才能在并发用例里观察到「流进行中」
MOCK_DELAY="${SMOKE_STREAM_DELAY:-1.2}"
HTTP_TIMEOUT="${SMOKE_HTTP_TIMEOUT:-30}"

BASE="http://127.0.0.1:$NODE_PORT"
STREAM_PAYLOAD='{"model":"mock-7b","messages":[{"role":"user","content":"hi"}],"stream":true}'

PASS_COUNT=0
FAILS=()
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '[PASS] %s\n' "$1"; }
fail() { FAILS+=("$1"); printf '[FAIL] %s\n' "$1"; }

TMP_DIR=""
MOCK_PID=""
NODE_PID=""
MOCK_LOG=""
NODE_LOG=""

dump_logs() {
  echo "---- node.log (tail -n 30) ----"
  tail -n 30 "$NODE_LOG" 2>/dev/null || true
  echo "---- mock.log (tail -n 20) ----"
  tail -n 20 "$MOCK_LOG" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  local pid
  for pid in ${NODE_PID:-} ${MOCK_PID:-}; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 0.2
  for pid in ${NODE_PID:-} ${MOCK_PID:-}; do
    kill -9 "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  if [ -n "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

port_busy() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# ---------------------------------------------------------------- 前置条件

echo "==> 仓库根目录: $REPO_ROOT"

if [ -x "$MANAGED_PY" ]; then
  PY="$MANAGED_PY"
else
  PY="$(command -v python3 || true)"
  echo "[WARN] 托管 Python 缺失，回退到 PATH 上的 python3: ${PY:-none}"
fi
if [ -z "$PY" ]; then
  echo "[ERR] 未找到 python3，请先安装 Python 3.11+" >&2
  exit 1
fi
PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> 使用 python ($PY_VER) @ $PY"
if [ "$(printf '%s\n' "$PY_VER" | cut -d. -f2)" -lt 11 ] 2>/dev/null; then
  echo "[ERR] 需要 Python >= 3.11，当前 $PY_VER" >&2
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "==> 创建虚拟环境: $VENV_DIR"
  "$PY" -m venv "$VENV_DIR" || { echo "[ERR] 创建虚拟环境失败" >&2; exit 1; }
fi
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if ! "$VENV_PY" -c 'import fastapi, uvicorn, httpx, yaml, psutil' >/dev/null 2>&1; then
  echo "==> 安装依赖: $REQ_FILE"
  (
    export CODEBUDDY_SAFE_DELETE_ENABLED=0
    "$VENV_PIP" install -q -r "$REQ_FILE"
  ) || { echo "[ERR] 依赖安装失败" >&2; exit 1; }
fi

if command -v lsof >/dev/null 2>&1; then
  if port_busy "$MOCK_PORT"; then
    echo "[ERR] 端口 $MOCK_PORT 已被占用（mock Ollama），请用 SMOKE_MOCK_PORT 换一个" >&2
    exit 1
  fi
  if port_busy "$NODE_PORT"; then
    echo "[ERR] 端口 $NODE_PORT 已被占用（quasheng 节点），请用 SMOKE_NODE_PORT 换一个" >&2
    exit 1
  fi
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/quasheng-smoke.XXXXXX")" || {
  echo "[ERR] 无法创建临时目录" >&2
  exit 1
}
MOCK_LOG="$TMP_DIR/mock.log"
NODE_LOG="$TMP_DIR/node.log"
echo "==> 临时目录: $TMP_DIR"

# ---------------------------------------------------------------- 起 mock 上游

echo "==> 启动 mock Ollama (port $MOCK_PORT, stream delay ${MOCK_DELAY}s)"
MOCK_STREAM_DELAY="$MOCK_DELAY" "$VENV_PY" -m uvicorn nodes.quasheng.tests.mock_ollama:app \
  --host 127.0.0.1 --port "$MOCK_PORT" --log-level warning >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
disown "$MOCK_PID" 2>/dev/null || true

mock_ready=0
for _ in $(seq 1 60); do
  if curl -sS --max-time 2 -o /dev/null "http://127.0.0.1:$MOCK_PORT/healthz" 2>/dev/null; then
    mock_ready=1
    break
  fi
  if ! kill -0 "$MOCK_PID" 2>/dev/null; then break; fi
  sleep 0.5
done
if [ "$mock_ready" -ne 1 ]; then
  echo "[ERR] mock Ollama 未就绪（port ${MOCK_PORT}）" >&2
  dump_logs
  exit 1
fi
pass "mock Ollama 就绪 ($MOCK_PORT)"

# ---------------------------------------------------------------- 配置与起节点

cat >"$TMP_DIR/config.yaml" <<EOF
ollama_base: "http://127.0.0.1:$MOCK_PORT"
port: $NODE_PORT
node_id: "smoke-node"
node_name: "冒烟节点"
shared_token: "$TOKEN"
auth_enabled: true
preload_models: []
capabilities_map:
  "mock-7b":
    capabilities: ["chat"]
    context_len: 32768
  "mock-embed":
    capabilities: ["embedding"]
    context_len: 8192
max_concurrency: 2
mem_warn_pct: 100
cpu_warn_pct: 100
request_timeout: 60
EOF

echo "==> 启动 quasheng 节点 (port $NODE_PORT)"
QUASHENG_CONFIG="$TMP_DIR/config.yaml" "$VENV_PY" -m uvicorn nodes.quasheng.app:app \
  --host 127.0.0.1 --port "$NODE_PORT" --log-level info >"$NODE_LOG" 2>&1 &
NODE_PID=$!
disown "$NODE_PID" 2>/dev/null || true

node_ready=0
for _ in $(seq 1 120); do
  code="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" "$BASE/v1/health" 2>/dev/null)" || code="000"
  if [ "$code" = "200" ]; then
    node_ready=1
    break
  fi
  if ! kill -0 "$NODE_PID" 2>/dev/null; then break; fi
  sleep 0.5
done
if [ "$node_ready" -ne 1 ]; then
  echo "[ERR] quasheng 节点未就绪（port ${NODE_PORT}）" >&2
  dump_logs
  exit 1
fi
pass "quasheng 节点就绪 (/v1/health 200)"

# ---------------------------------------------------------------- 断言工具

expect_code() { # name expected actual
  if [ "$3" = "$2" ]; then
    pass "$1"
  else
    fail "${1}（期望 ${2}，实际 ${3:-none}）"
  fi
}

json_check() { # name file python-code
  if "$PY" -c "$3" "$2" 2>"$TMP_DIR/pyerr.txt"; then
    pass "$1"
  else
    fail "$1"
    tail -n 3 "$TMP_DIR/pyerr.txt" 2>/dev/null | sed 's/^/         /'
  fi
}

hdr_check() { # name header-file regex
  if grep -Eqi -- "$3" "$2" 2>/dev/null; then
    pass "$1"
  else
    fail "$1"
  fi
}

http_get() { # path out [extra curl args...]
  local path=$1 out=$2
  shift 2
  curl -sS --max-time "$HTTP_TIMEOUT" -o "$out" -w '%{http_code}' "$@" "$BASE$path"
}

http_post() { # path out [extra curl args...]
  local path=$1 out=$2
  shift 2
  curl -sS --max-time "$HTTP_TIMEOUT" -o "$out" -w '%{http_code}' -X POST "$@" "$BASE$path"
}

AUTH_HEADER="Authorization: Bearer $TOKEN"
JSON_HEADER="Content-Type: application/json"

echo "==> 开始断言"

# ---------------------------------------------------------------- 1) health

code="$(http_get /v1/health "$TMP_DIR/health.json" -H "$AUTH_HEADER")" || code="000"
expect_code "GET /v1/health → 200" "200" "$code"
json_check "health 关键字段：node / status / models / resources / limits" "$TMP_DIR/health.json" '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
node = d["node"]
assert node["id"] == "smoke-node", node
assert node["role"] == "quasheng", node
assert node["runtime"] == "ollama", node
assert node["runtime_version"] == "0.5.7", node
assert node["contract_version"] == "1.0", node
assert node["arch"], node
assert d["status"] == "ready", d["status"]
res = d["resources"]
for k in ("cpu_pct", "mem_used_mb", "mem_total_mb", "disk_free_mb",
          "queue_len", "battery_pct", "temperature_c", "thermal_state"):
    assert k in res, k
assert res["battery_pct"] is None
assert res["temperature_c"] is None
assert res["thermal_state"] is None
assert res["queue_len"] == 0, res
assert d["limits"]["max_concurrency"] == 2, d["limits"]
assert d["limits"]["max_context_len"] == 32768, d["limits"]
by = {m["name"]: m for m in d["models"]}
assert list(by) == ["mock-7b", "mock-embed"], list(by)
assert by["mock-7b"]["loaded"] is True
assert by["mock-embed"]["loaded"] is False
assert by["mock-7b"]["capabilities"] == ["chat"], by["mock-7b"]
assert by["mock-embed"]["capabilities"] == ["embedding"], by["mock-embed"]
assert d["updated_at"], d
'

# ---------------------------------------------------------------- 2) models

code="$(http_get /v1/models "$TMP_DIR/models.json" -H "$AUTH_HEADER")" || code="000"
expect_code "GET /v1/models → 200" "200" "$code"
json_check "/v1/models 列表形状（object=list / data[] / id+object=model / owned_by=biyou-node）" "$TMP_DIR/models.json" '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["object"] == "list", d
assert isinstance(d["data"], list) and d["data"], d
for item in d["data"]:
    assert item["object"] == "model", item
    assert item["id"], item
    assert item["owned_by"] == "biyou-node", item
assert {i["id"] for i in d["data"]} == {"mock-7b", "mock-embed"}, d
'

# ---------------------------------------------------------------- 3) 鉴权

code="$(http_get /v1/health "$TMP_DIR/health_noauth.json")" || code="000"
expect_code "缺 token → /v1/health 401（health 同样受鉴权保护）" "401" "$code"
json_check "401 错误体为 OpenAI 风格 invalid_api_key" "$TMP_DIR/health_noauth.json" '
import json, sys
err = json.load(open(sys.argv[1], encoding="utf-8"))["error"]
assert set(err) == {"message", "type", "param", "code"}, err
assert err["type"] == "invalid_request_error", err
assert err["code"] == "invalid_api_key", err
'

code="$(http_get /v1/models "$TMP_DIR/models_noauth.json")" || code="000"
expect_code "缺 token → /v1/models 401" "401" "$code"

# ---------------------------------------------------------------- 4) 无 embedding 能力 → 501

code="$(http_post /v1/embeddings "$TMP_DIR/emb.json" \
  -H "$AUTH_HEADER" -H "$JSON_HEADER" \
  -d '{"model":"mock-7b","input":["hi"]}')" || code="000"
expect_code "mock-7b 未声明 embedding 能力 → 501" "501" "$code"
json_check "501 错误码 capability_not_supported" "$TMP_DIR/emb.json" '
import json, sys
err = json.load(open(sys.argv[1], encoding="utf-8"))["error"]
assert err["code"] == "capability_not_supported", err
assert err["type"] == "invalid_request_error", err
'

# ---------------------------------------------------------------- 5) 流式 chat

code="$(curl -sS --max-time "$HTTP_TIMEOUT" -N -D "$TMP_DIR/stream.hdr" \
  -o "$TMP_DIR/stream.txt" -w '%{http_code}' -X POST \
  -H "$AUTH_HEADER" -H "$JSON_HEADER" -d "$STREAM_PAYLOAD" \
  "$BASE/v1/chat/completions")" || code="000"
expect_code "流式 /v1/chat/completions → 200" "200" "$code"
hdr_check "流式响应 Content-Type: text/event-stream" "$TMP_DIR/stream.hdr" '^content-type: *text/event-stream'
json_check "流式 chunk 内容拼接完整且以 data: [DONE] 收尾" "$TMP_DIR/stream.txt" '
import json, sys
body = open(sys.argv[1], encoding="utf-8").read()
lines = [l for l in body.splitlines() if l.startswith("data: ")]
assert lines, "没有任何 data: 行"
assert lines[-1] == "data: [DONE]", lines[-1]
chunks = [json.loads(l[6:]) for l in lines if l != "data: [DONE]"]
assert len(chunks) >= 3, len(chunks)
contents = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
assert contents == "你好，我是准圣节点。", contents
'

# ---------------------------------------------------------------- 6) 并发超限 → 429

conc_pids=()
for i in 1 2 3; do
  curl -sS --max-time 20 -D "$TMP_DIR/conc_$i.hdr" -o "$TMP_DIR/conc_$i.body" \
    -w '%{http_code}' -X POST -H "$AUTH_HEADER" -H "$JSON_HEADER" \
    -d "$STREAM_PAYLOAD" "$BASE/v1/chat/completions" \
    >"$TMP_DIR/conc_$i.code" 2>/dev/null &
  conc_pids+=($!)
done
wait "${conc_pids[@]}" 2>/dev/null || true

# curl -w 输出不带换行，逐个补换行后再排序，避免 "200429200" 这类粘连
codes="$(for i in 1 2 3; do printf '%s\n' "$(cat "$TMP_DIR/conc_$i.code" 2>/dev/null)"; done | sort | tr '\n' ' ')"
if [ "$codes" = "200 200 429 " ]; then
  pass "max_concurrency=2 时第 3 个并发流 → 429（实际: ${codes}）"
else
  fail "并发闸门未按预期拒绝（期望 200 200 429，实际: ${codes:-none}）"
fi

rejected=""
for i in 1 2 3; do
  if [ "$(cat "$TMP_DIR/conc_$i.code" 2>/dev/null)" = "429" ]; then
    rejected="$i"
    break
  fi
done
if [ -n "$rejected" ]; then
  hdr_check "429 带 Retry-After 响应头" "$TMP_DIR/conc_$rejected.hdr" '^retry-after: *1'
  json_check "429 错误体 code=concurrency_limit_exceeded / type=rate_limit_error" \
    "$TMP_DIR/conc_$rejected.body" '
import json, sys
err = json.load(open(sys.argv[1], encoding="utf-8"))["error"]
assert err["code"] == "concurrency_limit_exceeded", err
assert err["type"] == "rate_limit_error", err
'
else
  fail "未找到 429 响应，无法校验 Retry-After / 错误体"
fi

# ---------------------------------------------------------------- 7) 客户端断连 → 中止上游

# 故意在流结束前主动断开（mock 全流约 3.6s）
curl -sS --max-time 1 -N -X POST -H "$AUTH_HEADER" -H "$JSON_HEADER" \
  -d "$STREAM_PAYLOAD" "$BASE/v1/chat/completions" >"$TMP_DIR/disconnect.body" 2>/dev/null || true

aborted=0
for _ in $(seq 1 20); do
  if grep -q "中止上游生成" "$NODE_LOG" 2>/dev/null; then
    aborted=1
    break
  fi
  sleep 0.5
done
if [ "$aborted" -eq 1 ]; then
  pass "客户端中途断连 → 服务端日志出现「中止上游生成」"
else
  fail "客户端中途断连 → 未在节点日志中找到「中止上游生成」"
fi

# ---------------------------------------------------------------- 汇总

echo
if [ "${#FAILS[@]}" -eq 0 ]; then
  echo "[OK] 冒烟通过：$PASS_COUNT 项断言全部通过"
  exit 0
fi

total=$((PASS_COUNT + ${#FAILS[@]}))
echo "[FAIL] 冒烟失败：${#FAILS[@]} / $total 项未通过"
for item in ${FAILS[@]+"${FAILS[@]}"}; do
  echo "  - $item"
done
dump_logs
exit 1
