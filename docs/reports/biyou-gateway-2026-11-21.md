# 道鉴 · 碧游宫网关接入实测报告

> 生成时间：2026-11-21 12:00 +0800　|　工具：`scripts/daojian/gateway_acceptance.py`　|　配置：`deploy/newapi/`
> 结论：**G1–G5 连续 3 次全绿（含清理用例 6/6）**；渠道同步幂等，网关选项已按基线生效。

## 一、背景与目的

第一步完成后，碧游宫（NewAPI）只有一条手工创建的 `ollama-local` 渠道，权重、重试、
自动隔离都停留在后台点选状态，无法复现。本任务把渠道与选项变成仓库内的声明式配置，
并用可重复的验收证明：统一入口、按模型路由、按权重分摊、故障自动隔离、基础并发。

## 二、环境

| 项 | 值 |
|---|---|
| 碧游宫 | `calciumion/new-api:v1.0.0-rc.48`，容器 `new-api`，映射 `127.0.0.1:3000` |
| 数据 | `/Users/zaze/biyou/newapi/data/one-api.db`（SQLite） |
| 准圣节点 | 准圣 Ollama `127.0.0.1:4010`（`qwen2.5:0.5b` / `qwen2.5:7b` / `nomic-embed-text`） |
| Ollama | `127.0.0.1:11434` |
| 客户端 key | 仓库 `.env` 的 `ZHENTU_NEWAPI_API_KEY`（与阵图共用，未入库） |
| 管理员凭据 | `~/biyou/newapi/conf/credentials.env`（仓库外） |

## 三、渠道与选项的前后对比

### 3.1 渠道（`channel sync` 后）

| 名称 | type | base_url | models | 权重 | 状态 |
|---|---|---|---|---|---|
| `ollama-local` | 4（Ollama） | `http://host.docker.internal:11434` | qwen2.5:0.5b / 7b / nomic-embed-text | 1 | 1 |
| `quasheng-01` | 1（OpenAI 兼容） | `http://host.docker.internal:4010` | 同上 + 别名 `quasheng/qwen2.5:7b` | 1 | 1 |

- 准圣渠道的 `model_mapping`：`quasheng/qwen2.5:7b` → `qwen2.5:7b`，用于「按模型路由」。
- 两条渠道都声明 `qwen2.5:7b`，用于「同模型多副本分摊」。
- 第二次 `channel sync` 输出全部 `unchanged`，幂等成立。

### 3.2 网关基线选项（`option apply` 后）

| 键 | 原值 | 基线 |
|---|---|---|
| `RetryTimes` | 0 | 1 |
| `ChannelDisableThreshold` | 5 | 5 |
| `AutomaticDisableChannelEnabled` | false | true |
| `AutomaticEnableChannelEnabled` | false | false |
| `AutomaticDisableStatusCodes` | 401 | 401,403,500,502,503,504 |

> 429 被**故意排除**在自动隔离之外：它是上游限流的暂时背压，应重试而不是摘除渠道。
> 这一点由实测触发（见第五节）。

## 四、用例与结果

三次独立运行（11:55:47 / 11:56:13 / 11:56:34）均为 6/6：

| 编号 | 场景 | 判据 | 三次结果 |
|---|---|---|---|
| G1 | 统一入口 | `/v1/models` 200 且含 `qwen2.5:7b` | 通过 ×3 |
| G2 | 按模型路由 | 别名请求 200，日志渠道仅 `quasheng-01` | 200 ×3（0.659/0.661/0.663s） |
| G3 | 权重分摊 | 声明 23:1（预测 75%），60 次日志计数 | 68.3% / 75.0% / 76.7% |
| G4 | 故障隔离 | 桩上游关停后渠道 `status=3`，其它模型仍 200 | 通过 ×3 |
| G5 | 基础并发 | 16 请求 / 并发 4，全部 2xx | 16/16 ×3，p50 2.58–2.60s，p95 2.59–2.65s |
| C1 | 清理 | 无 `biyou-verify-*` 残留，选项已恢复 | 通过 ×3 |

补充数据：

- G3 每轮 60 次请求全部 2xx，日志按 `request_id` 去重后 60 条；重/轻渠道计数分别为
  `41:19`、`45:15`、`46:14`。
- G4 关停桩上游后，请求返回 `500`（`upstream error: do request failed`），
  渠道在 30 秒轮询内变为 `status=3`；同时主模型请求仍为 `200`。
- G5 使用临时 `biyou-verify-conc` 渠道（type=4，别名映射到 `qwen2.5:7b`），
  跑完即删，避免准圣节点并发名额干扰网关并发结论。

## 五、实测暴露的两个关键事实

### 1. NewAPI 的「权重」不是直觉占比

`/api/option/` 没有内存缓存相关开关时，渠道选择走数据库路径，占比公式为：

```
P(渠道 i) = (weight_i + 10) / Σ(weight_j + 10)
```

因此声明 3:1 的实际占比是 `13/24 = 54.2%`，而不是 75%。首轮实测 32:28（53.3%）验证了这一点。
要让目标流量为 3:1，需声明 `23:1`（`33/44 = 75%`），验收脚本已按此构造并记录
`predicted_heavy_ratio=0.75`。若后续开启内存缓存，权重语义会变化，需要重新标定。

### 2. 把 429 纳入自动隔离会误杀健康节点

准圣节点 `max_concurrency=2`，16 请求 / 并发 4 时出现 1 个 429。首版基线把 429 列入
`AutomaticDisableStatusCodes`，累计到阈值后 `quasheng-01` 被自动禁用（`status=3`），
下一次运行 G2 直接 `503 model_not_found`。这与「限流是暂时背压」的语义冲突。
基线已改为只隔离 `401/403/5xx`，429 交给重试；改动后三次运行均稳定。

## 六、限制与未测项

1. **G5 并发固定到 Ollama 渠道**。准圣节点并发上限为 2，异构 16/4 并发会按设计返回 429；
   本用例只验证网关并发与 Ollama 承载，异构分摊由 G3（60 次双渠道）覆盖。
2. **单机单实例**。未验证多 worker / 多实例下的渠道缓存一致性；NewAPI 当前为单容器部署。
3. **未测流式**。G1–G5 都用非流式短请求；SSE 断流、长连接超时不在本任务范围。
4. **未验证自动恢复**。`AutomaticEnableChannelEnabled=false`，被隔离渠道需 `channel sync`
   或后台手动恢复；自动恢复留给 V1.1。
5. **管理员会话清理**。探测期间产生了 48 个 `Python-urllib/3.9` 登录会话，触达 NewAPI
   默认 50 的上限；已仅撤销这些探测会话，保留真实浏览器会话。这不是网关配置问题，
   但提示脚本化验收应复用 `NEWAPI_ADMIN_TOKEN`，避免反复登录。

## 七、复现

```bash
# 1) 渠道同步（幂等；第二次应全部 unchanged）
export NEWAPI_ADMIN_USERNAME=... NEWAPI_ADMIN_PASSWORD=...
python3 deploy/newapi/gateway_admin.py channel sync --dry-run
python3 deploy/newapi/gateway_admin.py channel sync

# 2) 网关基线选项（显式应用；channel sync 不会自动改选项）
python3 deploy/newapi/gateway_admin.py option apply --dry-run
python3 deploy/newapi/gateway_admin.py option apply

# 3) 验收（客户端 key 走 .env；管理员建议用 NEWAPI_ADMIN_TOKEN）
set -a; . ./.env; set +a
python3 scripts/daojian/gateway_acceptance.py --strict

# 4) 回归
.venv/bin/python -m pytest zhentu -q
.venv/bin/python scripts/daojian/orchestration_e2e.py --strict
```

本次三次运行的原始结果在 `scripts/daojian/out/gateway*.json`（本地，未入库）。

## 八、回归

- `pytest zhentu -q`：**76 通过**。
- `scripts/daojian/orchestration_e2e.py --strict`（桩模式）：**7/7 通过**。
- 临时渠道与选项清理：三次运行 C1 均通过，结束后 `channel list` 仅剩两条声明渠道。
