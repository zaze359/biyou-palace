# 设计 · 归档传道

## 1. 部署形态

中枢机（一台常开 PC）承载全部常驻控制面，边缘节点只跑推理与采集：

```
中枢机 Docker Compose（项目根 docker-compose.yml）
├── new-api       127.0.0.1:3000   碧游宫（include deploy/newapi）
├── zhentu        0.0.0.0:4020     阵图（本仓库镜像）
├── prometheus    0.0.0.0:9090     灵脉（include monitoring）
└── grafana       0.0.0.0:3001     灵脉看板

边缘节点（宿主机进程 / 安卓 App）
├── quasheng :4010 + Ollama :11434
└── sanxian  :4011（llama.cpp，adb reverse/forward）
```

## 2. 为什么用 compose include

`deploy/newapi/docker-compose.yml` 与 `monitoring/docker-compose.yml` 已分别被第二步、
第四步验证过。根 compose 用 `include` 复用它们，避免三份配置漂移；只有阵图是新增服务。

- `include` 需要 Compose ≥ 2.20；实测本机 5.7.0。
- 子文件内的相对路径按**子文件所在目录**解析，因此 NewAPI 的数据目录与 env_file 仍落在
  `deploy/newapi/` 下。
- 固定容器名（`new-api` 等）保持不变，便于脚本与文档引用。

## 3. 阵图镜像

`deploy/zhentu/Dockerfile`：

- 基础 `python:3.12-slim`，只复制 `zhentu/` 与 `requirements.txt`；
- 非 root 用户运行；
- SQLite 落 `/data/registry.db`，由命名卷 `zhentu-data` 持久化；
- 容器内 `ZHENTU_HOST=0.0.0.0`，对外发布端口由 `.env` 的 `ZHENTU_PORT` 控制；
- 连 NewAPI 用服务名 `http://new-api:3000`，不经过宿主机回环。

## 4. 从零复现的最小人工步骤

能脚本化的都脚本化，剩下三类必须人工：

1. NewAPI 首次管理员注册（镜像自身要求网页完成）；
2. 准圣安装 Ollama + 拉模型（体积大，且依赖 GPU/驱动）；
3. 散仙编译 APK + adb 推模型（依赖 Android 工具链与真机）。

`scripts/bootstrap_cluster.sh` 负责其余本地文件：

- 缺 `.env` 时从 `.env.example` 复制并生成随机 `ZHENTU_REGISTRY_TOKEN`、
  `NEWAPI_ADMIN_PASSWORD`；
- 缺 `deploy/newapi/conf/new-api.env` 时生成 `SESSION_SECRET` / `CRYPTO_SECRET`；
- 把 `ZHENTU_REGISTRY_TOKEN` 写入 `monitoring/prometheus/zhentu_token`（无换行）；
- `--check` 只校验不写；`--up` 写入后执行 `docker compose up -d`。

重复执行不覆盖任何已存在的文件。

## 5. 运维脚本边界

| 脚本 | 输入 | 输出 | 失败语义 |
|---|---|---|---|
| `register_nodes.py` | 节点清单 JSON | 每个节点注册结果表 | 任一失败退出码 1，不写部分状态以外的副作用 |
| `collect_logs.py` | 容器名 / 节点地址 | 输出目录（日志 + `summary.txt`） | 单个源失败记录到 summary，继续收集其它源 |

两者都只用标准库，避免运维机上还要装依赖。

## 6. 配置与密钥

- 根 `.env` 只放**中枢机编排**需要的非代码配置与 token；`.env.example` 入库，`.env` 被忽略。
- NewAPI 的 `SESSION_SECRET` / `CRYPTO_SECRET` 落在 `deploy/newapi/conf/new-api.env`
  （`conf/` 加入 `.gitignore`），由 bootstrap 生成。
- 灵脉抓取 token 文件同样忽略，只由 bootstrap 写入。
- 文档所有示例用 `<...>` 占位，不出现真实密钥。

## 7. 验证策略

不触碰正在运行的服务：

1. `docker compose config -q` 验证合并后的最终配置；
2. `bootstrap_cluster.sh --check` 在已有文件上幂等；
3. `register_nodes.py --dry-run` 验证清单解析；
4. `bash scripts/verify.sh` + `pytest zhentu` 验证没有回归；
5. `docker compose config` 输出中确认四个服务、端口与卷。

## 8. 已知限制（写入文档）

- 中枢机与网关单点；
- 灵脉抓取依赖 `host.docker.internal`，Linux 需改 targets；
- 散仙后台存活依赖系统策略；
- 阵图注册仍以静态清单 + 心跳为主，V1.1 再做自动入阵；
- 准圣 `fallback_source=registry` 与阵图结合属可选增强，不是默认路径。
