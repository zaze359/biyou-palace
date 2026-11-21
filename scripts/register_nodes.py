#!/usr/bin/env python3
"""批量注册节点到阵图（纯标准库）。

清单格式（也接受裸数组）：

```json
{
  "nodes": [
    {
      "id": "quasheng-01",
      "name": "准圣·01",
      "role": "quasheng",
      "endpoint": "http://192.168.1.10:4010",
      "models": [
        {"name": "qwen2.5:7b", "capabilities": ["chat"], "context_len": 32768, "loaded": true}
      ],
      "limits": {"max_concurrency": 4},
      "resources": {"queue_len": 0, "active": 0},
      "status": "ready",
      "heartbeat_interval_s": 15
    }
  ]
}
```

用法：
    export ZHENTU_REGISTRY_TOKEN='<cluster-token>'
    python3 scripts/register_nodes.py --file deploy/nodes.example.json --dry-run
    python3 scripts/register_nodes.py --file nodes.json

可选环境变量：
    ZHENTU_NODE_TOKENS   JSON 对象；某节点存在专属 token 时优先使用
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = os.environ.get("ZHENTU_REGISTRY_URL", "http://127.0.0.1:4020")
ROLE_DEFAULTS = {
    "quasheng": {"arch": "x86_64", "runtime": "ollama"},
    "sanxian": {"arch": "arm64", "runtime": "llama.cpp"},
}


def _load_nodes(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("nodes")
    if not isinstance(raw, list) or not raw:
        raise ValueError("清单必须是节点数组，或包含非空 nodes 数组的对象")
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 项不是对象")
        node_id = str(item.get("id") or "").strip()
        role = str(item.get("role") or "").strip()
        endpoint = str(item.get("endpoint") or "").strip()
        if not node_id or not role or not endpoint:
            raise ValueError(f"第 {index} 项缺少 id / role / endpoint")
        if role not in ROLE_DEFAULTS:
            raise ValueError(f"第 {index} 项 role={role!r} 非法（quasheng / sanxian）")
        nodes.append(item)
    return nodes


def _registry_node_tokens() -> dict[str, str]:
    raw = os.environ.get("ZHENTU_NODE_TOKENS") or ""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ZHENTU_NODE_TOKENS 不是合法 JSON：{exc.msg}") from None
    if not isinstance(parsed, dict):
        raise ValueError("ZHENTU_NODE_TOKENS 必须是 JSON 对象")
    return {str(key): str(value) for key, value in parsed.items()}


def _payload(item: dict[str, Any], boot_id: str) -> dict[str, Any]:
    role = str(item["role"])
    defaults = ROLE_DEFAULTS[role]
    node_id = str(item["id"])
    return {
        "node": {
            "id": node_id,
            "name": str(item.get("name") or node_id),
            "role": role,
            "arch": str(item.get("arch") or defaults["arch"]),
            "runtime": str(item.get("runtime") or defaults["runtime"]),
            "runtime_version": str(item.get("runtime_version") or "unknown"),
            "contract_version": str(item.get("contract_version") or "1.0"),
        },
        "endpoint": str(item["endpoint"]),
        "status": str(item.get("status") or "ready"),
        "models": list(item.get("models") or []),
        "limits": dict(item.get("limits") or {}),
        "resources": dict(item.get("resources") or {}),
        "heartbeat_interval_s": int(item.get("heartbeat_interval_s") or 15),
        "boot_id": str(item.get("boot_id") or boot_id),
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _post(registry: str, payload: dict[str, Any], token: str, timeout: float) -> tuple[bool, str]:
    node_id = payload["node"]["id"]
    request = urllib.request.Request(
        f"{registry.rstrip('/')}/zhentu/v1/nodes/register",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Node-Id": node_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(512).decode("utf-8", "replace")
            return response.status == 200, f"HTTP {response.status} {body[:160]}"
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", "replace")
        return False, f"HTTP {exc.code} {detail[:200]}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"连接失败：{exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量注册节点到阵图")
    parser.add_argument("--file", required=True, help="节点清单 JSON")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="阵图地址")
    parser.add_argument(
        "--token",
        default=os.environ.get("ZHENTU_REGISTRY_TOKEN", ""),
        help="集群 Bearer token（默认读 ZHENTU_REGISTRY_TOKEN）",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="单节点超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发送的报文")
    args = parser.parse_args(argv)

    try:
        items = _load_nodes(Path(args.file))
        node_tokens = _registry_node_tokens()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 2

    if not args.dry_run and not args.token:
        print("[ERR] 缺少 token：设置 ZHENTU_REGISTRY_TOKEN 或传 --token", file=sys.stderr)
        return 2

    boot_id = f"boot-batch-{time.strftime('%Y%m%d%H%M%S')}"
    failures = 0
    for item in items:
        payload = _payload(item, boot_id)
        node_id = payload["node"]["id"]
        token = node_tokens.get(node_id) or args.token
        if args.dry_run:
            print(f"[DRY] {node_id} -> {payload['endpoint']}")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        ok, detail = _post(args.registry, payload, token, args.timeout)
        print(f"[{'OK' if ok else 'FAIL'}] {node_id:<20} {detail}")
        if not ok:
            failures += 1

    if failures:
        print(f"\n失败 {failures}/{len(items)}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\n已注册 {len(items)} 个节点 -> {args.registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
