# 准圣 P1 模型生命周期管理（CLI/脚本层）

## Goal

在不新增 HTTP 管理端点的前提下，为 quasheng CLI 增加模型生命周期子命令，覆盖列出、拉取、离线导入、删除、卸载，并补齐预检、可读错误与离线导入文档。

契约依据：`docs/node-agent-contract.md` 第五章明确「不含模型 / 权重管理 API」，因此本任务只改 CLI 与本地文档，不新增任何 HTTP 端点。

## Requirements

### 命令清单

| 命令 | 作用 |
|---|---|
| `python -m nodes.quasheng.cli list` | 列出已安装模型，标出 `/api/ps` 中已加载的模型 |
| `python -m nodes.quasheng.cli pull <model>` | 拉取模型，失败给出可读原因 |
| `python -m nodes.quasheng.cli import <gguf> [--name <model>]` | 本地 GGUF → `ollama create`，固化 Modelfile 流程 |
| `python -m nodes.quasheng.cli delete <model> [--yes]` | 删除模型，默认交互确认 |
| `python -m nodes.quasheng.cli unload <model>` | `keep_alive=0` 卸载模型，释放内存 |

### 行为约束

- 全部命令直连本机 Ollama 原生 `/api/*`（管理面没有 OpenAI `/v1` 等价端点），仍使用 `trust_env=False`，不走 `HTTP(S)_PROXY`。
- 拉取/导入前做磁盘与内存预检：模型体积取远程 manifest 层大小（拉取）或 GGUF 文件大小（导入）；不足时拒绝并说明需要多少、可用多少。
- 任一步失败返回非零退出码，错误信息包含步骤与模型名。
- 参数风格沿用现有 `--base/--token/--model/--once`，管理命令使用 `--ollama` 指定 Ollama 地址（默认读 `QUASHENG_OLLAMA_BASE`）。
- 不新增 HTTP 管理端点；不改契约。

### 文档

`nodes/quasheng/README.md` 增加「模型生命周期管理（CLI）」与「离线 GGUF 导入」两节，包含 GGUF 来源、Modelfile 写法、`ollama create` 参数、导入后验证步骤。

## Acceptance Criteria

- [x] 五条命令可执行，成功路径输出清晰，失败路径非零退出且原因可读。
- [x] 磁盘/内存不足时在调用 pull/create 之前拒绝，消息包含所需与可用空间。
- [x] 删除默认要求确认；`--yes` 跳过确认；拒绝确认时不删除并返回非零。
- [x] mock Ollama 单测覆盖：list 成功、pull 成功/空间不足/上游报错、import 成功/文件缺失、delete 确认/拒绝/上游报错、unload 成功/报错。
- [x] 现有 CLI 行为（`--once`、REPL）与全量 pytest 不回归。
- [x] README 离线导入流程完整可照做。

## Notes

- 管理命令只面向本机运维，不经节点鉴权；`--token` 仅用于对话端点。
- P1 第一阶段不包含 HTTP 管理端点、模型下载队列、模型能力自动探测。
