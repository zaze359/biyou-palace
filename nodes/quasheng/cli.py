"""准圣节点本地模式：多轮对话 CLI + 模型生命周期管理。

对话模式刻意**不直连 Ollama**，而是通过 localhost 调用本节点自己的 /v1/*
端点——以此证明「本地模式」与「集群消费」走的是完全相同的代码路径。

模型管理（list/pull/import/delete/unload）直连本机 Ollama 原生 /api/*：
契约第五章明确节点不提供模型/权重管理 API，因此管理面只存在于命令行。

交互模式支持：
- 多轮上下文（user/assistant 历史逐轮发送）
- `/reset` 清空上下文
- `/model <name>` 运行中切换模型，`/model` 无参数列出可用模型
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

from .model_ops import (
    DEFAULT_OLLAMA_BASE,
    ModelOpError,
    OllamaAdmin,
    build_modelfile,
    check_capacity,
    format_size,
    remote_model_size,
)


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


# ---------------------------------------------------------------- 模型生命周期


def _admin_base(explicit: str | None) -> str:
    return explicit or os.environ.get("QUASHENG_OLLAMA_BASE") or DEFAULT_OLLAMA_BASE


def cmd_list(base: str, *, transport: httpx.BaseTransport | None = None) -> int:
    """列出已安装模型，并标出 /api/ps 中已加载的项。

    数据源与 /v1/health 一致：/api/tags（清单）+ /api/ps（已加载）。
    """
    with OllamaAdmin(base, transport=transport) as admin:
        tags = admin.tags()
        try:
            loaded = {
                str(item.get("name") or item.get("model"))
                for item in admin.ps()
                if isinstance(item, dict)
            }
        except ModelOpError as exc:
            print(f"提示：{exc}；已加载状态未知", file=sys.stderr)
            loaded = set()

    if not tags:
        print("（无已安装模型）")
        return 0

    print(f"{'模型'.ljust(32)} {'大小'.rjust(12)}  {'状态'.ljust(6)} 参数")
    for item in tags:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "?")
        details = item.get("details") or {}
        params = " ".join(
            str(details[key])
            for key in ("parameter_size", "quantization_level")
            if details.get(key)
        ) or "-"
        state = "已加载" if name in loaded else "未加载"
        print(
            f"{name.ljust(32)} {format_size(item.get('size')).rjust(12)}  "
            f"{state.ljust(6)} {params}"
        )
    return 0


def cmd_pull(
    model: str,
    base: str,
    *,
    transport: httpx.BaseTransport | None = None,
    registry_transport: httpx.BaseTransport | None = None,
    models_dir: Path | None = None,
) -> int:
    """拉取模型：先按远程 manifest 体积预检，再输出 Ollama 进度。"""
    size = remote_model_size(model, transport=registry_transport)
    if size is None:
        print(f"提示：无法获取 {model} 的远程体积，跳过磁盘/内存预检", file=sys.stderr)
    else:
        check_capacity(model, size, step="拉取", models_dir=models_dir)

    last_status = ""
    with OllamaAdmin(base, transport=transport) as admin:
        for event in admin.pull(model):
            status = str(event.get("status") or "").strip()
            # 只打印状态变化，避免百分比行刷屏
            if status and status != last_status:
                print(status, flush=True)
                last_status = status
    suffix = f"（{format_size(size)}）" if size else ""
    print(f"已拉取模型 {model}{suffix}")
    return 0


def cmd_import(
    gguf: str,
    name: str | None,
    base: str,
    *,
    system: str | None = None,
    template: str | None = None,
    transport: httpx.BaseTransport | None = None,
    models_dir: Path | None = None,
) -> int:
    """本地 GGUF → ollama create，并把等价 Modelfile 打到屏幕便于复现。"""
    path = Path(gguf).expanduser()
    model = (name or path.stem).strip()
    if not model:
        raise ModelOpError("导入模型失败：模型名为空，请用 --name 指定")
    if not path.is_file():
        raise ModelOpError(f"导入模型 {model} 失败：GGUF 文件不存在：{path}")
    if path.suffix.lower() != ".gguf":
        print(f"提示：{path.name} 不是 .gguf 后缀，仍按 GGUF 处理", file=sys.stderr)

    check_capacity(model, path.stat().st_size, step="导入", models_dir=models_dir)

    print("将使用以下 Modelfile：")
    print(build_modelfile(path, system=system, template=template).rstrip())
    with OllamaAdmin(base, transport=transport) as admin:
        admin.create(model, path, system=system, template=template)
    print(f"已导入模型 {model}")
    print(f"验证：python -m nodes.quasheng.cli list（应出现 {model}）")
    return 0


def cmd_delete(
    model: str,
    base: str,
    *,
    yes: bool = False,
    transport: httpx.BaseTransport | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """删除模型：默认交互确认；非交互环境必须显式 --yes。"""
    if not yes:
        if confirm is None:
            if not sys.stdin.isatty():
                raise ModelOpError(
                    f"删除模型 {model} 需要确认：当前不是交互终端，请显式加 --yes"
                )
            answer = input(f"确认删除模型 {model}？该操作不可撤销 [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
        else:
            confirmed = confirm(model)
        if not confirmed:
            print(f"已取消删除模型 {model}")
            return 1

    with OllamaAdmin(base, transport=transport) as admin:
        admin.delete(model)
    print(f"已删除模型 {model}")
    return 0


def cmd_unload(
    model: str, base: str, *, transport: httpx.BaseTransport | None = None
) -> int:
    """keep_alive=0 卸载模型，释放其占用的内存。"""
    with OllamaAdmin(base, transport=transport) as admin:
        admin.unload(model)
    print(f"已卸载模型 {model}（keep_alive=0，内存已释放）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="准圣节点本地对话与模型管理（对话走 localhost 契约端点，管理直连本机 Ollama）"
    )
    parser.add_argument("--base", default="http://127.0.0.1:4010", help="本节点地址")
    parser.add_argument("--token", default=None, help="bearer token（节点开启鉴权时必填）")
    parser.add_argument("--model", default=None, help="不填则取 /v1/models 的第一个")
    parser.add_argument("--once", default=None, help="只发送一条消息后退出")

    sub = parser.add_subparsers(dest="command")

    def add_admin_options(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--ollama",
            default=None,
            help=f"本机 Ollama 地址（默认读 QUASHENG_OLLAMA_BASE，否则 {DEFAULT_OLLAMA_BASE}）",
        )

    p = sub.add_parser("list", help="列出已安装模型并标出已加载项")
    add_admin_options(p)

    p = sub.add_parser("pull", help="拉取模型（拉取前做磁盘/内存预检）")
    p.add_argument("model", help="模型名，如 qwen2.5:7b-instruct-q4_K_M")
    add_admin_options(p)

    p = sub.add_parser("import", help="从本地 GGUF 导入模型（ollama create）")
    p.add_argument("gguf", help="GGUF 文件路径")
    p.add_argument("--name", default=None, help="模型名，默认取 GGUF 文件名")
    p.add_argument("--system", default=None, help="写入 Modelfile 的 SYSTEM 提示")
    p.add_argument("--template", default=None, help="写入 Modelfile 的 TEMPLATE")
    add_admin_options(p)

    p = sub.add_parser("delete", help="删除模型（默认需要确认）")
    p.add_argument("model", help="要删除的模型名")
    p.add_argument("--yes", action="store_true", help="跳过确认（非交互环境必须显式指定）")
    add_admin_options(p)

    p = sub.add_parser("unload", help="卸载模型（keep_alive=0）")
    p.add_argument("model", help="要卸载的模型名")
    add_admin_options(p)

    args = parser.parse_args()

    try:
        if args.command:
            base = _admin_base(args.ollama)
            if args.command == "list":
                code = cmd_list(base)
            elif args.command == "pull":
                code = cmd_pull(args.model, base)
            elif args.command == "import":
                code = cmd_import(
                    args.gguf,
                    args.name,
                    base,
                    system=args.system,
                    template=args.template,
                )
            elif args.command == "delete":
                code = cmd_delete(args.model, base, yes=args.yes)
            elif args.command == "unload":
                code = cmd_unload(args.model, base)
            else:  # pragma: no cover - argparse 已限定取值
                raise ModelOpError(f"未知命令：{args.command}")
        else:
            code = _chat_main(args)
    except ModelOpError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    if code:
        raise SystemExit(code)


def _chat_main(args: argparse.Namespace) -> int:
    """原有对话模式：走节点 /v1/*，不直连 Ollama。"""
    try:
        model = _pick_model(args.base, args.token, args.model)
    except CliHTTPError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"无法连接节点 {args.base}：{exc}", file=sys.stderr)
        return 1

    if args.once is not None:
        ok, _ = _stream_chat(
            args.base, args.token, model, [{"role": "user", "content": args.once}]
        )
        return 0 if ok else 1

    run_repl(args.base, args.token, model)
    return 0



if __name__ == "__main__":
    main()
