"""道鉴 · 长时耐久（D8）。

连续一段时长内混合负载（chat + embedding + health），周期性采样：
  - 进程是否存活 / health 是否可达（无崩溃 / 无 OOM 迹象）
  - resources.active 在负载停止后是否归零（无并发名额泄漏）
  - /v1/logs 是否出现 ERROR 堆积
  - /metrics 的 http 5xx 计数是否增长

默认 30 分钟；可用 --duration 缩短（冒烟验证用），--interval 控制采样周期。

用法：
    python3 scripts/daojian/soak.py [--duration 1800] [--interval 15]
        [--base-url URL] [--out PATH] [--strict]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CaseResult,
    Report,
    base_parser,
    default_out,
    exit_with,
    get,
    meta_from_args,
    post,
)


def parse_args() -> argparse.Namespace:
    p = base_parser("道鉴 · 长时耐久 D8")
    p.add_argument("--duration", type=int, default=1800, help="耐久时长（秒），默认 1800=30分钟")
    p.add_argument("--interval", type=int, default=15, help="健康采样间隔（秒）")
    p.add_argument("--cooldown", type=int, default=10, help="负载停止后等待 active 归零的秒数")
    return p.parse_args()


def chat(base: str, model: str, token: str, content: str) -> object:
    return post(f"{base}/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": content}],
                 "max_tokens": 16, "stream": False, "temperature": 0.7},
                token=token, timeout=60)


def embed(base: str, model: str, token: str, text: str) -> object:
    return post(f"{base}/v1/embeddings", {"model": model, "input": [text]}, token=token, timeout=60)


def error_count(base: str, token: str) -> int:
    try:
        data = get(f"{base}/v1/logs", token=token, timeout=5).json()
        return sum(1 for ln in data.get("lines", []) if str(ln.get("level", "")).upper() == "ERROR")
    except Exception:  # noqa: BLE001
        return -1


def http_5xx(base: str, token: str) -> float:
    try:
        txt = get(f"{base}/metrics", token=token, timeout=5).text()
        total = 0.0
        for line in txt.splitlines():
            if line.startswith("quasheng_http_requests_total") and 'status="5' in line:
                total += float(line.rsplit(" ", 1)[1])
        return total
    except Exception:  # noqa: BLE001
        return -1.0


def main() -> int:
    args = parse_args()
    r = Report("soak", env=meta_from_args(args))
    base, token = args.base_url, args.token
    c = CaseResult(
        "D8", "长时耐久",
        f"混合负载（chat+embedding+health）连续 {args.duration}s，每 {args.interval}s 采样健康/指标/日志",
        "无崩溃/OOM；负载停止后 active 归零；/v1/logs 无 ERROR 堆积",
    )
    errors_early = errors_late = 0
    samples: list[dict] = []
    round_i = 0
    request_results: dict[str, int] = {"chat_200": 0, "chat_fail": 0, "embed_200": 0, "embed_fail": 0}
    t0 = time.monotonic()
    try:
        if not get(f"{base}/v1/health", token=token, timeout=5).status == 200:
            c.measured = False
            c.notes.append("起始 health 不可达，跳过耐久")
            r.add(c)
            return exit_with(r, args.out or default_out("soak"), strict=args.strict)
        errors_early = max(error_count(base, token), 0)
        s5xx_early = max(http_5xx(base, token), 0.0)

        prompts = ["你好", "讲个笑话", "1+1等于几", "今天天气不错", "写一句诗"]
        next_sample = t0
        round_i = 0
        while time.monotonic() - t0 < args.duration:
            round_i += 1
            # 一批混合负载
            try:
                resp = chat(base, args.chat_model, token, random.choice(prompts))
                request_results["chat_200" if resp.status == 200 else "chat_fail"] += 1
            except Exception:  # noqa: BLE001
                request_results["chat_fail"] += 1
            try:
                resp = embed(base, args.embed_model, token, "耐久测试文本")
                request_results["embed_200" if resp.status == 200 else "embed_fail"] += 1
            except Exception:  # noqa: BLE001
                request_results["embed_fail"] += 1

            now = time.monotonic()
            if now >= next_sample:
                try:
                    h = get(f"{base}/v1/health", token=token, timeout=5).json()
                    sample = {
                        "t_s": round(now - t0, 1),
                        "status": h.get("status"),
                        "active": h.get("resources", {}).get("active"),
                        "mem_used_mb": h.get("resources", {}).get("mem_used_mb"),
                    }
                except Exception as exc:  # noqa: BLE001
                    sample = {"t_s": round(now - t0, 1), "health_error": f"{type(exc).__name__}: {exc}"}
                samples.append(sample)
                next_sample = now + args.interval

        elapsed = time.monotonic() - t0
        # 冷却后看 active 是否归零。注意：节点可能已崩溃，所有收尾探针都必须
        # 非抛异常，否则会把「节点崩溃」这一真实结论误标记为「未测」并丢失数据。
        time.sleep(args.cooldown)
        final_active: object = "unreachable"
        final_health_err = ""
        try:
            final_h = get(f"{base}/v1/health", token=token, timeout=5).json()
            final_active = final_h.get("resources", {}).get("active")
        except Exception as exc:  # noqa: BLE001
            final_health_err = f"{type(exc).__name__}: {exc}"
        errors_late = max(error_count(base, token), 0)
        s5xx_late = max(http_5xx(base, token), 0.0)

        c.data["elapsed_s"] = round(elapsed, 1)
        c.data["rounds"] = round_i
        c.data["request_results"] = request_results
        c.data["samples"] = samples
        c.data["final_active"] = final_active
        c.data["final_health_error"] = final_health_err
        c.data["errors_early"] = errors_early
        c.data["errors_late"] = errors_late
        c.data["http_5xx_delta"] = round(s5xx_late - s5xx_early, 2)

        health_errors = [s for s in samples if "health_error" in s]
        c.check("全程 health 可达（无崩溃/OOM 拒服）", not health_errors and not final_health_err,
                f"health 失败 {len(health_errors)} 次 / {len(samples)} 采样；收尾探针={final_health_err or 'ok'}")
        c.check("chat 请求无失败", request_results["chat_fail"] == 0,
                f"chat_fail={request_results['chat_fail']}")
        c.check("embedding 请求无失败", request_results["embed_fail"] == 0,
                f"embed_fail={request_results['embed_fail']}")
        c.check("负载停止后 active 归零", final_active in (0, 0.0, None),
                f"final active={final_active}")
        c.check("/v1/logs 无 ERROR 堆积", errors_late >= 0 and errors_early >= 0
                and errors_late - errors_early == 0,
                f"ERROR {errors_early} -> {errors_late}")
        c.check("无新增 5xx", s5xx_late >= 0 and s5xx_early >= 0 and s5xx_late - s5xx_early <= 0.5,
                f"5xx {s5xx_early} -> {s5xx_late}")
        if health_errors or final_health_err:
            first = next((s for s in samples if "health_error" in s), None)
            t_hint = first["t_s"] if first else "收尾阶段"
            c.notes.append(
                f"耐久期间节点失联（约 t={t_hint}s：{final_health_err or first['health_error']}）。"
                "按 D8 判据这是「进程崩溃/OOM 拒服」的实测失败，不是未测；已保留全部采样。"
            )
    except Exception as exc:  # noqa: BLE001
        # 仅当在到达耐久主循环前就失败（如起始探测异常）才算未测；
        # 若已产生采样/轮次，仍按已测处理并落盘现有数据。
        c.data.setdefault("rounds", round_i)
        c.data.setdefault("request_results", request_results)
        c.data.setdefault("samples", samples)
        c.notes.append(f"耐久异常中断：{type(exc).__name__}: {exc}")
        if not samples and round_i == 0:
            c.measured = False
        else:
            c.check("耐久过程未异常中断", False, c.notes[-1])
    r.add(c)
    out = args.out or default_out("soak")
    return exit_with(r, out, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
