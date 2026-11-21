# 碧游宫 · 运维手册

面向已按 [`docs/deployment.md`](deployment.md) 部署完成的集群。所有命令默认在仓库根目录执行。

---

## 1. 日常命令

```bash
# 状态
docker compose ps
docker compose logs --tail 100 zhentu
docker compose logs --tail 100 new-api

# 启停单个服务（不影响其它）
docker compose up -d zhentu
docker compose restart zhentu
docker compose stop zhentu

# 停止整个中枢机控制面（边缘节点继续本地服务）
docker compose down
```

阵图健康与在线节点：

```bash
curl -s http://127.0.0.1:4020/zhentu/v1/health
curl -s -H "Authorization: Bearer $ZHENTU_REGISTRY_TOKEN" \
  http://127.0.0.1:4020/zhentu/v1/nodes | python3 -m json.tool
```

---

## 2. 批量注册节点

适合节点很多、或节点未开启自动注册时使用。清单模板见
[`deploy/nodes.example.json`](../deploy/nodes.example.json)。

```bash
export ZHENTU_REGISTRY_TOKEN='<集群 token>'
python3 scripts/register_nodes.py --file deploy/nodes.example.json --dry-run
python3 scripts/register_nodes.py --file nodes.json
```

- 注册是**覆盖快照**：同名节点再次注册会更新 endpoint / models / limits。
- 注册成功不等于长期在线：节点仍需按 `heartbeat_interval_s` 上报心跳，否则租约到期变
  `offline`。开了 `registry.enabled: true` 的节点会自己注册 + 心跳，通常不需要手工跑本脚本。
- 需要给单个节点独立 token 时，设置 `ZHENTU_NODE_TOKENS`（JSON 对象），脚本会按节点 id 取用。
- 任一节点失败退出码为 1；`--dry-run` 只打印报文。

---

## 3. 日志收集

```bash
python3 scripts/collect_logs.py --since 2h
python3 scripts/collect_logs.py --since 30m --out logs/incident-20261121
```

默认收集：

| 来源 | 内容 |
|---|---|
| `new-api` / `biyou-zhentu` / `lingmai-prometheus` / `lingmai-grafana` | `docker logs` |
| 节点 `GET /v1/logs?limit=200` | 最近运行事件（脱敏环形缓冲） |
| 阵图 `GET /zhentu/v1/nodes` | 当前注册视图 |
| 诊断 | `docker version`、`docker compose ps`、`uname -a` |

节点与阵图源需要 token：

```bash
python3 scripts/collect_logs.py \
  --node quasheng-01=http://192.168.1.10:4010 \
  --node-token "$QUASHENG_SHARED_TOKEN" \
  --registry-token "$ZHENTU_REGISTRY_TOKEN"
```

单个来源失败只写进 `summary.txt`，不影响其它来源。输出目录默认在 `logs/`（已 git 忽略）。

---

## 4. 批量检定

```bash
set -a; source .env; set +a

# 单节点压测 D1/D2/D3/D6
python3 scripts/daojian/stress_nodes.py --base-url http://<准圣IP>:4010 --strict

# 故障注入 D4/D5/D7
python3 scripts/daojian/inject_faults.py --case all

# 长时耐久 D8（默认 30 分钟）
python3 scripts/daojian/soak.py --duration 1800 --strict

# 网关 G1–G5（会临时建 biyou-verify-* 渠道并在 finally 清理）
python3 scripts/daojian/gateway_acceptance.py --strict

# 编排 E2E（默认内联桩；真实链路由 --gateway-base-url 指定）
python3 scripts/daojian/orchestration_e2e.py --strict
python3 scripts/daojian/orchestration_e2e.py \
  --gateway-base-url http://127.0.0.1:3000/v1 \
  --planner-model qwen2.5:7b --chat-model qwen2.5:7b --strict
```

建议顺序：节点压测 → 网关验收 → 编排 E2E → 故障注入 → 长时耐久。长跑前确认没有其它
重负载（此前 D8 曾在宿主机高并发时出现准圣端口失联，独占环境复跑正常）。

---

## 5. 备份与升级

### 备份

```bash
mkdir -p backup/$(date +%F)

# NewAPI 数据（SQLite/上传文件）：先停再拷，避免写坏
docker compose stop new-api
tar czf backup/$(date +%F)/newapi-data.tgz -C deploy/newapi data
docker compose start new-api

# 阵图 SQLite（命名卷）
docker run --rm \
  -v biyou-palace_zhentu-data:/data \
  -v "$PWD/backup":/backup \
  alpine tar czf /backup/$(date +%F)/zhentu-data.tgz -C /data .

# 配置（不含明文密钥，按需单独保管）
cp .env.example backup/$(date +%F)/
cp deploy/newapi/channels.json deploy/newapi/gateway-options.json backup/$(date +%F)/
```

`.env`、`deploy/newapi/conf/new-api.env`、`monitoring/prometheus/*_token` 含密钥，
单独加密保管，不要放进仓库或普通共享目录。

### 升级

```bash
git pull
bash scripts/bootstrap_cluster.sh --check

docker compose build zhentu
docker compose up -d
docker compose ps

bash scripts/verify.sh
.venv/bin/python -m pytest zhentu -q
```

- NewAPI 镜像由 `.env` 的 `NEWAPI_IMAGE` 固定；升级前先备份，再改 tag 并
  `docker compose up -d new-api`。
- 阵图升级失败可回退：`git checkout <上一个提交> && docker compose build zhentu &&
  docker compose up -d zhentu`。SQLite 结构向后兼容，回退前仍建议备份。

---

## 6. 故障排查

| 现象 | 优先检查 | 处理 |
|---|---|---|
| `biyou-zhentu` 反复重启 | `docker compose logs zhentu` | 多为 `ZHENTU_REGISTRY_TOKEN` 为空；在 `.env` 填好后 `docker compose up -d zhentu` |
| 节点列表为空或很快 offline | 节点 `registry.enabled`、`base_url`、token | token 必须与中枢机 `.env` 一致；确认节点能访问 `<中枢IP>:4020` |
| 节点 `401` | 节点 `shared_token` / `auth_enabled` | 渠道 `key`、Prometheus token 必须与节点一致 |
| 网关渠道被自动禁用 | NewAPI 日志、渠道 `status` | 上游恢复后在后台重新启用；根因多为节点 5xx/超时。429 不应纳入隔离 |
| 网关权重与声明不符 | `deploy/newapi/gateway-options.json` | NewAPI 无内存缓存时按 `(weight+10)` 平滑，声明 3:1 实际约 54%，用更大比例构造目标流量 |
| Prometheus target `DOWN` | 节点地址、token 文件、Linux `host.docker.internal` | 改 `monitoring/prometheus/prometheus.yml` 后 `curl -X POST :9090/-/reload` |
| Grafana 无数据 | 数据源 URL、Prometheus targets | 数据源应为 `http://prometheus:9090`；先修 Prometheus |
| 散仙过一会失联 | 系统省电策略、App 是否被回收 | 电池「不受限制」、保持最近任务；必要时打开 App 或重启设备 |
| 端口被占用 | `lsof -nP -iTCP:3000/4020/9090/3001` | 停掉旧栈或改 `.env` 的端口变量 |
| 阵图不拆步 | `ZHENTU_PLANNER_MODEL` | 默认空 = 不规划；填入网关可用模型后重建 zhentu |
| 阵图取消不中断在途步骤 | 取消语义 | 当前为批次边界取消，在途步骤会跑完；属已知限制 |

通用取证：

```bash
docker compose ps
docker compose logs --tail 200 zhentu
python3 scripts/collect_logs.py --since 1h
```

---

## 7. 安全清单

- 内网使用，不把 `3000` / `9090` / `3001` 映射到公网。
- 节点监听非回环地址时必须配置 `shared_token`；阵图非回环监听必须配置
  `ZHENTU_REGISTRY_TOKEN`。
- `.env`、`deploy/newapi/conf/`、`monitoring/prometheus/*_token` 权限保持 `600`，禁止入库。
- 定期轮换阵图 token、节点 token、NewAPI 管理员密码与客户端 key；轮换后同步节点配置并
  重建 zhentu / 更新 Prometheus token 文件。
- Grafana 默认匿名只读；需要更强控制时关闭匿名并改管理员密码。

---

## 8. 已知限制与 V1.1 路线

**已知限制**

- 中枢机 / 碧游宫网关是单点：阵眼失效则全阵不可用。
- 节点注册仍以「节点自注册 + 静态清单」为主，没有自动发现与自动入阵。
- 阵图取消是批次边界语义，在途步骤不会立即中断。
- 阵图指标为进程内、重启归零；只有任务指标按账本现状计算。
- 散仙后台存活受 Android 省电策略影响，无法保证永久常驻。
- 灵脉默认 15 天保留、单 Prometheus；没有远程存储与高可用。

**V1.1 方向**

- 自动入阵：新节点上线自动注册并进入候选池。
- 动态权重：按灵脉实时时延/温度调整网关与阵图权重。
- 任务优先级与队列。
- 日志审计：每道果溯源到参与仙徒与子任务。
- 中枢高可用 / 网关多活。
