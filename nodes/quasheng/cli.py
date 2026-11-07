"""准圣节点本地模式：多轮对话 CLI。

刻意**不直连 Ollama**，而是通过 localhost 调用本节点自己的 /v1/* 端点——
以此证明「本地模式」与「集群消费」走的是完全相同的代码路径。

交互模式支持：
- 多轮上下文（user/assistant 历史逐轮发送）
- `/reset` 清空上下文
- `/model <name>` 运行中切换模型，`/model` 无参数列出可用模型
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx


class CliHTTPError(Exception):
    """节点返回非 2xx：消息已格式化为 OpenAI 错误体摘要。"""


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _format_error(status: int, body: bytes | str) -> str:
    """把 OpenAI 错误体压成一行 `HTTP <status> · <code> · <message>`。"""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = (text or "").strip()
    try:
        payload = json.loads(text)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            code = err.get("code") or "unknown"
            message = err.get("message") or text[:200]
            return f"HTTP {status} · {code} · {message}"
    except json.JSONDecodeError:
        pass
    return f"HTTP {status} · unknown · {text[:200] or '（空响应体）'}"


def _list_model_ids(base: str, token: str | None, *, transport=None) -> list[str]:
    with httpx.Client(transport=transport, timeout=10, trust_env=False) as client:
        resp = client.get(f"{base}/v1/models", headers=_headers(token))
    if resp.status_code >= 400:
        raise CliHTTPError(_format_error(resp.status_code, resp.content))
    data = resp.json().get("data") or []
    return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]


def _pick_model(
    base: str, token: str | None, explicit: str | None, *, transport=None
) -> str:
    if explicit:
        return explicit
    ids = _list_model_ids(base, token, transport=transport)
    if not ids:
        sys.exit("节点没有可用模型，请先在 Ollama 中 pull 模型")
    return ids[0]


def _stream_chat(
    base: str,
    token: str | None,
    model: str,
    messages: list[dict[str, Any]],
    *,
    transport=None,
) -> tuple[bool, str]:
    """发送一轮完整历史，流式打印 assistant 内容。

    返回 (是否成功, assistant 全文)。非 2xx 时打印 OpenAI 错误体摘要并返回
    (False, "")，不再静默输出空行。
    """
    payload = {"model": model, "messages": list(messages), "stream": True}
    parts: list[str] = []
    with httpx.Client(transport=transport, timeout=None, trust_env=False) as client:
        with client.stream(
            "POST",
            f"{base}/v1/chat/completions",
            json=payload,
            headers=_headers(token),
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                print(_format_error(resp.status_code, resp.content))
                return False, ""

            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                # delta.content 可能为 None（角色/结束标记 chunk），必须做保护
                if content:
                    parts.append(str(content))
                    print(content, end="", flush=True)
    print()
    return True, "".join(parts)


def run_repl(
    base: str,
    token: str | None,
    model: str,
    *,
    transport=None,
) -> None:
    """交互式多轮对话主循环。"""
    messages: list[dict[str, Any]] = []
    print(f"准圣本地模式 · 模型 {model} · 端点 {base}")
    print("命令：/exit 退出 · /reset 清空上下文 · /model [名称] 查看或切换模型\n")

    while True:
        try:
            raw = input("你 > ")
        except (KeyboardInterrupt, EOFError):
            print("\n已退出")
            return

        message = raw.strip()
        if not message:
            continue

        if message in ("/exit", "/quit"):
            break

        if message == "/reset":
            messages.clear()
            print("已清空上下文\n")
            continue

        if message.startswith("/model"):
            name = message[len("/model") :].strip()
            if name:
                model = name
                print(f"已切换到模型 {model}\n")
            else:
                try:
                    ids = _list_model_ids(base, token, transport=transport)
                    print("可用模型：" + (", ".join(ids) or "（无）") + "\n")
                except CliHTTPError as exc:
                    print(str(exc))
            continue

        if message.startswith("/"):
            print(f"未知命令：{message}（可用 /reset /model /exit）\n")
            continue

        messages.append({"role": "user", "content": message})
        print("准圣 > ", end="", flush=True)
        ok, reply = _stream_chat(base, token, model, messages, transport=transport)
        if ok:
            if reply:
                messages.append({"role": "assistant", "content": reply})
        else:
            # 失败轮次回滚 user 消息，避免把无效请求留在上下文里
            messages.pop()


def main() -> None:
    parser = argparse.ArgumentParser(description="准圣节点本地对话（走 localhost 同一套契约端点）")
    parser.add_argument("--base", default="http://127.0.0.1:4010", help="本节点地址")
    parser.add_argument("--token", default=None, help="bearer token（节点开启鉴权时必填）")
    parser.add_argument("--model", default=None, help="不填则取 /v1/models 的第一个")
    parser.add_argument("--once", default=None, help="只发送一条消息后退出")
    args = parser.parse_args()

    try:
        model = _pick_model(args.base, args.token, args.model)
    except CliHTTPError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        print(f"无法连接节点 {args.base}：{exc}", file=sys.stderr)
        raise SystemExit(1)

    if args.once is not None:
        ok, _ = _stream_chat(
            args.base, args.token, model, [{"role": "user", "content": args.once}]
        )
        if not ok:
            raise SystemExit(1)
        return

    run_repl(args.base, args.token, model)


if __name__ == "__main__":
    main()
