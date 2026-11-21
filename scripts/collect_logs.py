#!/usr/bin/env python3
"""收集集群日志与诊断快照（纯标准库）。

来源：
  - 中枢机容器日志：docker logs --since <window>（默认四个服务）
  - 边缘节点运行事件：GET <node>/v1/logs?limit=200（节点需可达且 token 正确）
  - 阵图注册视图：GET <registry>/zhentu/v1/nodes
  - 诊断快照：docker compose ps、docker version、uname、日期

用法：
    python3 scripts/collect_logs.py
    python3 scripts/collect_logs.py --since 2h --out logs/incident-1
    python3 scripts/collect_logs.py --node quasheng-01=http://192.168.1.10:4010 \
        --node-token "$QUASHENG_SHARED_TOKEN"

单个来源失败只记录到 summary.txt，不影响其它来源；全部失败时退出码 1。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONTAINERS = ("new-api", "biyou-zhentu", "lingmai-prometheus", "lingmai-grafana")


def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = proc.stdout
        if proc.stderr:
            output += ("\n" if output and not output.endswith("\n") else "") + proc.stderr
        return proc.returncode, output
    except FileNotFoundError:
        return 127, f"命令不存在：{args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时：{' '.join(args)}"
    except OSError as exc:
        return 1, f"命令失败：{exc}"


def _fetch_json(url: str, token: str, timeout: float) -> tuple[bool, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", "replace")
        return False, f"HTTP {exc.code} {body}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, f"请求失败：{exc}"


def _parse_nodes(values: list[str]) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"--node 需要 id=url 形式：{value!r}")
        node_id, url = value.split("=", 1)
        if not node_id.strip() or not url.strip():
            raise ValueError(f"--node 需要 id=url 形式：{value!r}")
        nodes.append((node_id.strip(), url.strip().rstrip("/")))
    return nodes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收集碧游宫集群日志与诊断快照")
    parser.add_argument(
        "--out",
        default=f"logs/collect-{time.strftime('%Y%m%d-%H%M%S')}",
        help="输出目录（默认 logs/collect-<时间>）",
    )
    parser.add_argument("--since", default="2h", help="docker logs 时间窗口（默认 2h）")
    parser.add_argument(
        "--container",
        action="append",
        dest="containers",
        help=f"容器名，可重复（默认 {' '.join(DEFAULT_CONTAINERS)}）",
    )
    parser.add_argument("--node", action="append", default=[], help="节点 id=url，可重复")
    parser.add_argument(
        "--node-token",
        default=os.environ.get("QUASHENG_SHARED_TOKEN", ""),
        help="节点 Bearer token（默认读 QUASHENG_SHARED_TOKEN）",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("ZHENTU_REGISTRY_URL", "http://127.0.0.1:4020"),
        help="阵图地址",
    )
    parser.add_argument(
        "--registry-token",
        default=os.environ.get("ZHENTU_REGISTRY_TOKEN", ""),
        help="阵图 token（默认读 ZHENTU_REGISTRY_TOKEN）",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP 超时秒数")
    args = parser.parse_args(argv)

    try:
        nodes = _parse_nodes(args.node)
    except ValueError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    containers = args.containers or list(DEFAULT_CONTAINERS)
    summary: list[str] = [
        f"time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"out={out}",
        f"containers={','.join(containers)}",
        f"nodes={','.join(f'{i}={u}' for i, u in nodes) or '-'}",
        "",
    ]
    attempted = 0
    succeeded = 0

    # 诊断快照
    for name, cmd in (
        ("docker-version.txt", ["docker", "version"]),
        ("compose-ps.txt", ["docker", "compose", "ps"]),
        ("uname.txt", ["uname", "-a"]),
    ):
        code, output = _run(cmd)
        (out / name).write_text(output, encoding="utf-8")
        summary.append(f"[{'OK' if code == 0 else 'WARN'}] {name} exit={code}")

    # 容器日志
    for container in containers:
        attempted += 1
        code, output = _run(["docker", "logs", "--since", args.since, container])
        safe_name = container.replace("/", "_")
        target = out / f"container-{safe_name}.log"
        target.write_text(output, encoding="utf-8")
        if code == 0:
            succeeded += 1
            summary.append(f"[OK] container {container} -> {target.name}")
        else:
            summary.append(f"[FAIL] container {container} exit={code}")

    # 节点运行事件
    for node_id, url in nodes:
        attempted += 1
        ok, data = _fetch_json(f"{url}/v1/logs?limit=200", args.node_token, args.timeout)
        target = out / f"node-{node_id}-logs.json"
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) if ok else str(data),
            encoding="utf-8",
        )
        if ok:
            succeeded += 1
            summary.append(f"[OK] node {node_id} -> {target.name}")
        else:
            summary.append(f"[FAIL] node {node_id}: {data}")

    # 阵图注册视图
    if args.registry_token:
        attempted += 1
        ok, data = _fetch_json(
            f"{args.registry.rstrip('/')}/zhentu/v1/nodes", args.registry_token, args.timeout
        )
        target = out / "zhentu-nodes.json"
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) if ok else str(data),
            encoding="utf-8",
        )
        if ok:
            succeeded += 1
            summary.append(f"[OK] registry -> {target.name}")
        else:
            summary.append(f"[FAIL] registry: {data}")
    else:
        summary.append("[SKIP] registry：未提供 ZHENTU_REGISTRY_TOKEN")

    summary.append("")
    summary.append(f"attempted={attempted} succeeded={succeeded}")
    (out / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    print(f"\n日志已写入：{out}")
    return 0 if attempted == 0 or succeeded > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
