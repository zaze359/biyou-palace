"""道鉴 · 本机 OpenAI 兼容桩网关（仅用于端到端链路验证）。

**定位**：阵图的 `PlannerClient` 与 `NewApiExecutor` 都要求一个 OpenAI 兼容网关，
而真实的碧游宫/NewAPI 渠道还没有接通（第二阶段未做）。本桩只做两件事：

1. `POST /v1/chat/completions`：请求体带 `X-Stub-Role: planner` 时返回一份**固定的两段式计划 JSON**；
   否则回显一条带 `X-Stub-Step` 标记的文本，让执行器与聚合器有内容可用。
2. 其余路径一律 404，保持"只提供必需能力"。

它不是产品代码，不进 `zhentu/`；放在 `scripts/daojian/` 与其它检定工具同层，
并在输出里显式标注 `stub=true`，避免被误当成真实网关的数据。

用法：
    python3 scripts/daojian/stub_gateway.py --port 18080
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _plan_json() -> str:
    """两段式计划：s1 独立、s2 依赖 s1（非必需，失败只标 partial）。"""
    return json.dumps(
        {
            "plan_id": "plan-stub-e2e",
            "intent": "端到端链路验证：先总结，再给风险提示",
            "max_steps": 2,
            "max_parallel": 2,
            "steps": [
                {
                    "id": "s1",
                    "goal": "总结要点",
                    "capability": "chat",
                    "input": {"messages": [{"role": "user", "content": "请总结要点"}]},
                    "depends_on": [],
                    "parallel_group": None,
                    "exclusive": False,
                    "required": True,
                    "failure_policy": "fail_task",
                    "model_hint": None,
                    "input_ref": None,
                    "output_schema": None,
                },
                {
                    "id": "s2",
                    "goal": "给出风险提示",
                    "capability": "chat",
                    "input": {"messages": [{"role": "user", "content": "请给出风险提示"}]},
                    "depends_on": ["s1"],
                    "parallel_group": None,
                    "exclusive": False,
                    "required": False,
                    "failure_policy": "partial",
                    "model_hint": None,
                    "input_ref": None,
                    "output_schema": None,
                },
            ],
            "aggregate": {
                "mode": "concat",
                "input_step_ids": ["s1", "s2"],
                "include_partial": True,
                "instructions": None,
            },
        },
        ensure_ascii=False,
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "daojian-stub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "stub 只实现 /v1/chat/completions"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "请求体不是合法 JSON"}})
            return

        # 规划请求的特征：system 提示里含「阵图编排器」；执行请求没有。
        # 桩不依赖自定义头，这样同一个桩既能给 PlannerClient 也能给执行器用。
        messages = payload.get("messages") or []
        system_text = " ".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") == "system"
        )
        if "阵图编排器" in system_text:
            planner_user = " ".join(
                str(m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
            )
            content = _plan_json()
        else:
            # 执行步骤：按请求内容区分快慢，给「取消」用例留出可观测窗口
            step = "exec"
            user_text = " ".join(
                str(m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
            )
            model = payload.get("model") or ""
            # SLOW1：只有第一步慢（用于「跑到一半取消」）；SLOW：每步都慢
            # 慢步骤：s2（"给出风险提示"）固定慢 1s，给取消用例留出窗口。
            # 原始用户消息里的 SLOW/SLOWALL 也能让该步慢，覆盖单步回退场景。
            if "给出风险提示" in user_text or "SLOWALL" in user_text:
                time.sleep(1.0)
            content = f"[stub:{user_text[:24] or step}:{model}] 桩网关回显：已完成该步骤。"
        self._send(
            200,
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": payload.get("model") or "stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
                "stub": True,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "stub": True})
            return
        self._send(404, {"error": {"message": "not found"}})


def start_in_thread(port: int = 18080, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, threading.Thread]:
    """在后台线程起桩网关，供端到端脚本内联使用。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main() -> int:
    parser = argparse.ArgumentParser(description="道鉴 OpenAI 兼容桩网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"stub gateway listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())