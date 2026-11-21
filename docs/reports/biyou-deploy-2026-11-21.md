# 碧游宫 · 归档传道交付报告（2026-11-21）

> 范围：第五步「归档传道」——中枢机一键编排、阵图镜像、部署/运维手册、批量注册与
> 日志收集脚本。目标是把前四步的能力收敛成「他人依文档从零复现」。

## 一、交付物

| 类别 | 路径 | 说明 |
|---|---|---|
| 一键编排 | `docker-compose.yml` | `include` NewAPI / 灵脉，新增 `zhentu` 服务与 `zhentu-data` 卷 |
| 配置模板 | `.env.example` | 碧游宫 / 阵图 / 灵脉全部变量与密钥占位 |
| 构建 | `deploy/zhentu/Dockerfile`、`.dockerignore` | 阵图镜像（python:3.12-slim，非 root，`/data`） |
| 初始化 | `scripts/bootstrap_cluster.sh` | 幂等生成 `.env`、NewAPI 密钥、灵脉 token；`--check` / `--up` |
| 批量注册 | `scripts/register_nodes.py` + `deploy/nodes.example.json` | JSON 清单注册到阵图，支持 `--dry-run` |
| 日志收集 | `scripts/collect_logs.py` | 容器日志 + 节点 `/v1/logs` + 阵图视图 + 诊断快照 |
| 部署手册 | `docs/deployment.md` | 从零到验收的完整命令 |
| 运维手册 | `docs/operations.md` | 注册 / 日志 / 检定 / 备份升级 / 故障排查 / V1.1 |
| 导航 | `README.md` | 新增集群定位、一键部署、网关与阵图、已知限制、参考文档 |

## 二、验证结果

所有验证均未启动或停止现有服务（`new-api`、宿主机阵图 `:4020`、准圣、Ollama 保持原状）。

| # | 命令 | 结果 |
|---|---|---|
| D1 | `docker compose config -q` | 通过；解析出 `new-api` / `zhentu` / `prometheus` / `grafana` 四个服务 |
| D2 | `bash scripts/bootstrap_cluster.sh --check` | 通过；只报告缺失文件，不写入 |
| D2b | 临时目录完整执行 bootstrap 两次 | 首次生成 `.env` / `new-api.env` / `zhentu_token` 且三者 token 一致；第二次不覆盖 |
| D3 | `register_nodes.py --file deploy/nodes.example.json --dry-run` | 通过；输出两个节点的完整注册报文 |
| D4 | `collect_logs.py --since 5m --out /tmp/...` | 通过；`new-api` 日志收集成功，未运行容器记为 `FAIL`，退出码 0 |
| D5 | 文档检查 | `deployment.md` 覆盖中枢机 → 网关 → 准圣 → 散仙 → 灵脉 → 道鉴 → 验收；README 可导航 |
| D6 | `bash scripts/verify.sh` | **211 passed** |
| D6b | `.venv/bin/python -m pytest zhentu -q` | **76 passed** |

Compose 解析确认：

- 子编排相对路径按 `deploy/newapi/`、`monitoring/` 自身目录解析，NewAPI 数据落
  `deploy/newapi/data`，看板与规则仍指向 `monitoring/`。
- `zhentu` 容器内 `ZHENTU_NEWAPI_BASE_URL=http://new-api:3000`，不绕宿主机回环。
- 灵脉端口改为 `${PROMETHEUS_PORT:-9090}` / `${GRAFANA_PORT:-3001}`，与根 `.env` 对齐。

## 三、未验证项（如实记录）

| 项 | 原因 | 影响 / 后续 |
|---|---|---|
| `docker build -f deploy/zhentu/Dockerfile .` | 本机无法访问 `registry-1.docker.io`（`python:3.12-slim` 拉取超时），本地仅有 new-api 与 alpine 镜像 | Dockerfile 结构与依赖已在宿主机 venv 验证；有外网或镜像代理的机器上应能构建。首次部署前建议先 `docker compose build zhentu` |
| `docker compose up -d` | 现有 `new-api` 容器与宿主机 `:4020` 阵图正在服务，按要求不启停 | 从零复现需在新中枢机或先停旧栈后执行；文档已写明迁移注意事项 |
| 真实多机节点上线 | 本次仅做仓库内验证，未新增设备 | 节点侧命令沿用前四步已实测脚本；部署手册给出逐条验证命令 |

## 四、关键设计决定

1. **`include` 复用而非复制**：根 compose 只新增阵图，避免 NewAPI / 灵脉三份配置漂移；
   已验证路径与卷解析正确。
2. **本地文件由 bootstrap 派生**：`.env`、`new-api.env`、`zhentu_token` 全部 git 忽略，
   脚本只生成缺失项，token 文件与 `.env` 始终同步。
3. **运维脚本纯标准库**：注册与日志收集不引入额外依赖；单源失败不拖垮整次收集。
4. **文档单一入口**：README 负责导航，`deployment.md` 负责从零，`operations.md` 负责日常；
   蓝图 PRD 保留阶段进度。

## 五、验收结论

第五步的仓库内交付项（D1–D6）全部通过。镜像构建与真实 `compose up` 受本机网络和
「不启停现有服务」约束未执行，已在文档与本节明确标注；在有外网的中枢机上按
`docs/deployment.md` 第 3 节执行即可完成首次拉起。
