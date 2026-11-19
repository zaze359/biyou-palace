# 道鉴：压测与故障注入检定

> 父任务：`10-13-biyou-v1-blueprint`（第四步「灵脉监控 + 道鉴检定」的剩余部分）
> 契约：`docs/node-agent-contract.md` v1.2 · 真机：XQ-AT72（Android 12）
> 目标：给节点与阵图一套可复现的压测 + 故障注入 + 长时耐久检定，产出 Markdown 报告。
> 非目标：对比不同模型质量、调优 llama.cpp 本身、改任何 `/v1` 契约行为。

## 一、现状

- 灵脉（监控）已完成并归档：三源 `/metrics`、Prometheus/Grafana、6 条告警。
- 道鉴完全空缺：无压测脚本、无故障注入用例、无检定报告。
- 本机可测对象：准圣 `:4010`（Ollama qwen2.5:0.5b）、散仙真机 `:4031`（adb forward 14011）、
  阵图 `:4020`（Bearer）、NewAPI `:3000`（渠道配置未知，归第二阶段）。

## 二、检定矩阵

| 编号 | 场景 | 方法 | 判据 |
|---|---|---|---|
| D1 | 节点并发上限 | 并发 1/2/3… 发 chat（`max_tokens` 固定） | `max_concurrency` 内全 200；超限立即 429 + `Retry-After`，无排队 |
| D2 | 达标时延 | 单请求各取 P50/P95（业务量 N≥20） | 记录真实数值，不设硬指标（小模型 + 手机） |
| D3 | 流式首字节 | 流式请求记录首 content 分片时间 | 与 `/metrics` 的 `first_byte_seconds` 直方图一致 |
| D4 | 进程停止 | SIGTERM/SIGKILL 节点 → 探活 | 端口立即拒连；看护/告警可见；重启后恢复 |
| D5 | 降载注入 | `thermalservice override-status 3` | 新请求 503 `thermal_shedding` + `Retry-After`；在途不中断；`reset` 后恢复 |
| D6 | 能力缺失 | 对 chat 模型请求 embeddings | 501 `capability_not_supported`，不计入模型成功计数 |
| D7 | 阵图退路 | 停阵图 → 准圣 `fallback_source=registry` 请求 | 阵图不可达只影响本次选路，自动退回静态链，chat 不失败 |
| D8 | 长时耐久 | 连续 30 分钟混合负载（chat+embedding+health） | 无进程崩溃/无 OOM；`active` 归零；`/v1/logs` 无 ERROR 堆积 |

## 三、交付物

- `scripts/daojian/`：
  - `stress_nodes.py`：按矩阵 D1/D2/D3/D6 施压并采集（纯标准库 + 线程池，不引入依赖）
  - `inject_faults.py`：D4/D5/D7 的故障注入与恢复脚本（调用 adb 与本机进程控制）
  - `soak.py`：D8 长时耐久
  - `report.py`：把 JSON 结果渲染成 `docs/reports/daojian-<date>.md`
- `docs/reports/daojian-*.md`：一次真实检定报告（含环境、命令、数据、结论）
- 各脚本支持 `--base-url` / `--token` / `--duration` 参数，默认值对应本机现状。

## 四、验收标准

- [ ] 四条脚本可在本机一条命令跑完，且重复执行结果可复现（同参数同结论级别）。
- [ ] D1–D8 每项都有实际测量数据写入报告（不允许「未测」占位，测不了的要写明原因）。
- [ ] 故障注入后系统状态可恢复，脚本退出码反映是否恢复成功。
- [ ] 报告给出结论与已知限制（手机算力、单测环境、未覆盖的网络故障）。
- [ ] 不改任何节点/阵图代码；若发现缺陷，另开任务修复。

## 五、风险

- 手机压测发热会触发降载，可能污染 D1/D2 数据；用例需先读 `thermal_shedding`，降载中暂停并记录。
- 本机准圣只有 0.5b 小模型，绝对时延不代表线上 7B 表现，报告需标注。
- 项目根有 `.venv` 但道鉴脚本只用标准库，避免为检定引入新依赖。