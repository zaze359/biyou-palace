#!/usr/bin/env python3
"""碧游宫联阵协议 · 可重复验收脚本。

用法：
    python3 scripts/lan_formation_acceptance.py
    python3 scripts/lan_formation_acceptance.py --verbose

只依赖 Python 标准库。会临时启动 scripts/lan_formation_server.py，
使用随机端口，不影响正在运行的联阵服务。
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "lan_formation_server.py"
HOST_TOKEN = "acceptance-host-token"


def free_tcp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def http_call(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    host_token: str | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if host_token:
        request.add_header("X-Biyou-Host-Token", host_token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


class Server:
    def __init__(self, mode: str, *, capacity: int = 8, passcode: str = "", discovery: bool = False) -> None:
        self.mode = mode
        self.capacity = capacity
        self.passcode = passcode
        self.discovery = discovery
        self.http_port = free_tcp_port()
        self.udp_port = free_udp_port() if discovery else 0
        self.base = f"http://127.0.0.1:{self.http_port}"
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "Server":
        args = [
            sys.executable,
            str(SERVER),
            "--mode",
            self.mode,
            "--port",
            str(self.http_port),
            "--host-token",
            HOST_TOKEN,
            "--capacity",
            str(self.capacity),
        ]
        if self.discovery:
            args.extend(["--discovery-port", str(self.udp_port)])
        else:
            args.append("--no-discovery")
        if self.passcode:
            args.extend(["--passcode", self.passcode])
        self.proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.wait_ready()
        return self

    def wait_ready(self, timeout: float = 8.0) -> None:
        assert self.proc is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务提前退出：\n{self.output()}")
            try:
                status, _ = http_call(self.base, "GET", "/lan/v1/health", timeout=0.3)
                if status == 200:
                    return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"服务未就绪：\n{self.output()}")

    def output(self) -> str:
        if self.proc is None or self.proc.stdout is None:
            return ""
        if self.proc.poll() is None:
            return ""
        return self.proc.stdout.read()

    def wait_exit(self, timeout: float = 4.0) -> bool:
        assert self.proc is not None
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if exc_type is not None:
            print(self.output(), file=sys.stderr)


def device(prefix: str = "android") -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return {
        "device_id": f"{prefix}-{suffix}",
        "name": f"验收设备 {suffix}",
        "platform": "android",
        "app_version": "0.1.0",
    }


def join_body(room_id: str, dev: dict[str, str], passcode: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol": 1,
        "room_id": room_id,
        "device": dev,
        "agent": {"root": "outer", "skills": ["chat"], "models": ["qwen3-0.6b"], "max_concurrency": 1},
    }
    if passcode is not None:
        body["passcode"] = passcode
    return body


def assert_status(actual: int, expected: int, context: str, body: Any = None) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: 期望 HTTP {expected}，实际 {actual}；响应={body}")


def assert_error(status: int, body: dict[str, Any], expected_status: int, expected_code: str, context: str) -> None:
    assert_status(status, expected_status, context, body)
    actual = body.get("error", {}).get("code")
    if actual != expected_code:
        raise AssertionError(f"{context}: 期望错误码 {expected_code}，实际 {actual}；响应={body}")


def test_open_mode() -> None:
    with Server("open") as server:
        status, body = http_call(server.base, "GET", "/lan/v1/health")
        assert_status(status, 200, "health", body)

        status, body = http_call(server.base, "GET", "/lan/v1/room")
        assert_status(status, 200, "room", body)
        room_id = body["room"]["room_id"]

        status, body = http_call(server.base, "GET", "/lan/v1/members")
        assert_status(status, 403, "members without host token", body)

        status, body = http_call(server.base, "GET", "/lan/v1/members", host_token="wrong")
        assert_status(status, 403, "members wrong host token", body)

        dev_a = device("open-a")
        status, joined = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_a))
        assert_status(status, 200, "open join", joined)
        token_a = joined["session_token"]
        member_a = joined["member_id"]

        status, body = http_call(server.base, "POST", "/lan/v1/heartbeat", {}, token=token_a)
        assert_status(status, 200, "open heartbeat", body)
        if body.get("players") != 1:
            raise AssertionError(f"heartbeat players 期望 1，实际 {body.get('players')}")

        status, body = http_call(server.base, "GET", "/lan/v1/members", host_token=HOST_TOKEN)
        assert_status(status, 200, "members with host token", body)
        if len(body.get("members", [])) != 1:
            raise AssertionError(f"members 期望 1，实际 {body}")

        status, body = http_call(server.base, "POST", f"/lan/v1/members/{member_a}/kick", {}, host_token=HOST_TOKEN)
        assert_status(status, 200, "kick", body)

        status, body = http_call(server.base, "POST", "/lan/v1/heartbeat", {}, token=token_a)
        assert_error(status, body, 401, "invalid_token", "heartbeat after kick")

        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_a))
        assert_error(status, body, 403, "banned", "rejoin after kick")

        dev_b = device("open-b")
        status, joined_b = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_b))
        assert_status(status, 200, "second open join", joined_b)

        status, body = http_call(server.base, "POST", "/lan/v1/leave", {}, token=joined_b["session_token"])
        assert_status(status, 200, "leave", body)

        status, body = http_call(server.base, "POST", "/lan/v1/heartbeat", {}, token=joined_b["session_token"])
        assert_error(status, body, 401, "invalid_token", "heartbeat after leave")

        status, body = http_call(server.base, "POST", "/lan/v1/join", {**join_body(room_id, dev_b), "protocol": 9})
        assert_error(status, body, 409, "protocol_mismatch", "protocol mismatch")

        status, body = http_call(server.base, "POST", "/lan/v1/join", {**join_body(room_id, dev_b), "room_id": "missing"})
        assert_error(status, body, 404, "room_not_found", "room not found")

        status, body = http_call(server.base, "POST", "/lan/v1/join", {"protocol": 1, "room_id": room_id, "device": {}})
        assert_error(status, body, 400, "invalid_device", "invalid device")

        rate_dev = device("rate-open")
        for attempt in range(5):
            status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, rate_dev))
            assert_status(status, 200, f"rate attempt {attempt + 1}", body)
        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, rate_dev))
        assert_error(status, body, 429, "rate_limited", "rate limited")

        status, body = http_call(server.base, "POST", "/lan/v1/disband", {}, host_token=HOST_TOKEN)
        assert_status(status, 200, "disband", body)
        if not body.get("closed"):
            raise AssertionError(f"disband 期望 closed=true，实际 {body}")
        if not server.wait_exit(4.0):
            raise AssertionError("散阵后服务未退出")


def test_closed_mode() -> None:
    with Server("closed", capacity=1, passcode="1234") as server:
        status, body = http_call(server.base, "GET", "/lan/v1/room")
        assert_status(status, 200, "closed room", body)
        room_id = body["room"]["room_id"]
        if body["room"]["mode"] != "closed":
            raise AssertionError(f"期望 closed，实际 {body['room']['mode']}")

        dev_a = device("closed-a")
        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_a))
        assert_error(status, body, 401, "passcode_required", "missing passcode")

        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_a, "wrong"))
        assert_error(status, body, 403, "passcode_invalid", "wrong passcode")

        status, joined = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_a, "1234"))
        assert_status(status, 200, "correct passcode", joined)

        dev_b = device("closed-b")
        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, dev_b, "1234"))
        assert_error(status, body, 409, "room_full", "room full")

        status, body = http_call(server.base, "POST", "/lan/v1/join", {**join_body(room_id, dev_b, "1234"), "protocol": 9})
        assert_error(status, body, 409, "protocol_mismatch", "closed protocol mismatch")

        status, body = http_call(
            server.base,
            "POST",
            f"/lan/v1/members/{joined['member_id']}/kick",
            {},
            host_token=HOST_TOKEN,
        )
        assert_status(status, 200, "closed kick to free capacity", body)

        rate_dev = device("rate-closed")
        for attempt in range(5):
            status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, rate_dev, "wrong"))
            assert_error(status, body, 403, "passcode_invalid", f"rate attempt {attempt + 1}")
        status, body = http_call(server.base, "POST", "/lan/v1/join", join_body(room_id, rate_dev, "wrong"))
        assert_error(status, body, 429, "rate_limited", "closed rate limited")


def test_udp_discovery() -> None:
    with Server("open", discovery=True) as server:
        assert server.udp_port > 0
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            client.bind(("127.0.0.1", 0))
            client.settimeout(0.5)
            message: dict[str, Any] | None = None
            for _ in range(8):
                client.sendto(json.dumps({"t": "discover", "v": 1}).encode(), ("127.0.0.1", server.udp_port))
                try:
                    data, _ = client.recvfrom(4096)
                    message = json.loads(data.decode("utf-8"))
                    break
                except socket.timeout:
                    continue
            if not message:
                raise AssertionError("UDP discover 未收到 room 响应")
            if message.get("t") != "room":
                raise AssertionError(f"期望 t=room，实际 {message}")
            if message.get("port") != server.http_port:
                raise AssertionError(f"期望 port={server.http_port}，实际 {message.get('port')}")
            if message.get("protocol") != 1:
                raise AssertionError(f"期望 protocol=1，实际 {message.get('protocol')}")
        finally:
            client.close()


def run_case(name: str, func: Callable[[], None]) -> bool:
    try:
        func()
        print(f"[PASS] {name}")
        return True
    except Exception as exc:  # noqa: BLE001 - 验收脚本需要打印所有失败
        print(f"[FAIL] {name}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="碧游宫联阵协议验收")
    parser.add_argument("--verbose", action="store_true", help="失败时打印服务端输出")
    args = parser.parse_args()

    cases = [
        ("O1-O13 开放阵：入阵/心跳/踢人/封禁/离阵/散阵", test_open_mode),
        ("C1-C8 闭阵：阵符/容量/协议/限速", test_closed_mode),
        ("D1 UDP 发现：discover → room", test_udp_discovery),
    ]
    results = [run_case(name, func) for name, func in cases]
    passed = sum(1 for ok in results if ok)
    failed = len(results) - passed
    print(f"\n结果：{passed}/{len(results)} 通过" + (f"，{failed} 失败" if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
