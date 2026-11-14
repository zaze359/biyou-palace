# 阵图节点注册与心跳

## Goal

新增旁路 zhentu Registry（4020/SQLite）：节点注册、心跳租约、能力查询；准圣先接入并实测隔离，随后散仙接入；不改 NewAPI，不改节点入站契约。

## Requirements

- Registry 是独立 Python/FastAPI 进程，端口 4020，SQLite 持久化，与 NewAPI 同机并存；不修改 NewAPI。
- 端点：register / heartbeat / deregister / nodes / nodes/{id} / capabilities / candidates / health。
- 注册报文与 `docs/node-agent-contract.md` v1.0 的 `/v1/health` 对齐；能力挂在模型上，输出规范名 `embedding`，输入兼容 `embeddings`。
- 租约：准圣心跳 15s、散仙 30s；`ttl = clamp(3×interval, role_floor, 600)`，准圣 floor 90s、散仙 floor 180s；只用 Registry 服务器时钟判离线。
- 鉴权沿用 Bearer `shared_token` 体系，使用独立集群级 `ZHENTU_REGISTRY_TOKEN`；非回环监听且 token 为空时拒绝启动。
- 准圣新增可选 `registry` 配置与后台 reporter；阵图不可达不得影响启动耗时、本地 chat/embeddings/health 与节点状态。
- 准圣 health 仅在 `registry.enabled=true` 时追加 `registry` 块；关闭时输出与现状逐字段一致。
- 散仙新增 reporter，随前台服务启停；失败只记日志，不影响本地服务。
- 契约新增 v1.1「节点注册与心跳（出站）」章节；节点不新增入站端点。

## Acceptance Criteria

- [ ] Registry 单测覆盖鉴权、报文校验、upsert、心跳、租约过期、boot_id 冲突、models_rev 变更、注销、过滤、能力聚合、候选排序、持久化重启。
- [ ] 准圣 `registry.enabled=false` 时全部既有测试与行为不变。
- [ ] 准圣开启注册后能在 Registry 查到在线节点、模型、能力、租约。
- [ ] 阵图不可达时准圣启动耗时不增加，本地 chat / 流式 / embeddings / health 全部正常，节点状态仍为 ready。
- [ ] 杀阵图后重启，准圣自动重新注册；准圣停机后租约到期显示 offline。
- [ ] 散仙能在 JVM 测试中构造并发送注册/心跳报文，失败不影响本地节点。
- [ ] 契约 v1.1 文档与两端 README 更新。

## Notes

- P1 只做注册/心跳/查询；`fallback_source=registry` 留到 P2。
- 不做节点间直连回退：执行仍走 NewAPI，避免凭据扩散与绕过网关。
