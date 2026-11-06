"""准圣节点本地模式：最小对话 CLI。

刻意**不直连 Ollama**，而是通过 localhost 调用本节点自己的 /v1/* 端点——
以此证明「本地模式」与「集群消费」走的是完全相同的代码路径。
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _pick_model(base: str, token: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    resp = httpx.get(f"{base}/v1/models", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        sys.exit("节点没有可用模型，请先在 Ollama 中 pull 模型")
    return str(data[0]["id"])


def _stream_chat(base: str, token: str | None, model: str, message: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "stream": True,
    }
    with httpx.stream(
        "POST",
        f"{base}/v1/chat/completions",
        json=payload,
        headers=_headers(token),
        timeout=None,
    ) as resp:
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
                print(content, end="", flush=True)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="准圣节点本地对话（走 localhost 同一套契约端点）")
    parser.add_argument("--base", default="http://127.0.0.1:4010", help="本节点地址")
    parser.add_argument("--token", default=None, help="bearer token（节点开启鉴权时必填）")
    parser.add_argument("--model", default=None, help="不填则取 /v1/models 的第一个")
    parser.add_argument("--once", default=None, help="只发送一条消息后退出")
    args = parser.parse_args()

    model = _pick_model(args.base, args.token, args.model)
    print(f"准圣本地模式 · 模型 {model} · 端点 {args.base}")
    print("输入 /exit 退出\n")

    if args.once is not None:
        _stream_chat(args.base, args.token, model, args.once)
        return

    try:
        while True:
            message = input("你 > ").strip()
            if not message:
                continue
            if message in ("/exit", "/quit"):
                break
            print("准圣 > ", end="", flush=True)
            _stream_chat(args.base, args.token, model, message)
            print()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出")


if __name__ == "__main__":
    main()
