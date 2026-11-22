# 部分归档 · 仅作参考实现

> 状态：V1.0 阵图（编排服务），自 2026-11-22 起不再演进。
> 判定依据：`.trellis/tasks/11-21-desktop-client/design.md`
> 「`docs/agent-contract.md` 与 `zhentu` 任务板/账本保留，作为联阵或后续任务治理的契约与参考实现，不在单机首版接线」。

## 保留部分（明确继续保留）

- `taskboard.py` / `taskboard_api.py` / `task_models.py`：任务板参考实现
- `ledger.py` / `costs.py`：香火账本与成本公式参考实现
- `tests/test_taskboard.py` 等对应测试

这些是 V2 任务治理（`.trellis/tasks/11-21-taskboard-cost`）的契约参考，**不要删除**。

## 归档部分（仅作参考，不再演进）

- `route.py` / `registry` / `orchestration.py` / `metrics.py` / `runner.py`：Python 编排与选路
- V2 联阵由 Node 侧的 `desktop/electron/lan-host/` 实现，不再走 Python 编排。

## 约定

- 不新增功能；任务板/账本只在 V2 任务治理真正接线时参考或迁移，不在此处继续开发。
- 启动入口见根目录 `start.sh legacy zhentu`。
