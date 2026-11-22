# 已归档 · 仅作参考实现

> 状态：V1.0 内门仙徒节点（旧名「准圣」），自 2026-11-22 起不再演进。
> 判定依据：`.trellis/tasks/11-21-biyou-superagent/prd.md`「服务端不是独立交付物；旧集群降级为联阵后端 / 参考实现」；
> `docs/naming.md`「旧代码 `quasheng` 仅作兼容」「目录重命名不在 V2.0-A 范围」。

## 保留原因

- **兼容名**：`quasheng` 作为旧路径别名继续存在，`start.sh legacy quasheng` 仍可启动。
- **参考实现**：Ollama 探测、OpenAI 兼容端点、降级策略可供 V2 桌面端本地模型插件参考。

## 当前方向

V2.0-A 单机首版 = 纯 dsh 原生 Agent，**不依赖本节点**；桌面端模型能力由
`@biyou/dsh-local-models` 插件提供。联阵模式下本节点可作为可选后端，但不是默认路径。

## 约定

- 不新增功能，不重构；仅在修复阻塞性缺陷时可改。
- 新功能请写到 V2 对应位置（桌面端插件 / 联阵 FormationHost）。
- 启动入口见根目录 `start.sh legacy`。
