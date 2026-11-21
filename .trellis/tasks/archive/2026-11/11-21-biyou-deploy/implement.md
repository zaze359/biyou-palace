# 实施 · 归档传道

## 顺序

1. **根编排**
   - `docker-compose.yml`：`include` NewAPI / 灵脉，新增 `zhentu` 服务与 `zhentu-data` 卷。
   - `.env.example`：NewAPI、阵图、灵脉、密钥占位。
   - `.dockerignore`：排除 `.venv`、`.git`、节点安卓工程与构建产物。
2. **阵图镜像**
   - `deploy/zhentu/Dockerfile`（python:3.12-slim，非 root，`/data`）。
   - 调整 `deploy/newapi/docker-compose.yml` 的 `env_file` 为可选，避免缺文件时无法解析。
3. **脚本**
   - `scripts/bootstrap_cluster.sh`：生成 `.env` / `conf/new-api.env` / `zhentu_token`；
     `--check` 只读校验，`--up` 拉起。
   - `scripts/register_nodes.py`：JSON 清单批量注册，`--dry-run` / `--timeout`。
   - `scripts/collect_logs.py`：容器日志 + 节点 `/v1/logs` + 诊断快照。
4. **文档**
   - `docs/deployment.md`：从零复现主手册。
   - `docs/operations.md`：日常运维与故障处理。
   - `README.md`：新增碧游宫 / 阵图 / 一键部署 / 从零复现 / 已知限制，更新目录树与参考文档。
5. **验收与归档**
   - `docker compose config -q`
   - `bash scripts/bootstrap_cluster.sh --check`
   - `scripts/register_nodes.py --dry-run`
   - `bash scripts/verify.sh`；`.venv/bin/python -m pytest zhentu -q`
   - 写 `docs/reports/biyou-deploy-2026-11-21.md`
   - 更新蓝图 PRD 第五步进度
   - 按具体路径提交；`task.py archive 11-21-biyou-deploy --skip-branch-validation`

## 约束

- 不执行 `docker compose up/down`，不动现有容器与宿主机进程。
- 不修改 `nodes/**`、`zhentu/**` 的运行时逻辑。
- 不提交 `.env`、`conf/new-api.env`、`zhentu_token`。
