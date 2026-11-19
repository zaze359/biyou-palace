"""道鉴 · 节点压测（D1/D2/D3/D6）。

D1 节点并发上限：并发 1/2/3… 发 chat，验证 max_concurrency 内全 200、超限立即
    429 + Retry-After、无排队。
D2 达标时延：单请求串行 N≥20，记录 P50/P95（小模型+手机，不设硬指标）。
D3 流式首字节：流式请求记录首 content 分片时间，并与 /metrics 的
    first_byte_seconds 直方图计数一致性核对。
D6 能力缺失：对 chat 模型请求 embeddings，验证 501 capability_not_supported
    且不计入模型成功计数（model_requests_total status=200 不增长）。

用法：
    python3 scripts/daojian/stress_nodes.py [--base-url URL] [--token TOKEN]
        [--concurrency N] [--n-latency N] [--out PATH] [--strict]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    metric_sum,
    post,
    summarize,
)


def parse_args() -> argparse.Namespace:
    p = base_parser("道鉴 · 节点压测 D1/D2/D3/D6")
    p.add_argument("--concurrency", type=int, default=0, help="D1 最大并发档位；默认取 health 的 max_concurrency+2")
    p.add_argument("--n-latency", type=int, default=20, help="D2 串行样本数（≥20）")
    p.add_argument("--warmup", type=int, default=1, help="正式打点前的预热请求数")
    return p.parse_args()


def health(base: str, token: str) -> dict:
    r = get(f"{base}/v1/health", token=token, timeout=10)
    return r.json()


def chat(base: str, model: str, token: str, *, max_tokens: int, stream: bool, content: str = "hi"):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "stream": stream,
        "temperature": 0.0,
    }
    return post(f"{base}/v1/chat/completions", payload, token=token, timeout=120)


def metrics_text(base: str, token: str) -> str:
    return get(f"{base}/metrics", token=token, timeout=10).text()


def case_d1(args, r: Report) -> None:
    base, token = args.base_url, args.token
    c = CaseResult(
        "D1", "节点并发上限",
        "并发 1..N 同时发 chat（max_tokens=8），记录各档 200/429 与 Retry-After",
        "max_concurrency 内全 200；超限立即 429 + Retry-After，无排队",
    )
    try:
        h = health(base, token)
        limit = int(h["limits"]["max_concurrency"])
        c.data["max_concurrency"] = limit
        top = args.concurrency or (limit + 2)
        c.data["tested_levels"] = list(range(1, top + 1))

        def one(level: int) -> dict:
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=level) as ex:
                futs = [ex.submit(chat, base, args.chat_model, token, max_tokens=8, stream=False) for _ in range(level)]
                rs = [f.result() for f in as_completed(futs)]
            statuses = [r.status for r in rs]
            retries = [r.header("Retry-After") for r in rs if r.status == 429]
            return {
                "level": level,
                "statuses": sorted(statuses),
                "n_200": statuses.count(200),
                "n_429": statuses.count(429),
                "retry_after": retries,
                "wall_s": round(time.perf_counter() - t0, 3),
            }

        results = []
        for level in range(1, top + 1):
            results.append(one(level))
            time.sleep(0.5)  # 让在途请求释放名额，避免档位互相污染
        c.data["levels"] = results

        over = [r for r in results if r["level"] > limit]
        within = [r for r in results if r["level"] <= limit]
        c.check("限额内全部 200", all(r["n_200"] == r["level"] for r in within),
                f"档位 {[ (r['level'], r['statuses']) for r in within ]}")
        c.check("超限立即 429（无排队）", all(r["n_200"] <= limit and r["n_429"] >= 1 for r in over),
                f"档位 {[ (r['level'], r['n_200'], r['n_429']) for r in over ]}")
        c.check("429 携带 Retry-After",
                all(x is not None for r in over for x in r["retry_after"])
                and all(len(r["retry_after"]) == r["n_429"] for r in over if r["n_429"] > 0),
                f"retry_after={[ r['retry_after'] for r in over ]}")
        c.check("超限无排队（响应即时）", all(r["wall_s"] < 30 for r in results),
                "所有档位在 30s 内返回，未出现排队等待")
    except Exception as exc:  # noqa: BLE001
        c.measured = False
        c.notes.append(f"无法施压：{type(exc).__name__}: {exc}")
    r.add(c)


def case_d2(args, r: Report) -> None:
    base, token = args.base_url, args.token
    c = CaseResult(
        "D2", "达标时延（P50/P95）",
        f"串行 {args.n_latency} 次单请求（max_tokens=16），记录整体耗时分布",
        "记录真实数值，不设硬指标（0.5b 小模型 + 手机环境）",
    )
    try:
        for _ in range(args.warmup):
            chat(base, args.chat_model, token, max_tokens=8, stream=False)
        lat: list[float] = []
        statuses: list[int] = []
        for _ in range(args.n_latency):
            resp = chat(base, args.chat_model, token, max_tokens=16, stream=False)
            statuses.append(resp.status)
            lat.append(resp.elapsed)
        c.data["latency"] = summarize(lat)
        c.data["statuses"] = {s: statuses.count(s) for s in set(statuses)}
        c.check("样本量 N≥20", len(lat) >= 20, f"实际 n={len(lat)}")
        c.check("样本全部 200", all(s == 200 for s in statuses), f"状态码分布 {c.data['statuses']}")
    except Exception as exc:  # noqa: BLE001
        c.measured = False
        c.notes.append(f"无法测量：{type(exc).__name__}: {exc}")
    r.add(c)


def case_d3(args, r: Report) -> None:
    base, token = args.base_url, args.token
    c = CaseResult(
        "D3", "流式首字节",
        "SSE 流式请求，记录首个非空 content 分片时间；与 /metrics first_byte 直方图计数核对",
        "客户端首字节时间与 /metrics first_byte_seconds 直方图一致（计数 +1）",
    )
    try:
        before = metrics_text(base, token)
        payload = {
            "model": args.chat_model,
            "messages": [{"role": "user", "content": "写一句关于秋天的话"}],
            "max_tokens": 48,
            "stream": True,
            "temperature": 0.7,
        }
        t0 = time.perf_counter()
        first_byte: float | None = None
        chunks = 0
        got_done = False
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            c.data["stream_status"] = resp.status
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    got_done = True
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta", {})
                content = delta.get("content") or ""
                if content and first_byte is None:
                    first_byte = time.perf_counter() - t0
                if content:
                    chunks += 1
        total = time.perf_counter() - t0
        after = metrics_text(base, token)

        delta_count = metric_sum(after, "quasheng_first_byte_seconds_count") - metric_sum(
            before, "quasheng_first_byte_seconds_count"
        )

        c.data["first_byte_s"] = round(first_byte, 4) if first_byte else None
        c.data["total_s"] = round(total, 4)
        c.data["content_chunks"] = chunks
        c.data["got_done"] = got_done
        c.data["first_byte_count_delta"] = delta_count
        c.check("收到首个 content 分片", first_byte is not None, f"first_byte={first_byte}")
        c.check("流式以 [DONE] 结束", got_done, f"got_done={got_done}")
        c.check("metrics 记录首字节（计数 +1）", delta_count >= 1.0, f"first_byte_count delta={delta_count}")
    except Exception as exc:  # noqa: BLE001
        c.measured = False
        c.notes.append(f"无法测量：{type(exc).__name__}: {exc}")
    r.add(c)


def case_d6(args, r: Report) -> None:
    base, token = args.base_url, args.token
    c = CaseResult(
        "D6", "能力缺失（501）",
        "对 chat-only 模型请求 embeddings，验证 501 capability_not_supported 且不计入成功计数",
        "501 capability_not_supported，不计入模型成功计数",
    )
    try:
        before = metrics_text(base, token)
        # 精确解析 model_requests_total 中 status=200 的计数
        def ok_count(txt: str) -> float:
            total = 0.0
            for line in txt.splitlines():
                if line.startswith("quasheng_model_requests_total") and 'status="200"' in line:
                    try:
                        total += float(line.rsplit(" ", 1)[1])
                    except ValueError:
                        pass
            return total

        before_success = ok_count(before)
        resp = post(f"{base}/v1/embeddings", {"model": args.chat_model, "input": ["hi"]}, token=token, timeout=30)
        after = metrics_text(base, token)
        after_success = ok_count(after)
        payload = resp.json() if resp.body else {}
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        c.data["status"] = resp.status
        c.data["code"] = err.get("code")
        c.data["error_type"] = err.get("type")
        c.data["model_requests_200_before"] = before_success
        c.data["model_requests_200_after"] = after_success
        c.check("返回 501", resp.status == 501, f"status={resp.status}")
        c.check("code=capability_not_supported", err.get("code") == "capability_not_supported", f"code={err.get('code')}")
        c.check("不计入模型成功计数", after_success <= before_success + 1e-9,
                f"status=200 计数 {before_success} -> {after_success}")
    except Exception as exc:  # noqa: BLE001
        c.measured = False
        c.notes.append(f"无法测量：{type(exc).__name__}: {exc}")
    r.add(c)


def main() -> int:
    args = parse_args()
    r = Report("stress_nodes", env=meta_from_args(args))
    case_d1(args, r)
    case_d2(args, r)
    case_d3(args, r)
    case_d6(args, r)
    out = args.out or default_out("stress_nodes")
    return exit_with(r, out, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
