#!/usr/bin/env python3
"""道鉴 · 碧游宫网关验收（G1–G5）。

验证四件事：
  G1 统一入口：客户端 key 能列出模型；
  G2 按模型路由：别名模型只命中准圣渠道；
  G3 权重分配：两条临时渠道权重 3:1，60 次请求按日志计数落在 62%–88%；
  G4 故障隔离：桩上游关停后渠道自动禁用，其它模型仍可服务；
  G5 基础并发：16 请求 / 并发 4，全部 2xx。

约束：
  - 临时渠道统一 ``biyou-verify-`` 前缀，``finally`` 删除；
  - 网关选项先快照，``finally`` 恢复并回读确认，失败则退出码非 0；
  - 桩上游绑定 0.0.0.0，容器经 host.docker.internal 访问宿主机。

用法：
    python3 scripts/daojian/gateway_acceptance.py --strict
    python3 scripts/daojian/gateway_acceptance.py --json out/gateway.json --strict
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "newapi"))

import gateway_admin as admin_mod  # noqa: E402

import common  # noqa: E402

ISOLATION_OPTIONS = (
    "RetryTimes",
    "ChannelDisableThreshold",
    "AutomaticDisableChannelEnabled",
    "AutomaticDisableStatusCodes",
)
TEMP_PREFIX = "biyou-verify-"


# --------------------------------------------------------------------------- #
# 桩上游
# --------------------------------------------------------------------------- #
class _StubState:
    mode = "ok"


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "biyou-gateway-stub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/v1/chat/completions"):
            self._send(404, {"error": {"message": "stub 只实现 /v1/chat/completions"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "请求体不是合法 JSON"}})
            return
        if _StubState.mode == "fail":
            self._send(500, {"error": {"message": "stub upstream failure", "type": "server_error"}})
            return
        self._send(
            200,
            {
                "id": "chatcmpl-biyou-stub",
                "object": "chat.completion",
                "model": payload.get("model") or "stub",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "stub ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "stub": True})
            return
        self._send(404, {"error": {"message": "not found"}})


def start_stub() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_stub(server: ThreadingHTTPServer | None) -> None:
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001 - 关停失败不掩盖主流程
        pass
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 客户端与日志
# --------------------------------------------------------------------------- #
def chat(gateway: str, api_key: str, model: str, *, max_tokens: int = 8, timeout: float = 120.0) -> common.HttpResponse:
    return common.post(
        f"{gateway}/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": max_tokens},
        token=api_key,
        timeout=timeout,
    )


def _dedupe(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for entry in logs:
        key = str(entry.get("request_id") or f"{entry.get('created_at')}:{entry.get('channel')}")
        seen[key] = entry
    return list(seen.values())


def wait_logs(
    admin: admin_mod.NewApiAdmin,
    *,
    since: int,
    model: str,
    minimum: int,
    timeout: float = 45.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    logs: list[dict[str, Any]] = []
    while True:
        logs = _dedupe(admin.list_logs(since=max(0, since - 2), model=model, limit=500))
        if len(logs) >= minimum or time.monotonic() >= deadline:
            return logs
        time.sleep(1.0)


def temp_spec(name: str, *, weight: int, upstream: str, model: str, upstream_model: str) -> admin_mod.ChannelSpec:
    return admin_mod.ChannelSpec(
        name=name,
        type=1,
        base_url=upstream,
        models=[model],
        group="default",
        groups=["default"],
        weight=weight,
        priority=0,
        auto_ban=1,
        key="sk-biyou-verify",
        model_mapping={model: upstream_model},
        setting={},
    )


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def case_g1(report: common.Report, gateway: str, api_key: str, model: str) -> None:
    case = report.add(
        common.CaseResult("G1", "统一入口可列出模型", "GET /v1/models", "200 且包含主模型")
    )
    try:
        resp = common.get(f"{gateway}/v1/models", token=api_key, timeout=30)
        case.data = {"status": resp.status, "elapsed": round(resp.elapsed, 3)}
        case.check("HTTP 200", resp.status == 200, f"status={resp.status}")
        ids: list[str] = []
        if resp.status == 200:
            try:
                ids = [str(item.get("id")) for item in (resp.json().get("data") or [])]
            except (ValueError, AttributeError) as exc:
                case.check("响应可解析", False, str(exc))
        case.data["models"] = ids
        case.check("包含主模型", model in ids, f"model={model} models={ids[:8]}")
    except Exception as exc:  # noqa: BLE001
        case.check("执行未抛异常", False, f"{type(exc).__name__}: {exc}")


def case_g2(
    report: common.Report,
    admin: admin_mod.NewApiAdmin,
    gateway: str,
    api_key: str,
    alias: str,
    expected_channel: str,
) -> None:
    case = report.add(
        common.CaseResult("G2", "别名模型只命中准圣渠道", f"POST /v1/chat/completions model={alias}", "200 且日志渠道一致")
    )
    try:
        started = int(time.time())
        resp = chat(gateway, api_key, alias)
        case.data = {"status": resp.status, "elapsed": round(resp.elapsed, 3), "model": alias}
        case.check("HTTP 200", resp.status == 200, f"status={resp.status} body={resp.text()[:160]}")
        if resp.status != 200:
            return
        logs = wait_logs(admin, since=started, model=alias, minimum=1, timeout=45)
        names = sorted({str(entry.get("channel_name")) for entry in logs})
        case.data["logs"] = len(logs)
        case.data["channels"] = names
        case.check("日志可查", len(logs) >= 1, f"logs={len(logs)}")
        case.check("渠道唯一且为准圣", names == [expected_channel], f"channels={names}")
    except Exception as exc:  # noqa: BLE001
        case.check("执行未抛异常", False, f"{type(exc).__name__}: {exc}")


def case_g3(
    report: common.Report,
    admin: admin_mod.NewApiAdmin,
    gateway: str,
    api_key: str,
    *,
    upstream: str,
    upstream_model: str,
    requests: int,
    heavy_weight: int,
    light_weight: int,
    created: list[int],
) -> None:
    case = report.add(
        common.CaseResult(
            "G3",
            "同模型双渠道按权重分配",
            f"声明权重 {heavy_weight}:{light_weight}，连发 {requests} 次短请求，按日志渠道计数",
            "占比落在 62%–88%",
        )
    )
    model = f"{TEMP_PREFIX}w3"
    channel_ids: list[int] = []
    try:
        # 创建阶段：只要成功一个就登记，保证 finally 能清理半成品。
        for name, weight in ((f"{TEMP_PREFIX}w3-heavy", heavy_weight), (f"{TEMP_PREFIX}w3-light", light_weight)):
            spec = temp_spec(name, weight=weight, upstream=upstream, model=model, upstream_model=upstream_model)
            cid = admin.create_channel(spec)
            channel_ids.append(cid)
            created.append(cid)

        case.check("两条临时渠道创建成功", len(channel_ids) == 2, f"ids={channel_ids}")
        if len(channel_ids) != 2:
            return

        started = int(time.time())
        statuses: list[int] = []
        elapsed: list[float] = []
        for _ in range(requests):
            resp = chat(gateway, api_key, model, max_tokens=4, timeout=60)
            statuses.append(resp.status)
            elapsed.append(resp.elapsed)

        case.data = {
            "requests": requests,
            "ok": sum(1 for s in statuses if 200 <= s < 300),
            "status_counts": {str(s): statuses.count(s) for s in sorted(set(statuses))},
            "latency": common.summarize(elapsed),
            "weights": {"heavy": heavy_weight, "light": light_weight},
            # NewAPI 在无内存缓存时按 (weight+10) 计算占比；给出预测值便于解读。
            "predicted_heavy_ratio": round((heavy_weight + 10) / (heavy_weight + light_weight + 20), 4),
        }
        case.check("全部请求 2xx", all(200 <= s < 300 for s in statuses), f"statuses={case.data['status_counts']}")

        logs = wait_logs(
            admin,
            since=started,
            model=model,
            minimum=max(1, case.data["ok"]),
            timeout=90,
        )
        counts = {cid: 0 for cid in channel_ids}
        for entry in logs:
            cid = entry.get("channel")
            if cid in counts:
                counts[cid] += 1
        total = sum(counts.values())
        heavy = counts.get(channel_ids[0], 0) if channel_ids else 0
        ratio = (heavy / total) if total else None
        case.data.update(
            {
                "logs": total,
                "channel_counts": {str(k): v for k, v in counts.items()},
                "heavy_ratio": round(ratio, 4) if ratio is not None else None,
                "heavy_channel": channel_ids[0] if channel_ids else None,
            }
        )
        case.check("日志覆盖请求数", total >= case.data["ok"], f"logs={total} ok={case.data['ok']}")
        case.check("两条渠道均被命中", all(v > 0 for v in counts.values()), f"counts={counts}")
        case.check("占比在 62%–88%", ratio is not None and 0.62 <= ratio <= 0.88, f"ratio={ratio}")
    except Exception as exc:  # noqa: BLE001
        case.check("执行未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        for cid in channel_ids:
            try:
                admin.delete_channel(cid)
                if cid in created:
                    created.remove(cid)
            except Exception as exc:  # noqa: BLE001
                case.notes.append(f"临时渠道 {cid} 删除失败：{exc}")


def case_g4(
    report: common.Report,
    admin: admin_mod.NewApiAdmin,
    gateway: str,
    api_key: str,
    *,
    model: str,
    snapshot: dict[str, str],
    created: list[int],
) -> None:
    case = report.add(
        common.CaseResult(
            "G4",
            "桩上游关停后自动隔离",
            "临时渠道指向进程内桩上游；关停桩后触发失败并自动禁用",
            "渠道 status=3，且主模型仍 200",
        )
    )
    stub: ThreadingHTTPServer | None = None
    cid: int | None = None
    status_snapshot: dict[int, int] = {}
    try:
        # 记录所有既有渠道状态：G4 会把阈值收紧到 1，若其它渠道因瞬时失败被误禁，finally 还原。
        status_snapshot = {
            int(ch["id"]): int(ch.get("status") or 0) for ch in admin.list_channels() if ch.get("id") is not None
        }
        stub = start_stub()
        port = stub.server_address[1]
        admin.set_option("RetryTimes", "0")
        admin.set_option("ChannelDisableThreshold", "1")
        admin.set_option("AutomaticDisableChannelEnabled", "true")
        admin.set_option("AutomaticDisableStatusCodes", "500")

        down_model = f"{TEMP_PREFIX}down"
        spec = admin_mod.ChannelSpec(
            name=f"{TEMP_PREFIX}down",
            type=1,
            base_url=f"http://host.docker.internal:{port}",
            models=[down_model],
            group="default",
            groups=["default"],
            weight=0,
            priority=0,
            auto_ban=1,
            key="sk-biyou-verify",
            model_mapping={},
            setting={},
        )
        cid = admin.create_channel(spec)
        created.append(cid)

        before = chat(gateway, api_key, down_model, max_tokens=4, timeout=30)
        case.check("关停前桩上游可用", before.status == 200, f"status={before.status}")

        stop_stub(stub)
        stub = None
        after = chat(gateway, api_key, down_model, max_tokens=4, timeout=30)
        case.data["failure_status"] = after.status
        case.check("关停后请求失败", 500 <= after.status < 600, f"status={after.status} body={after.text()[:160]}")

        status_now = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            channel = admin.get_channel(cid)
            status_now = (channel or {}).get("status")
            if status_now == 3:
                break
            time.sleep(0.5)
        case.data["channel_status"] = status_now
        case.check("渠道自动禁用 status=3", status_now == 3, f"status={status_now}")

        healthy = chat(gateway, api_key, model, max_tokens=4, timeout=60)
        case.data["healthy_status"] = healthy.status
        case.check("其它渠道仍可服务", healthy.status == 200, f"status={healthy.status}")
    except Exception as exc:  # noqa: BLE001
        case.check("执行未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        stop_stub(stub)
        if cid is not None:
            try:
                admin.delete_channel(cid)
                if cid in created:
                    created.remove(cid)
            except Exception as exc:  # noqa: BLE001
                case.notes.append(f"临时渠道 {cid} 删除失败：{exc}")
        # 选项恢复到调用前快照（即使用例失败也必须恢复）。
        try:
            for key in ISOLATION_OPTIONS:
                value = snapshot.get(key)
                if value is not None:
                    admin.set_option(key, value)
            restored = admin.get_options()
            mismatched = [k for k in ISOLATION_OPTIONS if snapshot.get(k) is not None and restored.get(k) != snapshot[k]]
            case.data["options_restored"] = not mismatched
            case.check("选项已恢复", not mismatched, f"mismatched={mismatched}")

            # 还原被本次收紧阈值误禁的既有渠道（只处理 1 → 非 1 的变化）。
            reenabled: list[int] = []
            for channel_id, before in status_snapshot.items():
                if before != 1:
                    continue
                current = admin.get_channel(channel_id) or {}
                if int(current.get("status") or 0) != 1:
                    admin.set_channel_status(channel_id, 1)
                    reenabled.append(channel_id)
            case.data["reenabled_channels"] = reenabled
            if reenabled:
                case.notes.append(f"G4 期间被自动禁用的既有渠道已恢复：{reenabled}")
        except Exception as exc:  # noqa: BLE001
            case.check("选项已恢复", False, f"{type(exc).__name__}: {exc}")


def case_g5(
    report: common.Report,
    admin: admin_mod.NewApiAdmin,
    gateway: str,
    api_key: str,
    *,
    total: int,
    concurrency: int,
    upstream: str,
    upstream_model: str,
    created: list[int],
) -> None:
    """用临时 type=4 渠道把并发压到 Ollama，避免准圣节点 429 名额干扰网关并发结论。"""
    case = report.add(
        common.CaseResult(
            "G5",
            "基础并发",
            f"临时 Ollama 渠道，{total} 请求 / 并发 {concurrency}",
            "全部 2xx，记录 p50/p95",
        )
    )
    temp_model = f"{TEMP_PREFIX}conc"
    cid: int | None = None
    try:
        spec = admin_mod.ChannelSpec(
            name=f"{TEMP_PREFIX}conc",
            type=4,
            base_url=upstream,
            models=[temp_model],
            group="default",
            groups=["default"],
            weight=0,
            priority=0,
            auto_ban=1,
            key="ollama-local",
            model_mapping={temp_model: upstream_model},
            setting={},
        )
        cid = admin.create_channel(spec)
        created.append(cid)

        def one() -> tuple[int, float]:
            resp = chat(gateway, api_key, temp_model, max_tokens=4, timeout=120)
            return resp.status, resp.elapsed

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(lambda _: one(), range(total)))
        statuses = [s for s, _ in results]
        elapsed = [e for _, e in results]
        case.data = {
            "total": total,
            "concurrency": concurrency,
            "channel": f"{TEMP_PREFIX}conc",
            "upstream_model": upstream_model,
            "status_counts": {str(s): statuses.count(s) for s in sorted(set(statuses))},
            "latency": common.summarize(elapsed),
        }
        case.check("全部 2xx", all(200 <= s < 300 for s in statuses), f"statuses={case.data['status_counts']}")
        case.check("时延已记录", len(elapsed) == total, f"n={len(elapsed)}")
    except Exception as exc:  # noqa: BLE001
        case.check("执行未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        if cid is not None:
            try:
                admin.delete_channel(cid)
                if cid in created:
                    created.remove(cid)
            except Exception as exc:  # noqa: BLE001
                case.notes.append(f"临时渠道 {cid} 删除失败：{exc}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="道鉴 · 碧游宫网关验收（G1–G5）")
    parser.add_argument("--gateway", default=os.environ.get("NEWAPI_BASE_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--api-key", default=os.environ.get("ZHENTU_NEWAPI_API_KEY", ""))
    parser.add_argument("--model", default="qwen2.5:7b", help="主模型（G1/G5）")
    parser.add_argument("--alias", default="quasheng/qwen2.5:7b", help="准圣别名模型（G2）")
    parser.add_argument("--alias-channel", default="quasheng-01", help="别名应命中的渠道名")
    parser.add_argument("--weight-upstream", default="http://host.docker.internal:4010", help="G3 临时渠道的上游")
    parser.add_argument("--weight-upstream-model", default="qwen2.5:0.5b", help="G3 临时渠道映射到的上游模型")
    parser.add_argument("--weight-requests", type=int, default=60, help="G3 请求数")
    parser.add_argument(
        "--weight-heavy",
        type=int,
        default=23,
        help="G3 重渠道权重（NewAPI 无内存缓存时按 weight+10 平滑，23:1 等效 75%% 流量）",
    )
    parser.add_argument("--weight-light", type=int, default=1, help="G3 轻渠道权重")
    parser.add_argument("--concurrency-total", type=int, default=16, help="G5 总请求数")
    parser.add_argument("--concurrency", type=int, default=4, help="G5 并发数")
    parser.add_argument("--g5-upstream", default="http://host.docker.internal:11434", help="G5 临时渠道的上游（Ollama）")
    parser.add_argument("--g5-upstream-model", default="qwen2.5:7b", help="G5 临时渠道映射到的上游模型")
    parser.add_argument("--out", default=common.default_out("gateway"), help="结果 JSON 输出路径")
    parser.add_argument("--strict", action="store_true", help="失败时返回非 0 退出码")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gateway = args.gateway.rstrip("/")
    if not args.api_key:
        print("错误: 缺少客户端 key，请设置 ZHENTU_NEWAPI_API_KEY 或 --api-key", file=sys.stderr)
        return 2

    report = common.Report(
        suite="biyou-gateway",
        env={
            "gateway": gateway,
            "model": args.model,
            "alias": args.alias,
            "alias_channel": args.alias_channel,
            "weight_upstream": args.weight_upstream,
            "weight_upstream_model": args.weight_upstream_model,
            "api_key_set": True,
            "host": common.env_info().get("host"),
            "python": common.env_info().get("python"),
        },
    )

    admin: admin_mod.NewApiAdmin | None = None
    snapshot: dict[str, str] = {}
    created: list[int] = []
    fatal = ""

    try:
        admin = admin_mod.resolve_admin(gateway)
        snapshot = admin.get_options()
        case_g1(report, gateway, args.api_key, args.model)
        case_g2(report, admin, gateway, args.api_key, args.alias, args.alias_channel)
        case_g3(
            report,
            admin,
            gateway,
            args.api_key,
            upstream=args.weight_upstream,
            upstream_model=args.weight_upstream_model,
            requests=args.weight_requests,
            heavy_weight=args.weight_heavy,
            light_weight=args.weight_light,
            created=created,
        )
        case_g4(
            report,
            admin,
            gateway,
            args.api_key,
            model=args.model,
            snapshot=snapshot,
            created=created,
        )
        case_g5(
            report,
            admin,
            gateway,
            args.api_key,
            total=args.concurrency_total,
            concurrency=args.concurrency,
            upstream=args.g5_upstream,
            upstream_model=args.g5_upstream_model,
            created=created,
        )
    except admin_mod.AdminError as exc:
        fatal = str(exc)
        print(f"错误: {fatal}", file=sys.stderr)
    finally:
        # 兜底清理：用例内部已清理的会从 created 移除。
        cleanup_errors: list[str] = []
        leftovers: list[str] = []
        if admin is not None:
            for cid in list(created):
                try:
                    admin.delete_channel(cid)
                    created.remove(cid)
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(f"删除渠道 {cid} 失败：{exc}")
            try:
                leftovers = [
                    str(ch.get("name")) for ch in admin.list_channels() if str(ch.get("name", "")).startswith(TEMP_PREFIX)
                ]
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"核对临时渠道失败：{exc}")
            cleanup_case = report.add(
                common.CaseResult("C1", "临时资源清理", "删除 biyou-verify-* 渠道并核对列表", "无临时渠道残留")
            )
            cleanup_case.data = {"leftovers": leftovers}
            cleanup_case.check("临时渠道已删除", not leftovers, f"leftovers={leftovers}")
            cleanup_case.check("删除无异常", not cleanup_errors, "; ".join(cleanup_errors))
        else:
            cleanup_case = report.add(
                common.CaseResult("C1", "临时资源清理", "管理员不可用，未创建任何资源", "无需清理")
            )
            cleanup_case.measured = False
            cleanup_case.notes.append(f"管理员鉴权失败：{fatal}")

    present = {case.id for case in report.cases}
    for case_id, title, method in (
        ("G1", "统一入口可列出模型", "GET /v1/models"),
        ("G2", "别名模型只命中准圣渠道", "POST /v1/chat/completions"),
        ("G3", "同模型双渠道按权重分配", "日志计数"),
        ("G4", "桩上游关停后自动隔离", "自动禁用"),
        ("G5", "基础并发", "并发请求"),
    ):
        if case_id in present:
            continue
        case = report.add(common.CaseResult(case_id, title, method, "前序异常，未执行"))
        case.measured = False
        case.notes.append(f"未执行原因：{fatal or '前序用例中断'}")

    data = report.to_dict()
    out = report.write(args.out)
    summary = data["summary"]
    print("\n[道鉴] 用例结果：")
    for case in data["cases"]:
        if not case["measured"]:
            mark = "未测"
        else:
            mark = "通过" if case["passed"] else "失败"
        detail = "；".join(
            f"{c['name']}={'✓' if c['passed'] else '✗'}" for c in case["checks"]
        )
        print(f"  [{case['id']}] {mark} {case['title']}  {detail}")
    print(f"\n[道鉴] 结果已写入: {out}")
    print(f"[道鉴] 汇总: {summary}")

    if args.strict and (summary["failed"] or fatal):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
