#!/usr/bin/env python3
"""碧游宫联阵协议 · 桌面端最小验证服务。

只依赖 Python 标准库，用于在真正接入 Electron/Android 之前验证：

- UDP 广播发现（mDNS 暂未实现，先验证兜底路径）
- 开放阵 / 闭阵入阵
- 心跳与租约
- 离阵 / 踢人 / 散阵
- SSE 事件流

协议基准：docs/lan-formation-contract.md
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import queue
import secrets
import socket
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PROTOCOL = 1
LEASE_TTL_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
DISCOVERY_INTERVAL_SECONDS = 2.0
PBKDF2_ITERATIONS = 200_000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def hash_passcode(passcode: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def verify_passcode(passcode: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def is_valid_device(device: Any) -> bool:
    if not isinstance(device, dict):
        return False
    for key, limit in (("device_id", 128), ("name", 64), ("platform", 32), ("app_version", 32)):
        value = device.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return False
    return True


@dataclass
class Member:
    member_id: str
    device_id: str
    name: str
    platform: str
    app_version: str
    role: str
    agent: dict[str, Any]
    token_hash: str
    joined_at: str
    last_seen: float
    lease_expires_at: float
    status: str = "active"
    events: queue.Queue[dict[str, Any] | None] = field(default_factory=queue.Queue)


class Room:
    def __init__(
        self,
        *,
        name: str,
        mode: str,
        passcode: str,
        capacity: int,
        host_name: str,
        host_platform: str,
        app_version: str,
        host_token: str,
    ) -> None:
        self.protocol = PROTOCOL
        self.room_id = str(uuid.uuid4())
        self.name = name
        self.mode = mode
        self.capacity = capacity
        self.host_name = host_name
        self.host_platform = host_platform
        self.app_version = app_version
        self.host_token = host_token
        self.started_at = utc_now_iso()
        self.http_port = 0
        self.active = True
        self.lock = threading.RLock()
        self.members: dict[str, Member] = {}
        self.bans: set[str] = set()
        self.rate: dict[tuple[str, str], deque[float]] = {}
        self.stop_event = threading.Event()
        self.pass_salt = ""
        self.pass_hash = ""
        if mode == "closed":
            self.pass_salt, self.pass_hash = hash_passcode(passcode)

    def room_view(self) -> dict[str, Any]:
        with self.lock:
            players = len(self.members)
        return {
            "protocol": self.protocol,
            "room_id": self.room_id,
            "name": self.name,
            "mode": self.mode,
            "host": {
                "name": self.host_name,
                "platform": self.host_platform,
                "app_version": self.app_version,
            },
            "capacity": self.capacity,
            "players": players,
            "started_at": self.started_at,
            "lease": {
                "ttl_seconds": LEASE_TTL_SECONDS,
                "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            },
        }

    def discovery_view(self) -> dict[str, Any]:
        with self.lock:
            players = len(self.members)
        return {
            "t": "room",
            "v": PROTOCOL,
            "room_id": self.room_id,
            "name": self.name,
            "mode": self.mode,
            "protocol": PROTOCOL,
            "capacity": self.capacity,
            "players": players,
            "host": self.host_name,
            "app": self.app_version,
            "port": self.http_port,
            "started_at": self.started_at,
        }

    def allow_join_attempt(self, key: tuple[str, str]) -> bool:
        now = time.time()
        with self.lock:
            attempts = self.rate.setdefault(key, deque())
            while attempts and now - attempts[0] > RATE_LIMIT_WINDOW_SECONDS:
                attempts.popleft()
            if len(attempts) >= RATE_LIMIT_ATTEMPTS:
                return False
            attempts.append(now)
            return True

    def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        with self.lock:
            members = list(self.members.values())
        event = {"type": event_type, "data": data}
        for member in members:
            member.events.put(event)

    def authenticate(self, token: str) -> Member | None:
        if not token:
            return None
        token_hash = sha256_hex(token)
        with self.lock:
            for member in self.members.values():
                if hmac.compare_digest(member.token_hash, token_hash):
                    return member
        return None

    def join(self, payload: Any, ip: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        device = payload.get("device") if isinstance(payload, dict) else None
        device_id = device.get("device_id", "") if isinstance(device, dict) else ""
        rate_key = (ip, device_id if isinstance(device_id, str) and device_id else "-")
        if not self.allow_join_attempt(rate_key):
            return 429, error("rate_limited", "入阵尝试过于频繁"), {"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)}

        if not isinstance(payload, dict):
            return 400, error("invalid_request", "请求体必须是 JSON 对象"), {}
        if payload.get("protocol") != PROTOCOL:
            return 409, error("protocol_mismatch", "协议版本不兼容"), {}
        if payload.get("room_id") != self.room_id:
            return 404, error("room_not_found", "阵不存在或已更换"), {}

        with self.lock:
            if not self.active:
                return 409, error("room_closed", "阵已散"), {}
            existing = next(
                (m for m in self.members.values() if m.device_id == device_id),
                None,
            ) if isinstance(device_id, str) and device_id else None
            effective_players = len(self.members) - (1 if existing else 0)
            if effective_players >= self.capacity:
                return 409, error("room_full", "阵已满"), {}

        if not is_valid_device(device):
            return 400, error("invalid_device", "设备字段缺失或非法"), {}

        if device_id in self.bans:
            return 403, error("banned", "该设备已被阵主移出本次阵"), {}

        if self.mode == "closed":
            passcode = payload.get("passcode")
            if not isinstance(passcode, str) or not passcode:
                return 401, error("passcode_required", "闭阵需要阵符"), {}
            if not verify_passcode(passcode, self.pass_salt, self.pass_hash):
                return 403, error("passcode_invalid", "阵符错误"), {}

        if existing is not None:
            with self.lock:
                self.members.pop(existing.member_id, None)
            existing.events.put(None)
            self.broadcast(
                "member.left",
                {"member_id": existing.member_id, "name": existing.name, "reason": "replaced"},
            )

        token = secrets.token_urlsafe(32)
        now = time.time()
        agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
        root = agent.get("root", "outer")
        role = root if root in ("inner", "outer") else "outer"
        member = Member(
            member_id=str(uuid.uuid4()),
            device_id=device_id,
            name=device["name"],
            platform=device["platform"],
            app_version=device["app_version"],
            role=role,
            agent=agent,
            token_hash=sha256_hex(token),
            joined_at=utc_now_iso(),
            last_seen=now,
            lease_expires_at=now + LEASE_TTL_SECONDS,
        )
        with self.lock:
            self.members[member.member_id] = member

        self.broadcast(
            "member.joined",
            {
                "member_id": member.member_id,
                "name": member.name,
                "platform": member.platform,
                "role": member.role,
            },
        )
        return 200, {
            "ok": True,
            "member_id": member.member_id,
            "session_token": token,
            "role": member.role,
            "room": self.room_view(),
            "lease": {
                "ttl_seconds": LEASE_TTL_SECONDS,
                "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            },
        }, {}

    def heartbeat(self, member: Member) -> tuple[int, dict[str, Any]]:
        now = time.time()
        with self.lock:
            current = self.members.get(member.member_id)
            if current is None:
                return 401, error("invalid_token", "会话不存在或已失效")
            current.last_seen = now
            current.lease_expires_at = now + LEASE_TTL_SECONDS
            current.status = "active"
            players = len(self.members)
            expires_at = current.lease_expires_at
        return 200, {
            "ok": True,
            "server_time": utc_now_iso(),
            "lease_expires_at": datetime.fromtimestamp(expires_at, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "players": players,
        }

    def leave(self, member: Member) -> tuple[int, dict[str, Any]]:
        with self.lock:
            removed = self.members.pop(member.member_id, None)
        if removed is not None:
            removed.events.put(None)
            self.broadcast(
                "member.left",
                {"member_id": removed.member_id, "name": removed.name, "reason": "left"},
            )
        return 200, {"ok": True}

    def kick(self, member_id: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            removed = self.members.pop(member_id, None)
            if removed is not None:
                self.bans.add(removed.device_id)
        if removed is None:
            return 404, error("member_not_found", "成员不存在")
        removed.events.put(
            {
                "type": "member.kicked",
                "data": {"member_id": removed.member_id, "reason": "kicked"},
            }
        )
        self.broadcast(
            "member.kicked",
            {"member_id": removed.member_id, "name": removed.name, "reason": "kicked"},
        )
        return 200, {"ok": True}

    def disband(self) -> bool:
        with self.lock:
            if not self.active:
                return False
            self.active = False
            members = list(self.members.values())
        event = {
            "type": "room.closed",
            "data": {"room_id": self.room_id, "reason": "host_disbanded"},
        }
        for member in members:
            member.events.put(event)
        self.stop_event.set()
        return True

    def member_views(self) -> list[dict[str, Any]]:
        with self.lock:
            members = list(self.members.values())
        return [
            {
                "member_id": m.member_id,
                "device_id": m.device_id,
                "name": m.name,
                "platform": m.platform,
                "app_version": m.app_version,
                "role": m.role,
                "agent": m.agent,
                "status": m.status,
                "joined_at": m.joined_at,
                "last_seen": datetime.fromtimestamp(m.last_seen, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "lease_expires_at": datetime.fromtimestamp(m.lease_expires_at, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            for m in members
        ]


class DiscoveryService:
    def __init__(self, room: Room, port: int) -> None:
        self.room = room
        self.port = port
        self.sock: socket.socket | None = None

    def start(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(0.5)
        except OSError as exc:
            print(f"[WARN] UDP 发现不可用：{exc}", file=sys.stderr)
            return False
        self.sock = sock
        threading.Thread(target=self._run, name="lan-discovery", daemon=True).start()
        return True

    def _run(self) -> None:
        assert self.sock is not None
        last_announce = 0.0
        while not self.room.stop_event.is_set():
            now = time.time()
            if self.room.active and now - last_announce >= DISCOVERY_INTERVAL_SECONDS:
                self._send(self.room.discovery_view(), ("255.255.255.255", self.port))
                last_announce = now
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if message.get("t") == "discover" and message.get("v") == PROTOCOL:
                self._send(self.room.discovery_view(), addr)

    def _send(self, payload: dict[str, Any], address: tuple[str, int]) -> None:
        if self.sock is None:
            return
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.sock.sendto(body, address)
        except OSError:
            pass

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class FormationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], room: Room, verbose: bool) -> None:
        super().__init__(address, FormationHandler)
        self.room = room
        self.verbose = verbose

    def shutdown_later(self, delay: float = 0.5) -> None:
        threading.Timer(delay, self.shutdown).start()


class FormationHandler(BaseHTTPRequestHandler):
    server_version = "BiyouLAN/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def room(self) -> Room:
        return self.server.room  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > 1_000_000:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[7:].strip()

    def _host_ok(self) -> bool:
        ip = self.client_address[0]
        loopback = ip in ("127.0.0.1", "::1") or ip.startswith("::ffff:127.")
        if not loopback:
            return False
        supplied = self.headers.get("X-Biyou-Host-Token", "")
        return hmac.compare_digest(supplied, self.room.host_token)

    def _path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self) -> None:
        path = self._path()
        if path == "/":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "biyou-lan-formation",
                    "protocol": PROTOCOL,
                    "endpoints": [
                        "GET /lan/v1/health",
                        "GET /lan/v1/room",
                        "POST /lan/v1/join",
                        "POST /lan/v1/heartbeat",
                        "POST /lan/v1/leave",
                        "GET /lan/v1/events",
                    ],
                },
            )
            return
        if path == "/lan/v1/health":
            self._send_json(200, {"ok": True, "service": "biyou-lan-formation", "protocol": PROTOCOL})
            return
        if path == "/lan/v1/room":
            self._send_json(200, {"ok": True, "room": self.room.room_view()})
            return
        if path == "/lan/v1/members":
            if not self._host_ok():
                self._send_json(403, error("forbidden", "阵主接口仅限本机"))
                return
            self._send_json(200, {"ok": True, "members": self.room.member_views()})
            return
        if path == "/lan/v1/events":
            self._stream_events()
            return
        self._send_json(404, error("not_found", "端点不存在"))

    def do_POST(self) -> None:
        path = self._path()
        if path == "/lan/v1/join":
            payload = self._read_json()
            status, body, headers = self.room.join(payload, self.client_address[0])
            self._send_json(status, body, headers)
            return
        if path == "/lan/v1/heartbeat":
            member = self.room.authenticate(self._bearer())
            if member is None:
                self._send_json(401, error("invalid_token", "会话不存在或已失效"))
                return
            status, body = self.room.heartbeat(member)
            self._send_json(status, body)
            return
        if path == "/lan/v1/leave":
            member = self.room.authenticate(self._bearer())
            if member is None:
                self._send_json(401, error("invalid_token", "会话不存在或已失效"))
                return
            status, body = self.room.leave(member)
            self._send_json(status, body)
            return
        if path == "/lan/v1/disband":
            if not self._host_ok():
                self._send_json(403, error("forbidden", "阵主接口仅限本机"))
                return
            changed = self.room.disband()
            self._send_json(200, {"ok": True, "closed": changed})
            if changed:
                self.server.shutdown_later()  # type: ignore[attr-defined]
            return
        if path.startswith("/lan/v1/members/") and path.endswith("/kick"):
            if not self._host_ok():
                self._send_json(403, error("forbidden", "阵主接口仅限本机"))
                return
            member_id = path[len("/lan/v1/members/") : -len("/kick")]
            status, body = self.room.kick(member_id)
            self._send_json(status, body)
            return
        self._send_json(404, error("not_found", "端点不存在"))

    def _stream_events(self) -> None:
        member = self.room.authenticate(self._bearer())
        if member is None:
            self._send_json(401, error("invalid_token", "会话不存在或已失效"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                try:
                    event = member.events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                event_type = event.get("type", "message")
                data = event.get("data", {})
                chunk = (
                    f"event: {event_type}\n"
                    f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                ).encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
                if event_type in ("member.kicked", "room.closed"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True


def expiry_loop(room: Room) -> None:
    while not room.stop_event.wait(5):
        now = time.time()
        with room.lock:
            offline = [
                member
                for member in room.members.values()
                if member.status == "active" and member.lease_expires_at < now
            ]
            for member in offline:
                member.status = "offline"
        for member in offline:
            room.broadcast(
                "member.offline",
                {"member_id": member.member_id, "name": member.name},
            )


def create_server(bind: str, port: int, room: Room, verbose: bool) -> FormationHTTPServer:
    last_error: OSError | None = None
    for candidate in range(port, port + 100):
        try:
            return FormationHTTPServer((bind, candidate), room, verbose)
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"无法绑定 HTTP 端口 {port}-{port + 99}: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="碧游宫联阵协议 · 最小桌面验证服务")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP 绑定地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=4022, help="HTTP 起始端口，默认 4022")
    parser.add_argument("--discovery-port", type=int, default=4021, help="UDP 发现端口，默认 4021")
    parser.add_argument("--name", default="碧游宫", help="阵名")
    parser.add_argument("--mode", choices=("open", "closed"), default="open", help="开放阵/闭阵")
    parser.add_argument("--passcode", default="", help="闭阵阵符（4-64 字符）")
    parser.add_argument("--capacity", type=int, default=8, help="最大仙徒数，默认 8")
    parser.add_argument("--host-name", default=socket.gethostname(), help="阵主显示名")
    parser.add_argument("--host-platform", default=sys.platform, help="阵主平台")
    parser.add_argument("--app-version", default="0.1.0", help="客户端版本")
    parser.add_argument("--host-token", default="", help="阵主令牌；不填则随机生成")
    parser.add_argument("--no-discovery", action="store_true", help="不启动 UDP 发现")
    parser.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    args = parser.parse_args()
    if args.capacity < 1:
        parser.error("--capacity 必须大于 0")
    if args.mode == "closed" and not (4 <= len(args.passcode) <= 64):
        parser.error("闭阵的 --passcode 必须为 4-64 字符")
    if args.mode == "open" and args.passcode:
        parser.error("开放阵不需要 --passcode")
    return args


def main() -> int:
    args = parse_args()
    host_token = args.host_token or secrets.token_urlsafe(24)
    room = Room(
        name=args.name,
        mode=args.mode,
        passcode=args.passcode,
        capacity=args.capacity,
        host_name=args.host_name,
        host_platform=args.host_platform,
        app_version=args.app_version,
        host_token=host_token,
    )
    server = create_server(args.bind, args.port, room, args.verbose)
    room.http_port = server.server_address[1]
    discovery: DiscoveryService | None = None
    if not args.no_discovery:
        discovery = DiscoveryService(room, args.discovery_port)
        discovery.start()
    threading.Thread(target=expiry_loop, args=(room,), name="lease-expiry", daemon=True).start()

    print("== 碧游宫联阵 · 最小验证服务 ==")
    print(f"阵名    : {room.name}")
    print(f"模式    : {room.mode}")
    print(f"阵号    : {room.room_id}")
    print(f"HTTP    : http://{args.bind}:{room.http_port}/lan/v1")
    if discovery is not None:
        print(f"UDP发现 : 0.0.0.0:{args.discovery_port}（mDNS 未实现，先用 UDP 兜底）")
    else:
        print("UDP发现 : 已关闭")
    print(f"阵主令牌: {host_token}")
    if room.mode == "open":
        print("提示    : 开放阵无需阵符，校验通过即入阵；阵主可踢人。")
    print("按 Ctrl-C 停止。", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止……", flush=True)
    finally:
        room.active = False
        room.stop_event.set()
        if discovery is not None:
            discovery.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
