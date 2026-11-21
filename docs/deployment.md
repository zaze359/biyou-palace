# 碧游宫 · 部署手册（从零复现）

本文从一台空的中枢机开始，按顺序完成：中枢机编排 → 碧游宫初始化 → 准圣上线 →
散仙上线 → 渠道同步 → 灵脉 → 验收。所有命令均在仓库根目录执行。

---

## 0. 拓扑与端口

```
边缘节点                                     中枢机（一台常开 PC）
┌──────────────────────┐                  ┌──────────────────────────────────┐
│ 准圣 PC               │  4010 推理        │  new-api       127.0.0.1:3000    │
│ Ollama :11434         │ ───────────────▶ │  zhentu        0.0.0.0:4020     │
│                       │  4020 注册/心跳   │  prometheus    0.0.0.0:9090     │
├──────────────────────┤                  │  grafana       0.0.0.0:3001     │
│ 散仙 安卓             │                  │                                  │
│ llama.cpp :4011      │                  │  docker compose（仓库根）          │
└──────────────────────┘                  └──────────────────────────────────┘
```

| 端口 | 服务 | 暴露范围 | 说明 |
|---|---|---|---|
| `3000` | 碧游宫 NewAPI | 仅本机 | 统一 OpenAI 兼容入口；节点不直连 |
| `4020` | 阵图 ZhenTu | 局域网 | 节点注册 / 心跳 / 选路 / 任务 |
| `9090` | Prometheus | 局域网 | 灵脉时序库 |
| `3001` | Grafana | 局域网 | 看板「灵脉 · 碧游宫集群」 |
| `4010` | 准圣节点 | 局域网 | OpenAI 兼容推理 |
| `4011` | 散仙节点 | 局域网 | OpenAI 兼容推理 |

> 数据不出局域网：不要把 `3000` / `9090` / `3001` 映射到公网。

---

## 1. 前置条件

### 中枢机

- Linux / macOS，Docker ≥ 24，**Docker Compose ≥ 2.20**（根编排使用 `include`）
- 建议 ≥ 4 核 / 8 GB；磁盘预留 NewAPI 数据、Prometheus 15 天时序
- 固定局域网 IP（下文记作 `<中枢IP>`）
- 可选：Python 3.11+（跑 `gateway_admin.py`、道鉴脚本）

```bash
docker --version
docker compose version    # 必须 >= 2.20
```

### 准圣节点（每台 PC）

- Python 3.11+
- Ollama ≥ 0.5.x（低于此版本没有 `/v1/*`）
- 与中枢机同一局域网

### 散仙节点（安卓）

- JDK 17、Android SDK 35、Build-Tools 35.0.0、NDK 27.x、CMake 3.22.1、adb
- 与中枢机同一局域网；模型 `.gguf` 需自行推送

---

## 2. 获取代码

```bash
git clone <仓库地址> biyou-palace
cd biyou-palace
```

---

## 3. 中枢机：一条命令起控制面

```bash
# 生成 .env / NewAPI 密钥 / 灵脉 token，并校验 compose 配置（不启动）
bash scripts/bootstrap_cluster.sh

# 启动 new-api + zhentu + prometheus + grafana
docker compose up -d

docker compose ps
curl -s http://127.0.0.1:3000/api/status | head -c 200
curl -s http://127.0.0.1:4020/zhentu/v1/health
```

`bootstrap_cluster.sh` 是幂等的：已存在的 `.env`、`deploy/newapi/conf/new-api.env`、
`monitoring/prometheus/zhentu_token` 不会被覆盖。只想检查不写文件：

```bash
bash scripts/bootstrap_cluster.sh --check
```

生成的关键变量（都在被 git 忽略的本地文件里）：

| 变量 | 位置 | 用途 |
|---|---|---|
| `ZHENTU_REGISTRY_TOKEN` | `.env` | 阵图 Bearer；节点注册、灵脉抓取共用 |
| `NEWAPI_ADMIN_USERNAME` / `PASSWORD` | `.env` | `gateway_admin.py` 管理员鉴权 |
| `ZHENTU_NEWAPI_API_KEY` | `.env` | 客户端调用 key；阵图规划/聚合也用它 |
| `SESSION_SECRET` / `CRYPTO_SECRET` | `deploy/newapi/conf/new-api.env` | NewAPI 会话与加密 |

> 如果你之前单独跑过 `deploy/newapi` 或 `monitoring` 的 compose，先 `cd` 到对应目录
> `docker compose down` 再回根目录启动。根编排的卷名带 `biyou-palace_` 前缀，不会复用
> 旧栈的匿名数据；需要保留历史时先备份 `data/` 与 Grafana 卷。

---

## 4. 碧游宫初始化

### 4.1 注册管理员

打开 <http://127.0.0.1:3000>，用 `.env` 里的 `NEWAPI_ADMIN_USERNAME` /
`NEWAPI_ADMIN_PASSWORD` 注册首个管理员（NewAPI 首次启动要求网页创建）。

```bash
grep -E '^NEWAPI_ADMIN_(USERNAME|PASSWORD)=' .env
```

### 4.2 创建客户端 API key

NewAPI 后台 → 令牌 → 新建令牌，把 key 写入 `.env`：

```bash
ZHENTU_NEWAPI_API_KEY=sk-...
```

然后重建阵图容器让新 key 生效：

```bash
docker compose up -d zhentu
```

### 4.3 同步渠道与网关基线

```bash
set -a; source .env; set +a

python3 deploy/newapi/gateway_admin.py channel list
python3 deploy/newapi/gateway_admin.py channel sync --dry-run
python3 deploy/newapi/gateway_admin.py channel sync
python3 deploy/newapi/gateway_admin.py option apply
python3 deploy/newapi/gateway_admin.py channel test quasheng-01
```

`deploy/newapi/channels.json` 默认包含两条渠道：

| 渠道 | 类型 | base_url | 适用 |
|---|---|---|---|
| `ollama-local` | 4（Ollama） | `host.docker.internal:11434` | 中枢机本机的 Ollama |
| `quasheng-01` | 1（OpenAI 兼容） | `host.docker.internal:4010` | 中枢机本机的准圣 |

**远程节点必须改 base_url**：把 `host.docker.internal` 换成节点局域网 IP，
例如 `http://192.168.1.10:4010`。散仙渠道同理：

```json
{
  "name": "sanxian-01",
  "type": 1,
  "base_url": "http://192.168.1.20:4011",
  "models": ["Qwen3-0.6B-Q8_0.gguf"],
  "group": "default",
  "weight": 1,
  "auto_ban": 1,
  "key": "<散仙 shared_token，未开鉴权随便填>",
  "setting": {}
}
```

改完重新 `channel sync`。`channel sync` 只创建/更新声明内的渠道，不删除其它渠道。

---

## 5. 准圣节点上线

在准圣 PC 上：

```bash
# 1) 安装并启动 Ollama，拉模型
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# 2) 生成配置
cp nodes/quasheng/config.example.yaml nodes/quasheng/config.yaml
```

编辑 `nodes/quasheng/config.yaml`，至少改四处：

```yaml
host: "0.0.0.0"                 # 允许网关/灵脉从局域网访问
shared_token: "<强随机token>"   # 非回环监听必填
auth_enabled: true

preload_models:
  - "qwen2.5:7b"

capabilities_map:
  "qwen2.5:7b":
    capabilities: ["chat"]
    context_len: 32768
  "nomic-embed-text:latest":
    capabilities: ["embedding"]
    context_len: 8192

registry:
  enabled: true
  base_url: "http://<中枢IP>:4020"
  token_env: "ZHENTU_REGISTRY_TOKEN"
  heartbeat_interval_s: 15
  public_endpoint: "http://<准圣IP>:4010"
```

```bash
# 3) 启动（前台；首次会自动建 .venv、装依赖、探活 Ollama）
export ZHENTU_REGISTRY_TOKEN='<与中枢机 .env 一致>'
bash scripts/run_quasheng.sh
```

验证：

```bash
curl http://<准圣IP>:4010/v1/health | python3 -m json.tool | head -40
curl -H "Authorization: Bearer $ZHENTU_REGISTRY_TOKEN" \
  http://<中枢IP>:4020/zhentu/v1/nodes | python3 -m json.tool
```

`/v1/health` 的 `status` 应为 `ready`，`models[]` 中模型 `loaded=true`；
阵图节点列表应在 90 秒租约内出现 `quasheng-01`。

> 若准圣与中枢机同机，`host` 可保留 `127.0.0.1`，渠道继续用
> `host.docker.internal`；此时网关容器经 `extra_hosts` 访问宿主机。

---

## 6. 散仙节点上线

```bash
cd nodes/sanxian
./build_and_run_sanxian.sh --push-model ~/Qwen3-0.6B-Q8_0.gguf
```

编辑 `nodes/sanxian/composeApp/src/androidMain/assets/sanxian.json` 后**重新编译**：

```json
{
  "registry_url": "http://<中枢IP>:4020",
  "registry_token": "<与中枢机 ZHENTU_REGISTRY_TOKEN 一致>",
  "registry_heartbeat_interval_s": 15,
  "registry_public_endpoint": "http://<散仙IP>:4011"
}
```

设备与中枢机同局域网时直接抓设备 IP：

```bash
curl http://<散仙IP>:4011/v1/health
```

仅本机调试时可用 `adb forward tcp:4011 tcp:4011`，但网关容器无法访问另一台机器的
adb 转发端口——真实集群请使用设备局域网 IP。

后台存活：保持 App 在前台或「最近任务」，电池设为「不受限制」；详见
[`nodes/sanxian/README.md`](../nodes/sanxian/README.md#后台存活用户侧约定android-12实测结论)。

---

## 7. 灵脉

`bootstrap_cluster.sh` 已把 `ZHENTU_REGISTRY_TOKEN` 写入
`monitoring/prometheus/zhentu_token`。若节点在远程机器，改抓取目标：

`monitoring/prometheus/prometheus.yml`

```yaml
- job_name: quasheng
  static_configs:
    - targets: ["192.168.1.10:4010"]
- job_name: sanxian
  static_configs:
    - targets: ["192.168.1.20:4011"]
- job_name: zhentu
  static_configs:
    - targets: ["<中枢IP>:4020"]
```

节点开了 `auth_enabled` 时，取消对应 job 的 `authorization` 注释，并把 token 写入
`monitoring/prometheus/quasheng_token` / `sanxian_token`（不要带换行）。

```bash
curl -X POST http://127.0.0.1:9090/-/reload
```

- Prometheus Targets：<http://127.0.0.1:9090/targets>，三个 job 均 `UP`
- Grafana：<http://127.0.0.1:3001> → 看板「灵脉 · 碧游宫集群」

---

## 8. 道鉴验收

```bash
set -a; source .env; set +a

# 节点压测 / 故障注入 / 长时耐久
python3 scripts/daojian/stress_nodes.py --base-url http://<准圣IP>:4010 --strict
python3 scripts/daojian/inject_faults.py --case all
python3 scripts/daojian/soak.py --duration 600 --strict

# 网关验收（G1–G5）
python3 scripts/daojian/gateway_acceptance.py --strict

# 编排链路（桩模式；真实模式加 --gateway-base-url http://127.0.0.1:3000/v1）
python3 scripts/daojian/orchestration_e2e.py --strict
```

报告默认落在 `docs/reports/` 或 `out/`，均为本地产物，不入库。

---

## 9. 从零复现验收清单

| # | 检查 | 命令 | 期望 |
|---|---|---|---|
| 1 | 编排可解析 | `docker compose config -q` | 退出码 0 |
| 2 | 四个容器 | `docker compose ps` | `new-api` / `biyou-zhentu` / `lingmai-prometheus` / `lingmai-grafana` 均 running |
| 3 | 阵图存活 | `curl -s http://127.0.0.1:4020/zhentu/v1/health` | `"status":"ready"`、`"db":"ok"` |
| 4 | 网关模型 | `curl -s -H "Authorization: Bearer $ZHENTU_NEWAPI_API_KEY" http://127.0.0.1:3000/v1/models` | 200，含已配置模型 |
| 5 | 节点在线 | `curl -s -H "Authorization: Bearer $ZHENTU_REGISTRY_TOKEN" http://127.0.0.1:4020/zhentu/v1/nodes` | 准圣/散仙 `lease.state=online` |
| 6 | 指标三源 | Prometheus `/targets` | 三个 job `UP` |
| 7 | 网关验收 | `gateway_acceptance.py --strict` | G1–G5 全绿 |
| 8 | 编排验收 | `orchestration_e2e.py --strict` | 7/7 |

---

## 10. 常见问题

见 [`docs/operations.md`](operations.md) 的「故障排查」。最短路径：

```bash
docker compose ps
docker compose logs --tail 100 zhentu
python3 scripts/collect_logs.py --since 1h
```
