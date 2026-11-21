# 实施 · 碧游宫网关接入

## 步骤

1. `deploy/newapi/gateway_admin.py`
   - 鉴权：`NEWAPI_ADMIN_TOKEN` → 用户名/密码登录 → refresh cookie 文件。
   - 子命令：`channel list|sync|test|delete`、`option get|set|apply`、`log list`。
   - 更新 payload 白名单剔除 `status`；删除默认只允许 `biyou-verify-` 前缀。
   - 自检：`python3 deploy/newapi/gateway_admin.py channel list`、`option get`。

2. `deploy/newapi/channels.json`
   - `ollama-local`（type=4）与 `quasheng-01`（type=1，含别名 `quasheng/qwen2.5:7b`）。
   - 两条都声明 `qwen2.5:7b`；准圣别名走 `model_mapping`。

3. `deploy/newapi/gateway-options.json`
   - 五项基线：重试 1、阈值 5、自动隔离开、自动恢复关、可重试状态码集合。

4. `deploy/newapi/docker-compose.yml` + `.env.example`
   - 与线上容器一致的镜像、端口、数据目录、`host.docker.internal` 映射；
   - `.env.example` 不含真实值，覆盖管理员三路鉴权与客户端 key。

5. `scripts/daojian/gateway_acceptance.py`
   - G1 统一入口、G2 指定渠道转发、G3 权重 3:1（60 次日志计数）、
     G4 桩上游关停后自动隔离、G5 16/4 并发。
   - 临时渠道前缀 `biyou-verify-`，`finally` 删除；选项快照并恢复。

6. 实跑
   - `python3 deploy/newapi/gateway_admin.py channel sync --dry-run` → 实际 sync；
   - `python3 deploy/newapi/gateway_admin.py option apply --dry-run` → 实际 apply；
   - `python3 scripts/daojian/gateway_acceptance.py --json scripts/daojian/out/gateway.json --strict`；
   - `pytest zhentu -q`；`python3 scripts/daojian/orchestration_e2e.py --strict`（桩模式）。

7. 报告与进度
   - `docs/reports/biyou-gateway-2026-11-21.md`：环境、渠道前后对比、G1–G6 实测、限制；
   - 蓝图 PRD 第二步补进度；
   - 按具体路径提交（不提交 out/、临时文件、任何密钥）。

## 完成定义

- [ ] `channel sync` 幂等：第二次运行全部 `unchanged`。
- [ ] `option apply` 后回读值与声明一致。
- [ ] G1–G5 全通过，报告含真实数字。
- [ ] G6 回归全绿。
- [ ] 临时渠道与选项全部清理/恢复，仓库无密钥。
