# PRD · 第五步：归档传道（一键部署 / 运维手册 / 从零复现）

## 一、目标

把前四步已跑通的节点、网关、阵图、灵脉、道鉴收敛成**别人照着文档就能从零复现**的交付物：

1. 中枢机一条 `docker compose up -d` 起碧游宫 + 阵图 + 灵脉；
2. 边缘节点按文档上线、注册、接入网关；
3. 运维脚本覆盖批量注册、日志收集、批量检定；
4. 明确已知限制与 V1.1 迭代路线。

## 二、范围

### 做

- 根级 `docker-compose.yml` + `.env.example`：`new-api` / `zhentu` / `prometheus` / `grafana` 四个服务。
- `deploy/zhentu/Dockerfile` + 根 `.dockerignore`：阵图镜像化。
- `scripts/bootstrap_cluster.sh`：幂等生成本地 `.env`、NewAPI 环境文件、灵脉抓取 token，可选直接拉起。
- `scripts/register_nodes.py`：按 JSON 清单批量注册节点到阵图（纯标准库）。
- `scripts/collect_logs.py`：收集容器日志、节点 `/v1/logs` 与诊断快照（纯标准库）。
- `docs/deployment.md`：前置条件 → 中枢机 → 准圣 → 散仙 → 渠道同步 → 灵脉 → 道鉴 → 验收。
- `docs/operations.md`：批量注册、日志收集、批量检定、备份/升级、常见故障。
- 扩展根 `README.md`：碧游宫、阵图编排、一键部署、从零复现、已知限制。
- `docs/reports/biyou-deploy-2026-11-21.md`：本次交付实测报告。
- 更新蓝图 PRD 第五步进度。

### 不做

- 不改节点 / 阵图 / 网关的 HTTP 契约与推理行为。
- 不启动或停止现有 `new-api`、阵图、准圣、Ollama 等服务。
- 不写 `~/biyou` 下的运行数据，不提交任何明文密钥。
- 不做 V1.1 功能（自动入阵、动态权重、任务优先级、日志审计、高可用）。

## 三、验收标准

| 编号 | 场景 | 判据 |
|---|---|---|
| D1 | Compose 可解析 | 根目录 `docker compose config -q` 退出码 0 |
| D2 | 配置自洽 | `bash scripts/bootstrap_cluster.sh --check` 不覆盖已有文件且退出码 0 |
| D3 | 批量注册可用 | `register_nodes.py --dry-run` 输出计划；对不可达阵图返回非 0 且错误可读 |
| D4 | 日志收集可用 | `collect_logs.py --help` 正常；无 Docker 时记录错误但不抛栈 |
| D5 | 文档闭环 | `docs/deployment.md` 覆盖从零到验收的全部命令；`README.md` 能导航到该文档 |
| D6 | 回归不退化 | `bash scripts/verify.sh` 通过；`.venv/bin/python -m pytest zhentu -q` 通过 |
