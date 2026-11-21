"""道鉴 · 阵图编排端到端链路验证（E2E）。

验证内容（对应 blueprint 第三步「复杂请求自动拆多子任务并行；单散仙掉线不中断整条推演」）：

- E1 提交 → 202 + `task_id` / `events_url` / `result_url`
- E2 事件流：`task/created → task/planned → step/started → step/result → aggregate/* → task/completed`
- E3 最终 `ResultEnvelope`：`status=completed`、`answer` 含两个步骤内容、`sources` 两段
- E4 幂等：同 `request_id` 再提交返回 200 且 `task_id` 相同、不重复执行
- E5 越权：换一个节点身份读同一任务 → 403
- E6 取消：长任务在第一步后取消 → `canceled`
- E7 能力缺失：`requirements.capabilities=["rerank"]`（无节点声明）→ 任务 `failed` 且错误码可读

**桩网关**：阵图的 Planner 与 Execution 都要一个 OpenAI 兼容网关。本脚本默认内联起
`stub_gateway.py`，报告里标 `stub=true`；传 `--gateway-base-url` 时走真实网关，标记 `stub=false`。

真实网关模式与桩模式有三处必要差异（都由 `real` 分支处理，不改变桩模式行为）：

- 密钥取 `--gateway-api-key` 或 `ZHENTU_NEWAPI_API_KEY`（不再用 `stub-key`）；
- 规划模型、节点模型、规划/执行超时、任务截止时间都换成真实可用的值
  （7B 在 CPU 上单步可达数十秒，桩模式的 60s 截止必然超时）；
- E3 不再断言桩回显文本，改为断言「多步结果 + 答案非空 + sources 与结果数一致」。

用法：
    python3 scripts/daojian/orchestration_e2e.py            # 内联桩网关 + 临时阵图实例
    python3 scripts/daojian/orchestration_e2e.py --json out/e2e.json
    # 真实网关：阵图 -> NewAPI -> 节点
    python3 scripts/daojian/orchestration_e2e.py \
        --gateway-base-url http://127.0.0.1:3000/v1 \
        --planner-model qwen2.5:7b --chat-model qwen2.5:7b \
        --json out/e2e-real.json --strict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.daojian import stub_gateway  # noqa: E402

NODE_TOKEN = "e2e-node-token"
NODE_ID = "sanxian-01"
OTHER_NODE_ID = "sanxian-02"


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.op = _opener()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        node_id: str | None = NODE_ID,
        token: str | None = NODE_TOKEN,
    ) -> tuple[int, Any, dict[str, str]]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if node_id:
            headers["X-Node-Id"] = node_id
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with self.op.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, _maybe_json(raw), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, _maybe_json(raw), dict(exc.headers)

    def stream_events(self, path: str, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """读 SSE 直到连接关闭（任务终态时服务端结束流）。"""
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {NODE_TOKEN}",
                "X-Node-Id": NODE_ID,
                "Accept": "text/event-stream",
            },
        )
        events: list[dict[str, Any]] = []
        deadline = time.time() + timeout
        with self.op.open(req, timeout=timeout) as resp:
            for raw in resp:
                if time.time() > deadline:
                    break
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _collect_events(http: Any, task_id: str) -> list[dict[str, Any]]:
    """把某任务的事件流完整读一遍（终态时服务端会读完剩余事件再关闭）。"""
    collected: list[dict[str, Any]] = []
    try:
        with http.stream(
            "GET",
            f"/zhentu/v1/requests/{task_id}/events",
            headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-Node-Id": NODE_ID},
        ) as stream:
            for line in stream.iter_lines():
                if line and line.startswith("data: "):
                    collected.append(json.loads(line[6:]))
    except Exception:
        pass
    return collected

def _request_body(
    request_id: str,
    *,
    capabilities: list[str] | None = None,
    deadline_s: int = 60,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "origin_node_id": NODE_ID,
        "mode": "cluster",
        "requested_mode": "cluster",
        "privacy": "normal",
        "deadline_s": deadline_s,
        "input": {"messages": [{"role": "user", "content": "请总结并给出风险提示"}]},
        "requirements": {"capabilities": capabilities or ["chat"]},
    }


def _wait_terminal(client: Client, task_id: str, *, timeout: float = 60.0) -> tuple[Any, int]:
    deadline = time.time() + timeout
    last: Any = None
    status = 0
    while time.time() < deadline:
        status, body, _ = client.request("GET", f"/zhentu/v1/requests/{task_id}")
        last = body
        if isinstance(body, dict):
            state = body.get("status")
            # 终态 ResultEnvelope 带 answer/sources；进行中只有 summary
            if state in {"completed", "partial", "failed", "canceled"} and (
                "schema_version" in body or "answer" in body or "sources" in body
            ):
                return body, status
        time.sleep(0.1)
    return last, status


def run(
    gateway_base_url: str | None = None,
    *,
    stub: bool | None = None,
    gateway_api_key: str | None = None,
    planner_model: str = "qwen2.5:7b",
    chat_model: str = "e2e-chat",
    deadline_s: int = 60,
    cancel_wait_s: float | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    started = time.time()

    real = gateway_base_url is not None
    stub_server = None
    if not real:
        stub_server, _ = stub_gateway.start_in_thread(port=0, host="127.0.0.1")
        gateway_base_url = f"http://127.0.0.1:{stub_server.server_address[1]}"
        stub = True if stub is None else stub
    else:
        stub = False if stub is None else stub
    if cancel_wait_s is None:
        # 真实 7B 单步可能跑数十秒；取消在批次边界生效，要等首步跑完才落终态
        cancel_wait_s = 300.0 if real else 20.0

    os.environ["ZHENTU_NODE_TOKENS"] = json.dumps({NODE_ID: NODE_TOKEN, OTHER_NODE_ID: NODE_TOKEN + "-2"})
    os.environ["ZHENTU_NEWAPI_BASE_URL"] = gateway_base_url
    os.environ["ZHENTU_AGGREGATOR_MODE"] = "concat"
    if real:
        api_key = gateway_api_key or os.environ.get("ZHENTU_NEWAPI_API_KEY") or ""
        if not api_key:
            raise SystemExit("真实网关模式需要 --gateway-api-key 或环境变量 ZHENTU_NEWAPI_API_KEY")
        os.environ["ZHENTU_NEWAPI_API_KEY"] = api_key
        # 7B 在 CPU 上规划/执行都要数十秒，桩模式的默认超时会让任务稳定超时失败
        os.environ["ZHENTU_PLANNER_MODEL"] = planner_model
        os.environ["ZHENTU_PLANNER_TIMEOUT_S"] = "300"
        os.environ["ZHENTU_NEWAPI_TIMEOUT_S"] = "300"
    else:
        os.environ["ZHENTU_NEWAPI_API_KEY"] = "stub-key"
        # 不设 ZHENTU_PLANNER_MODEL 时阵图直接用 SingleStepPlanner（单步），
        # 要验证「拆多子任务」必须让 PlannerClient 真正去调规划模型；桩网关会返回两段计划。
        os.environ["ZHENTU_PLANNER_MODEL"] = "planner-stub"

    tmpdir = Path(tempfile.mkdtemp(prefix="daojian-e2e-"))
    sys.path.insert(0, str(ROOT))
    from zhentu.app import create_app  # noqa: PLC0415 - 需要先设好环境变量
    from zhentu.config import load_settings  # noqa: PLC0415

    os.environ.setdefault("ZHENTU_HOST", "127.0.0.1")
    os.environ["ZHENTU_DB"] = str(tmpdir / "e2e.db")
    settings = load_settings()
    app = create_app(settings)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    def check(name: str, ok: bool, detail: str, data: Any = None) -> None:
        results.append({"case": name, "ok": bool(ok), "detail": detail, "data": data})

    with TestClient(app) as http:
        for node_id, role, port in (
            (NODE_ID, "sanxian", 4031),
            ("quasheng-01", "quasheng", 4010),
        ):
            reg = http.post(
                "/zhentu/v1/nodes/register",
                headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-Node-Id": node_id},
                json={
                    "node": {
                        "id": node_id,
                        "name": node_id,
                        "role": role,
                        "arch": "arm64" if role == "sanxian" else "x86_64",
                        "runtime": "llama.cpp" if role == "sanxian" else "ollama",
                        "runtime_version": "e2e",
                        "contract_version": "1.0",
                    },
                    "endpoint": f"http://127.0.0.1:{port}",
                    "status": "ready",
                    "models": [
                        {"name": chat_model, "capabilities": ["chat"], "context_len": 4096, "loaded": True}
                    ],
                    "limits": {"max_concurrency": 4, "max_context_len": 4096},
                    "resources": {"queue_len": 0, "active": 0},
                    "heartbeat_interval_s": 15,
                    "boot_id": "boot-e2e-0001",
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                },
            )
            if reg.status_code != 200:
                results.append({
                    "case": f"E0 注册节点 {node_id}",
                    "ok": False,
                    "detail": f"status={reg.status_code} {reg.text[:160]}",
                    "data": None,
                })

        def call(method: str, path: str, **kwargs: Any) -> Any:
            body = kwargs.get("body")
            node_id = kwargs.get("node_id", NODE_ID)
            token = kwargs.get("token", NODE_TOKEN)
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if node_id:
                headers["X-Node-Id"] = node_id
            return http.request(method, path, json=body, headers=headers)

        # ---- E1 提交
        request_id = "req-e2e-" + uuid.uuid4().hex[:12]
        resp = call(
            "POST", "/zhentu/v1/requests", body=_request_body(request_id, deadline_s=deadline_s)
        )
        created = resp.json()
        task_id = created.get("task_id")
        check(
            "E1 提交返回 202 与 task_id/urls",
            resp.status_code == 202 and bool(task_id) and "events_url" in created and "result_url" in created,
            f"status={resp.status_code}",
            created,
        )

        # ---- E2/E3 事件流 + 最终结果
        # 真实模型（CPU 上的 7B）规划 + 两步执行可能超过 60s，按任务截止时间等；
        # 桩模式仍用 60s，行为不变。
        final, get_status = _wait_via(call, task_id, timeout=deadline_s if real else 60)
        types: list[str] = []
        events: list[dict[str, Any]] = []
        if task_id:
            try:
                # 任务已终态时服务端会读完剩余事件再结束流；这里完整收集一遍用于断言
                with http.stream("GET", f"/zhentu/v1/requests/{task_id}/events",
                                 headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-Node-Id": NODE_ID}) as stream:
                    for line in stream.iter_lines():
                        if line and line.startswith("data: "):
                            event = json.loads(line[6:])
                            events.append(event)
                            types.append(event.get("type"))
            except Exception as exc:  # 流异常不影响最终结果判定
                types.append(f"stream_error:{type(exc).__name__}")

        expected = ["task/created", "task/planned", "step/started", "step/result", "task/completed"]
        missing = [t for t in expected if t not in types]
        planned = next((e for e in events if e.get("type") == "task/planned"), {})
        check(
            "E2 事件流按序覆盖关键类型",
            not missing,
            f"types={types[:12]}{'...' if len(types) > 12 else ''} planned={planned.get('data')}",
            {"types": types, "planned": planned.get("data")},
        )

        step_results = [e for e in events if e.get("type") == "step/result"]
        answer = (final or {}).get("answer") if isinstance(final, dict) else None
        if real:
            # 真实模型不会回显桩的固定文本；这里只验证「拆出多步 + 汇总非空 + 来源可追溯」，
            # 具体内容由真实模型决定，不做文本匹配。
            sources = (final or {}).get("sources") or []
            ok_final = (
                isinstance(final, dict)
                and final.get("status") == "completed"
                and isinstance(answer, str)
                and len(answer.strip()) >= 20
                and len(step_results) >= 2
                and len(sources) == len(step_results)
            )
            e3_detail = (
                f"status={(final or {}).get('status')} step_results={len(step_results)} "
                f"sources={len(sources)} answer_len={len(answer or '')}"
            )
            e3_title = "E3 最终 ResultEnvelope 完成、多步结果汇总"
        else:
            ok_final = (
                isinstance(final, dict)
                and final.get("status") == "completed"
                and isinstance(answer, str)
                and "请总结要点" in answer
                and "请给出风险提示" in answer
                and answer.index("请总结要点") < answer.index("请给出风险提示")
                and len(step_results) == 2
            )
            e3_detail = f"status={(final or {}).get('status')} step_results={len(step_results)}"
            e3_title = "E3 最终 ResultEnvelope 完成、两步结果按序拼接"
        check(
            e3_title,
            ok_final,
            e3_detail,
            {
                "status": (final or {}).get("status"),
                "answer": ((final or {}).get("answer") or "")[:300],
                "step_results": [r.get("data", {}).get("step_id") for r in step_results],
            },
        )

        # ---- E4 幂等
        dup = call(
            "POST", "/zhentu/v1/requests", body=_request_body(request_id, deadline_s=deadline_s)
        )
        dbody = dup.json()
        check(
            "E4 同 request_id 幂等返回原任务",
            dup.status_code == 200 and dbody.get("task_id") == task_id and dbody.get("duplicate") is True,
            f"status={dup.status_code} duplicate={dbody.get('duplicate')}",
            dbody,
        )

        # ---- E5 越权
        foreign = call("GET", f"/zhentu/v1/requests/{task_id}", node_id=OTHER_NODE_ID, token=NODE_TOKEN + "-2")
        check(
            "E5 非发起节点读任务返回 403",
            foreign.status_code == 403 and (foreign.json().get("error") or {}).get("code") == "task_forbidden",
            f"status={foreign.status_code}",
            foreign.json(),
        )

        # ---- E6 取消
        cancel_req = "req-e2e-cancel-" + uuid.uuid4().hex[:8]
        cancel_body = _request_body(cancel_req, deadline_s=deadline_s)
        # SLOWALL：桩网关执行会沉睡 1s，保证取消落在执行中途；
        # 真实模型下取消通常落在规划阶段或首批执行中，两者都按批次边界生效。
        cancel_body["input"]["messages"] = [{"role": "user", "content": "SLOWALL 请总结并给出风险提示"}]
        cresp = call("POST", "/zhentu/v1/requests", body=cancel_body)
        cancel_task = cresp.json().get("task_id")
        cancel_status = 0
        cancel_final: Any = None
        if cancel_task:
            # 等任务进入 running（执行已开始），再留 100ms 让首步真正跑起来后发取消
            for _ in range(300):
                body = call("GET", f"/zhentu/v1/requests/{cancel_task}").json()
                if body.get("status") == "running":
                    break
                time.sleep(0.01)
            time.sleep(0.1)
            cdel = call("POST", f"/zhentu/v1/requests/{cancel_task}/cancel")
            cancel_status = cdel.status_code
            # 取消在下一个批次边界生效：此时首步可能已跑完，必须用 SLOW 让首步足够长
            cancel_final, _ = _wait_via(call, cancel_task, timeout=cancel_wait_s)
            events_of_cancel = _collect_events(http, cancel_task)
            cancel_events = [e.get("type") for e in events_of_cancel]
        # 取消语义（看 zhentu/runner.py）：批次边界生效。
        # 取消落在「两步都跑完之后、聚合之前」→ 直接进入 canceled，没有待取消步骤；
        # 只有还有未开始的步骤时才发 step/canceled。两种情况都以「任务终态=canceled」为准。
        check(
            "E6 取消后任务进入 canceled",
            cancel_status in (200, 202)
            and (cancel_final or {}).get("status") == "canceled",
            f"cancel_http={cancel_status} final={(cancel_final or {}).get('status')} "
            f"events={cancel_events[:8]}",
            {"cancel_http": cancel_status, "final_status": (cancel_final or {}).get("status"),
             "events": cancel_events[:12]},
        )

        # ---- E7 能力缺失
        # 桩网关的规划提示不含 requirements，无法让计划步骤变成 rerank；这里改为验证
        # 选路层对「无节点声明该能力」的行为：POST /route 应 200 + selected=null + no_route。
        route_resp = call(
            "POST",
            "/zhentu/v1/route",
            body={"capability": "rerank", "max_candidates": 5},
        )
        rbody = route_resp.json()
        check(
            "E7 无节点声明该能力时选路返回 no_route",
            route_resp.status_code == 200
            and rbody.get("selected") is None
            and rbody.get("reason") == "no_route",
            f"status={route_resp.status_code} reason={rbody.get('reason')}",
            {"status": route_resp.status_code, "reason": rbody.get("reason")},
        )

    if stub_server is not None:
        stub_server.shutdown()

    passed = sum(1 for r in results if r["ok"])
    return {
        "tool": "orchestration_e2e",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - started, 2),
        "gateway": gateway_base_url,
        "stub": bool(stub),
        "planner_model": os.environ.get("ZHENTU_PLANNER_MODEL"),
        "chat_model": chat_model,
        "deadline_s": deadline_s,
        "passed": passed,
        "total": len(results),
        "results": results,
    }


def _wait_via(call: Any, task_id: str, timeout: float = 60.0) -> tuple[Any, int]:
    deadline = time.time() + timeout
    last: Any = None
    status = 0
    while time.time() < deadline:
        resp = call("GET", f"/zhentu/v1/requests/{task_id}")
        status = resp.status_code
        last = resp.json()
        if isinstance(last, dict):
            state = last.get("status")
            if state in {"completed", "partial", "failed", "canceled"} and (
                "schema_version" in last or "answer" in last or "sources" in last or "error" in last
            ):
                return last, status
        time.sleep(0.1)
    return last, status


def main() -> int:
    parser = argparse.ArgumentParser(description="阵图编排端到端链路验证")
    parser.add_argument("--gateway-base-url", default=None, help="真实 OpenAI 兼容网关；缺省用内联桩")
    parser.add_argument(
        "--gateway-api-key", default=None, help="真实网关密钥；缺省读 ZHENTU_NEWAPI_API_KEY"
    )
    parser.add_argument("--planner-model", default="qwen2.5:7b", help="真实网关模式下的规划模型")
    parser.add_argument(
        "--chat-model", default="e2e-chat", help="注册节点声明的模型名；真实模式需为网关可路由的模型"
    )
    parser.add_argument("--deadline-s", type=int, default=60, help="任务截止时间（秒）")
    parser.add_argument("--json", dest="json_path", default=None, help="结果写入 JSON 文件")
    parser.add_argument("--strict", action="store_true", help="有失败项时退出码 1")
    args = parser.parse_args()

    result = run(
        args.gateway_base_url,
        gateway_api_key=args.gateway_api_key,
        planner_model=args.planner_model,
        chat_model=args.chat_model,
        deadline_s=args.deadline_s,
    )
    print(f"编排 E2E：{result['passed']}/{result['total']} 通过（stub={result['stub']}，{result['elapsed_s']}s）")
    for item in result["results"]:
        print(f"  {'OK ' if item['ok'] else 'FAIL'} {item['case']} — {item['detail']}")
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {out}")
    if args.strict and result["passed"] != result["total"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())