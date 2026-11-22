# 已归档 · 仅作参考实现

> 状态：V1.0 山门（NewAPI / 网关）部署配置，自 2026-11-22 起不再演进。
> 判定依据：`.trellis/tasks/11-21-biyou-superagent/prd.md`「服务端不是独立交付物；旧集群降级为联阵后端 / 参考实现」；
> V2 单机首版明确「无 NewAPI 依赖」。

## 保留原因

模型转发、权重、provider 隔离的配置形态可供 V2 参考；V2 的模型面由
dsh 的 provider 机制与 `@biyou/dsh-local-models` 承担。

## 约定

- 不新增部署形态；仅在修复阻塞性缺陷时可改。
- 中枢机一键编排入口见根目录 `start.sh legacy cluster`。
