# 已归档 · 仅作参考实现

> 状态：V1.0 外门仙徒节点（旧名「散仙」，Kotlin + llama.cpp Android），自 2026-11-22 起不再演进。
> 判定依据：`.trellis/tasks/11-21-biyou-superagent/prd.md`「服务端不是独立交付物；旧集群降级为联阵后端 / 参考实现」；
> `docs/naming.md`「旧代码 `sanxian` 仅作兼容」「目录重命名不在 V2.0-A 范围」。

## 保留原因

- **兼容名**：`sanxian` 作为旧路径别名继续存在，`start.sh legacy sanxian` 仍可启动。
- **参考实现**：llama.cpp JNI 集成、GGUF 加载、移动端推理链路可供后续移动端参考
  （移动端新规划见 `.trellis/tasks/11-21-mobile-client`）。

## 当前方向

V2.0-A 桌面端**不做移动端 App**；移动端是独立后置任务。桌面端本地模型由
`@biyou/dsh-local-models` 插件提供。

## 约定

- 不新增功能，不重构；仅在修复阻塞性缺陷时可改。
- `third_party/llama.cpp` 为上游源码，不要本地修改。
- 启动入口见根目录 `start.sh legacy`。
