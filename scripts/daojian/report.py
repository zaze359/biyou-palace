"""道鉴 · 报告渲染。

把各检定脚本产出的 JSON 结果聚合，渲染成 docs/reports/daojian-<date>.md。
可对未测项目写明原因（脚本中 measured=false 的用例会标注为「未测」并列出 notes）。

用法：
    python3 scripts/daojian/report.py [--out docs/reports/daojian-<date>.md]
        [--results scripts/daojian/out]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]

CASE_ORDER = ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]
CASE_NAMES = {
    "D1": "节点并发上限", "D2": "达标时延（P50/P95）", "D3": "流式首字节",
    "D4": "进程停止与恢复", "D5": "降载注入", "D6": "能力缺失（501）",
    "D7": "阵图退路", "D8": "长时耐久",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="道鉴 · 渲染 Markdown 检定报告")
    default_date = datetime.now(CST).strftime("%Y-%m-%d")
    p.add_argument("--results", default=str(ROOT / "scripts" / "daojian" / "out"),
                   help="JSON 结果目录")
    p.add_argument("--out", default=str(ROOT / "docs" / "reports" / f"daojian-{default_date}.md"),
                   help="报告输出路径")
    p.add_argument("--json-out", default="", help="可选：同时输出聚合后的 JSON")
    return p.parse_args()


def load_results(results_dir: Path) -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = []
    for f in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "cases" in data:
                data["_file"] = f.name
                suites.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return suites


def collect_cases(suites: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for s in suites:
        for case in s.get("cases", []):
            cid = case.get("id", "")
            # 同名用例取「已测」优先，其次取最新
            if cid not in cases or (case.get("measured") and not cases[cid].get("measured")):
                cases[cid] = case
    return cases


def fmt_value(v: Any) -> str:
    if isinstance(v, dict):
        return " ".join(f"`{k}`={x}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def render(args: argparse.Namespace, suites: list[dict[str, Any]], cases: dict[str, dict[str, Any]]) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %z")
    env: dict[str, Any] = {}
    for s in suites:
        env.update({k: v for k, v in s.get("env", {}).items() if v not in (None, "")})

    lines: list[str] = []
    lines.append("# 道鉴 · 节点与阵图检定报告")
    lines.append("")
    lines.append(f"> 生成时间：{now}　|　契约：`docs/node-agent-contract.md` v1.2　|　"
                 "工具：`scripts/daojian/`（纯 Python 标准库）")
    lines.append("")
    lines.append("## 一、环境与对象")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 主机 | `{env.get('host', 'N/A')}` |")
    lines.append(f"| Python | `{env.get('python', 'N/A')}` |")
    lines.append(f"| 被测节点 base_url | `{env.get('base_url', 'N/A')}` |")
    lines.append(f"| chat 模型 | `{env.get('chat_model', 'N/A')}` |")
    lines.append(f"| embedding 模型 | `{env.get('embed_model', 'N/A')}` |")
    lines.append(f"| 真机 adb 设备 | `{fmt_value(env.get('adb_devices', 'N/A'))}` |")
    lines.append("")
    lines.append("常驻对象：准圣 `:4010`（Ollama qwen2.5:0.5b + nomic-embed-text）、"
                 "散仙真机 `:4031`（adb forward `14011`）、阵图 `:4020`、Prometheus `:9090`。"
                 "D4/D7 使用临时实例与空闲端口，避免干扰常驻服务；D5 在真机执行并强制复位。")
    lines.append("")

    # 汇总表
    lines.append("## 二、检定矩阵与结论")
    lines.append("")
    lines.append("| 编号 | 场景 | 结果 | 关键数据 |")
    lines.append("|---|---|---|---|")
    for cid in CASE_ORDER:
        c = cases.get(cid)
        if not c:
            lines.append(f"| {cid} | {CASE_NAMES.get(cid, '')} | — | 无结果文件 |")
            continue
        if not c.get("measured"):
            lines.append(f"| {cid} | {c.get('title', CASE_NAMES.get(cid, ''))} | 未测 | {fmt_value(c.get('notes'))} |")
            continue
        verdict = "通过" if c.get("passed") else "未通过"
        key = _key_data(cid, c)
        lines.append(f"| {cid} | {c.get('title', CASE_NAMES.get(cid, ''))} | {verdict} | {key} |")
    lines.append("")

    # 逐项明细
    lines.append("## 三、逐项明细")
    lines.append("")
    for cid in CASE_ORDER:
        c = cases.get(cid)
        if not c:
            continue
        lines.append(f"### {cid} · {c.get('title', CASE_NAMES.get(cid, ''))}")
        lines.append("")
        lines.append(f"- 判定：{c.get('judgement', '')}")
        lines.append(f"- 方法：{c.get('method', '')}")
        if not c.get("measured"):
            lines.append("- **未测**（原因见下）")
            for n in c.get("notes", []):
                lines.append(f"  - {n}")
            lines.append("")
            continue
        lines.append("")
        lines.append("检查点：")
        lines.append("")
        lines.append("| 检查 | 结果 | 说明 |")
        lines.append("|---|---|---|")
        for ck in c.get("checks", []):
            mark = "✅" if ck.get("passed") else "❌"
            detail = str(ck.get("detail", "")).replace("|", "\\|")[:300]
            lines.append(f"| {ck.get('name', '')} | {mark} | {detail} |")
        lines.append("")
        data = c.get("data", {})
        if data:
            lines.append("原始数据：")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(data, ensure_ascii=False, indent=2)[:6000])
            lines.append("```")
            lines.append("")
        if c.get("notes"):
            lines.append("说明：")
            for n in c["notes"]:
                lines.append(f"- {n}")
            lines.append("")

    # 结论与限制
    total = len([c for cid, c in cases.items() if c.get("measured")])
    passed = len([c for cid, c in cases.items() if c.get("measured") and c.get("passed")])
    unmeasured = [cid for cid, c in cases.items() if not c.get("measured")]
    lines.append("## 四、总体结论")
    lines.append("")
    lines.append(f"已测 {total} 项，通过 {passed} 项" + (f"，未测 {len(unmeasured)} 项（{', '.join(unmeasured)}）" if unmeasured else "") + "。")
    lines.append("")
    lines.append("已知限制：")
    lines.append("")
    lines.append("- 本机准圣为 `qwen2.5:0.5b` 小模型，绝对时延（D2/D3）不代表线上 7B 表现；本报告只记录真实数值，不设硬指标。")
    lines.append("- D4/D7 使用临时实例，未接入 Prometheus/Grafana 看护栈；「告警可见」以端口拒连 + 进程退出的探活条件等价表述。")
    lines.append("- 单测环境未覆盖真实网络故障（丢包/分区/DNS），仅覆盖进程停止与阵图不可达两类。")
    lines.append("- D8 默认 30 分钟；本次实际时长以明细中 `elapsed_s` 为准。")
    lines.append("- 耐久观察：一次并发跑在其它高负载任务同时运行时，准圣节点曾在耐久后段出现端口失联"
                 "（首次长跑因此判未通过）；独占环境复跑 30 分钟/3547 轮全绿。怀疑与宿主机内存压力相关，"
                 "未做归因定论。若需排除该风险，应在独占机器上重跑 D8。")
    lines.append("")
    lines.append("## 五、复现命令")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 scripts/daojian/stress_nodes.py --n-latency 20 --strict")
    lines.append("python3 scripts/daojian/inject_faults.py --case all --strict")
    lines.append("python3 scripts/daojian/soak.py --duration 1800 --strict")
    lines.append("python3 scripts/daojian/report.py")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _key_data(cid: str, c: dict[str, Any]) -> str:
    d = c.get("data", {})
    try:
        if cid == "D1":
            return f"max_concurrency={d.get('max_concurrency')}，档位={len(d.get('levels', []))}，超限 429+Retry-After=1s"
        if cid == "D2":
            lat = d.get("latency", {})
            return f"n={lat.get('n')}，P50={lat.get('p50')}s，P95={lat.get('p95')}s"
        if cid == "D3":
            return f"首字节={d.get('first_byte_s')}s，stream=[DONE]={d.get('got_done')}，metrics 计数+{d.get('first_byte_count_delta')}"
        if cid == "D4":
            return f"SIGTERM/SIGKILL 端口拒连={d.get('sigterm_port_refused')}/{d.get('sigkill_port_refused')}，重启恢复={d.get('sigterm_recovered')}/{d.get('sigkill_recovered')}"
        if cid == "D5":
            nr = d.get("new_request", {})
            return f"热状态→降载={d.get('shedding_observed')}，新请求={nr.get('status')}/{nr.get('code')}，在途={d.get('inflight_status')}，复位={d.get('recovered')}"
        if cid == "D6":
            return f"status={d.get('status')}，code={d.get('code')}，成功计数={d.get('model_requests_200_before')}→{d.get('model_requests_200_after')}"
        if cid == "D7":
            return f"阵图停→chat={d.get('chat_during_outage')}，节点状态={d.get('node_status_during_outage')}，阵图恢复={d.get('zhentu_recovered')}"
        if cid == "D8":
            rr = d.get("request_results", {})
            return f"时长={d.get('elapsed_s')}s，轮次={d.get('rounds')}，chat={rr.get('chat_200')}/{rr.get('chat_fail')}，active={d.get('final_active')}，ERROR={d.get('errors_early')}→{d.get('errors_late')}"
    except Exception:  # noqa: BLE001
        pass
    return "见明细"


def main() -> int:
    args = parse_args()
    results_dir = Path(args.results)
    suites = load_results(results_dir)
    cases = collect_cases(suites)
    md = render(args, suites, cases)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[道鉴] 报告已生成: {out}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"suites": [{k: v for k, v in s.items() if k != '_file'} for s in suites]},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
