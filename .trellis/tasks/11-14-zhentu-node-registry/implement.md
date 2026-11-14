# 实施计划

## 顺序

1. `zhentu/` Registry：config、models、store、app、tests、run script。
2. 准圣：config、registry reporter、lifespan/health 接入、tests。
3. 真实进程验证：注册 → 查询 → 杀阵图 → 本地 chat/embeddings/health → 重启阵图自动重注册。
4. 散仙：NodeConfig、NodeRegistryReporter、前台服务接入、JVM tests。
5. 文档：契约 v1.1、两端 README、config 示例。

## 校验命令

```bash
# Registry
.venv/bin/python -m pytest zhentu/tests -q
# 准圣全量
.venv/bin/python -m pytest nodes/quasheng/tests -q
# 准圣 smoke
bash scripts/smoke_quasheng.sh
# 散仙 JVM 测试
cd nodes/sanxian && ./gradlew :shared:testDebugUnitTest
```

## 实时验证

```bash
export ZHENTU_REGISTRY_TOKEN=dev-token
bash scripts/run_zhentu.sh &
QUASHENG_CONFIG=... bash scripts/run_quasheng.sh &
curl -s -H "Authorization: Bearer dev-token" http://127.0.0.1:4020/zhentu/v1/nodes
# 杀 Registry，验证准圣本地 chat/health 正常；重启 Registry，等待自动重注册
```

## 回滚

- 节点 `registry.enabled=false` 即完全恢复现状；Registry 可独立停掉，不影响 NewAPI 与节点。
- SQLite 文件可直接删除；无节点侧迁移。
