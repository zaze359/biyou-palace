#!/usr/bin/env python3
"""碧游宫网关管理员 CLI（NewAPI，纯标准库）。

把渠道与网关选项从「后台点选」变成「仓库声明 + 一条命令同步」，并提供验收所需的
渠道测试、日志检索与临时渠道清理能力。

鉴权优先级（高 → 低）：
  1. ``NEWAPI_ADMIN_TOKEN``（可选 ``NEWAPI_ADMIN_USER_ID``，默认 1）
  2. ``NEWAPI_ADMIN_USERNAME`` / ``NEWAPI_ADMIN_PASSWORD``
  3. ``NEWAPI_ADMIN_COOKIE_FILE`` 中的 ``new_api_refresh``（可选 ``NEWAPI_ADMIN_SESSION``）

用法：
    python3 deploy/newapi/gateway_admin.py channel list
    python3 deploy/newapi/gateway_admin.py channel sync --dry-run
    python3 deploy/newapi/gateway_admin.py option apply
    python3 deploy/newapi/gateway_admin.py log list --since 1795230000 --model qwen2.5:7b

安全约定：不打印 token / 密码；渠道 key 只从声明里的占位值或 ``key_env`` 指向的环境变量读取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_BASE_URL = os.environ.get("NEWAPI_BASE_URL", "http://127.0.0.1:3000")
DEFAULT_CHANNELS = HERE / "channels.json"
DEFAULT_OPTIONS = HERE / "gateway-options.json"
TEMP_CHANNEL_PREFIX = "biyou-verify-"

# 更新渠道时 NewAPI 会拒绝 status 字段（Invalid parameters），因此单独排除。
UPDATE_EXCLUDED_FIELDS = ("status",)
COMPARE_FIELDS = (
    "type",
    "base_url",
    "models",
    "group",
    "groups",
    "weight",
    "priority",
    "auto_ban",
    "model_mapping",
    "setting",
)


class AdminError(RuntimeError):
    """管理员 API 调用失败（可读信息，不含密钥）。"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"raw": raw.decode("utf-8", errors="replace")[:500]}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise AdminError(f"无法连接 NewAPI（{url}）：{exc.reason}") from exc


def _message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("message_key") or payload.get("raw") or payload)
    return str(payload)


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def _read_refresh_cookie(path: str) -> str:
    """从 Cookie 文件里取 new_api_refresh 的值。

    兼容三种写法：Netscape cookies.txt、``Cookie: ...`` 头、裸 ``new_api_refresh=...``。
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise AdminError(f"Cookie 文件不存在：{p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#") and "\t" not in text and "new_api_refresh=" not in text:
            continue
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1].strip()
        # Netscape 格式：域 \t 标志 \t 路径 \t 安全 \t 过期 \t 名称 \t 值
        parts = text.split("\t")
        if len(parts) >= 7 and parts[5] == "new_api_refresh":
            return parts[6]
        for chunk in text.lstrip("#HttpOnly_").split(";"):
            if chunk.strip().startswith("new_api_refresh="):
                return chunk.split("=", 1)[1].strip()
    raise AdminError(f"Cookie 文件里没有 new_api_refresh：{p}")


def _auth_headers(token: str, user_id: int) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "New-Api-User": str(user_id),
    }


class NewApiAdmin:
    """NewAPI 管理 API 的最小客户端。"""

    def __init__(self, base_url: str, token: str, user_id: int = 1, *, source: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_id = int(user_id)
        self.source = source
        self.timeout = timeout

    # -- 基础调用 ---------------------------------------------------------- #
    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        return _http_json(method, url, body=body, headers=_auth_headers(self.token, self.user_id), timeout=self.timeout)

    def call_ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, payload = self.call(method, path, body)
        if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
            raise AdminError(f"{method} {path} 失败（HTTP {status}）：{_message(payload)}")
        return payload.get("data")

    # -- 渠道 -------------------------------------------------------------- #
    def list_channels(self) -> list[dict[str, Any]]:
        data = self.call_ok("GET", "/api/channel/?p=0&page_size=50")
        if isinstance(data, list):
            return data
        return list((data or {}).get("items") or [])

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        data = self.call_ok("GET", f"/api/channel/{channel_id}")
        return data if isinstance(data, dict) else None

    def find_channel(self, selector: str) -> dict[str, Any] | None:
        channels = self.list_channels()
        if selector.isdigit():
            target = int(selector)
            for ch in channels:
                if ch.get("id") == target:
                    return ch
            return None
        for ch in channels:
            if ch.get("name") == selector:
                return ch
        return None

    def create_channel(self, spec: "ChannelSpec") -> int:
        body = {"mode": "single", "channel": spec.create_payload()}
        data = self.call_ok("POST", "/api/channel/", body)
        if isinstance(data, dict) and data.get("id"):
            return int(data["id"])
        if isinstance(data, dict) and data.get("ids"):
            return int(data["ids"][0])
        raise AdminError(f"渠道 {spec.name} 创建成功但未返回 id：{data}")

    def update_channel(self, channel_id: int, spec: "ChannelSpec") -> None:
        payload = spec.update_payload(channel_id)
        self.call_ok("PUT", "/api/channel/", payload)

    def delete_channel(self, channel_id: int) -> None:
        self.call_ok("DELETE", f"/api/channel/{channel_id}")

    def set_channel_status(self, channel_id: int, status: int) -> None:
        """启停渠道；NewAPI 只接受 1/2/3，0 会被判为 Invalid parameters。"""
        self.call_ok("POST", f"/api/channel/{channel_id}/status", {"status": int(status)})

    def test_channel(self, channel_id: int, model: str) -> dict[str, Any]:
        status, payload = self.call("GET", f"/api/channel/test/{channel_id}?model={urllib.parse.quote(model)}")
        if status != 200 or not isinstance(payload, dict):
            raise AdminError(f"渠道测试失败（HTTP {status}）：{_message(payload)}")
        return payload

    # -- 选项 -------------------------------------------------------------- #
    def get_options(self) -> dict[str, str]:
        data = self.call_ok("GET", "/api/option/")
        return {str(o.get("key")): str(o.get("value", "")) for o in (data or [])}

    def set_option(self, key: str, value: str) -> None:
        self.call_ok("PUT", "/api/option/", {"key": key, "value": str(value)})

    # -- 日志 -------------------------------------------------------------- #
    def list_logs(
        self,
        *,
        since: int = 0,
        model: str | None = None,
        channel: int | None = None,
        limit: int = 200,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """按时间倒序取消费日志；since 为 Unix 秒（含）。

        NewAPI 的日志按 created_at 倒序分页，遇到早于 since 的条目即停止翻页。
        """
        items: list[dict[str, Any]] = []
        page = 0
        while len(items) < limit:
            data = self.call_ok("GET", f"/api/log/?p={page}&page_size={page_size}&type=2")
            batch = list((data or {}).get("items") or [])
            if not batch:
                break
            reached_end = False
            for entry in batch:
                created = int(entry.get("created_at") or 0)
                if created < since:
                    reached_end = True
                    break
                if model and entry.get("model_name") != model:
                    continue
                if channel is not None and entry.get("channel") != channel:
                    continue
                items.append(entry)
                if len(items) >= limit:
                    break
            if reached_end:
                break
            page += 1
        return items


def login(base_url: str, username: str, password: str, *, timeout: float = 60.0) -> NewApiAdmin:
    status, payload = _http_json(
        "POST",
        f"{base_url.rstrip('/')}/api/user/login",
        body={"username": username, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        raise AdminError(f"管理员登录失败（HTTP {status}）：{_message(payload)}")
    data = payload.get("data") or {}
    token = data.get("access_token")
    if not token:
        raise AdminError("管理员登录成功但响应里没有 access_token")
    user_id = (data.get("user") or {}).get("id") or 1
    return NewApiAdmin(base_url, token, int(user_id), source="login", timeout=timeout)


def refresh(base_url: str, cookie_file: str, *, timeout: float = 60.0) -> NewApiAdmin:
    cookie = f"new_api_refresh={_read_refresh_cookie(cookie_file)}"
    headers = {"Content-Type": "application/json", "Cookie": cookie}
    session = os.environ.get("NEWAPI_ADMIN_SESSION", "").strip()
    if session:
        headers["X-Auth-Session"] = session
    status, payload = _http_json(
        "POST",
        f"{base_url.rstrip('/')}/api/user/auth/refresh",
        body={},
        headers=headers,
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        raise AdminError(f"刷新会话失败（HTTP {status}）：{_message(payload)}")
    data = payload.get("data") or {}
    token = data.get("access_token")
    if not token:
        raise AdminError("刷新成功但响应里没有 access_token")
    user_id = (data.get("user") or {}).get("id") or os.environ.get("NEWAPI_ADMIN_USER_ID", "1")
    return NewApiAdmin(base_url, token, int(user_id), source="refresh", timeout=timeout)


def resolve_admin(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 60.0) -> NewApiAdmin:
    token = os.environ.get("NEWAPI_ADMIN_TOKEN", "").strip()
    if token:
        user_id = int(os.environ.get("NEWAPI_ADMIN_USER_ID", "1") or 1)
        return NewApiAdmin(base_url, token, user_id, source="env:NEWAPI_ADMIN_TOKEN", timeout=timeout)

    username = os.environ.get("NEWAPI_ADMIN_USERNAME", "").strip()
    password = os.environ.get("NEWAPI_ADMIN_PASSWORD", "").strip()
    if username and password:
        return login(base_url, username, password, timeout=timeout)

    cookie_file = os.environ.get("NEWAPI_ADMIN_COOKIE_FILE", "").strip()
    if cookie_file:
        return refresh(base_url, cookie_file, timeout=timeout)

    raise AdminError(
        "未找到管理员凭据：请设置 NEWAPI_ADMIN_TOKEN，或 NEWAPI_ADMIN_USERNAME/NEWAPI_ADMIN_PASSWORD，"
        "或 NEWAPI_ADMIN_COOKIE_FILE（含 new_api_refresh）"
    )


# --------------------------------------------------------------------------- #
# 渠道声明
# --------------------------------------------------------------------------- #
def _as_json_text(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json_text(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class ChannelSpec:
    name: str
    type: int
    base_url: str
    models: list[str]
    group: str = "default"
    groups: list[str] = field(default_factory=list)
    weight: int = 0
    priority: int = 0
    auto_ban: int = 1
    key: str = ""
    key_env: str = ""
    model_mapping: dict[str, str] = field(default_factory=dict)
    setting: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> "ChannelSpec":
        models = entry.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        group = str(entry.get("group") or "default")
        groups = entry.get("groups") or [group]
        mapping = entry.get("model_mapping") or {}
        setting = entry.get("setting") or {}
        if isinstance(mapping, str):
            mapping = _parse_json_text(mapping)
        if isinstance(setting, str):
            setting = _parse_json_text(setting)
        return cls(
            name=str(entry["name"]),
            type=int(entry["type"]),
            base_url=str(entry.get("base_url") or ""),
            models=[str(m) for m in models],
            group=group,
            groups=[str(g) for g in groups],
            weight=int(entry.get("weight") or 0),
            priority=int(entry.get("priority") or 0),
            auto_ban=int(entry.get("auto_ban", 1)),
            key=str(entry.get("key") or ""),
            key_env=str(entry.get("key_env") or ""),
            model_mapping={str(k): str(v) for k, v in mapping.items()},
            setting=setting,
        )

    def resolved_key(self) -> str:
        if self.key_env:
            value = os.environ.get(self.key_env, "").strip()
            if not value:
                raise AdminError(f"渠道 {self.name} 声明了 key_env={self.key_env}，但环境变量为空")
            return value
        return self.key

    def _common_payload(self) -> dict[str, Any]:
        key = self.resolved_key()
        if not key:
            raise AdminError(
                f"渠道 {self.name} 的 key 为空；NewAPI 创建渠道要求非空 key（无鉴权上游填占位值即可）"
            )
        return {
            "name": self.name,
            "type": self.type,
            "key": key,
            "base_url": self.base_url,
            "models": ",".join(self.models),
            "group": self.group,
            "groups": self.groups or [self.group],
            "auto_ban": self.auto_ban,
            "priority": self.priority,
            "weight": self.weight,
            "model_mapping": _as_json_text(self.model_mapping),
            "setting": _as_json_text(self.setting) or "{}",
        }

    def create_payload(self) -> dict[str, Any]:
        return {**self._common_payload(), "status": 1}

    def update_payload(self, channel_id: int) -> dict[str, Any]:
        payload = {"id": int(channel_id), **self._common_payload()}
        for extra in UPDATE_EXCLUDED_FIELDS:
            payload.pop(extra, None)
        return payload

    def comparable(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "base_url": self.base_url,
            "models": sorted(self.models),
            "group": self.group,
            "groups": sorted(self.groups or [self.group]),
            "weight": self.weight,
            "priority": self.priority,
            "auto_ban": self.auto_ban,
            "model_mapping": self.model_mapping,
            "setting": self.setting,
        }


def existing_comparable(channel: dict[str, Any]) -> dict[str, Any]:
    models = channel.get("models") or ""
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    group = str(channel.get("group") or "default")
    groups = channel.get("groups") or [group]
    return {
        "type": int(channel.get("type") or 0),
        "base_url": str(channel.get("base_url") or ""),
        "models": sorted(str(m) for m in models),
        "group": group,
        "groups": sorted(str(g) for g in groups),
        "weight": int(channel.get("weight") or 0),
        "priority": int(channel.get("priority") or 0),
        "auto_ban": int(channel.get("auto_ban") or 0),
        "model_mapping": _parse_json_text(channel.get("model_mapping")),
        "setting": _parse_json_text(channel.get("setting")),
    }


def diff_fields(spec: ChannelSpec, existing: dict[str, Any]) -> list[str]:
    want = spec.comparable()
    have = existing_comparable(existing)
    return [name for name in COMPARE_FIELDS if want.get(name) != have.get(name)]


def load_channels_config(path: Path) -> list[ChannelSpec]:
    if not path.is_file():
        raise AdminError(f"渠道声明文件不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AdminError(f"渠道声明不是合法 JSON：{path}（{exc}）") from exc
    entries = raw.get("channels") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise AdminError(f"渠道声明为空：{path}")
    return [ChannelSpec.from_dict(entry) for entry in entries]


def load_options_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise AdminError(f"选项声明文件不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AdminError(f"选项声明不是合法 JSON：{path}（{exc}）") from exc
    options = raw.get("options") if isinstance(raw, dict) else raw
    if not isinstance(options, dict) or not options:
        raise AdminError(f"选项声明为空：{path}")
    return {str(k): str(v) for k, v in options.items()}


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def sync_channels(admin: NewApiAdmin, specs: list[ChannelSpec], *, dry_run: bool) -> list[dict[str, Any]]:
    existing = {str(ch.get("name")): ch for ch in admin.list_channels()}
    results: list[dict[str, Any]] = []
    for spec in specs:
        current = existing.get(spec.name)
        if current is None:
            if dry_run:
                results.append({"name": spec.name, "action": "would-create", "fields": []})
            else:
                channel_id = admin.create_channel(spec)
                results.append({"name": spec.name, "action": "created", "channel_id": channel_id, "fields": []})
            continue

        changed = diff_fields(spec, current)
        if not changed:
            action = "unchanged"
        elif dry_run:
            action = "would-update"
        else:
            admin.update_channel(int(current["id"]), spec)
            action = "updated"

        # 声明即期望启用；同步会把被自动隔离的渠道重新拉起（显式同步才生效）。
        status = int(current.get("status") or 0)
        if status != 1:
            if dry_run:
                action = "would-enable" if action == "unchanged" else f"{action}+would-enable"
            else:
                admin.set_channel_status(int(current["id"]), 1)
                action = "enabled" if action == "unchanged" else f"{action}+enabled"
        results.append(
            {"name": spec.name, "action": action, "channel_id": current.get("id"), "fields": changed}
        )
    return results


def apply_options(admin: NewApiAdmin, desired: dict[str, str], *, dry_run: bool) -> list[dict[str, Any]]:
    current = admin.get_options()
    results: list[dict[str, Any]] = []
    for key, value in desired.items():
        before = current.get(key)
        if before == value:
            results.append({"key": key, "action": "unchanged", "before": before, "after": value})
        elif dry_run:
            results.append({"key": key, "action": "would-set", "before": before, "after": value})
        else:
            admin.set_option(key, value)
            results.append({"key": key, "action": "set", "before": before, "after": value})
    if not dry_run:
        after = admin.get_options()
        mismatched = [r["key"] for r in results if after.get(r["key"]) != desired[r["key"]]]
        if mismatched:
            raise AdminError(f"选项回读不一致：{', '.join(mismatched)}")
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return
    if isinstance(obj, list):
        for row in obj:
            print(json.dumps(row, ensure_ascii=False))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _channel_row(ch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ch.get("id"),
        "name": ch.get("name"),
        "type": ch.get("type"),
        "base_url": ch.get("base_url"),
        "models": ch.get("models"),
        "group": ch.get("group"),
        "weight": ch.get("weight"),
        "priority": ch.get("priority"),
        "auto_ban": ch.get("auto_ban"),
        "status": ch.get("status"),
    }


def cmd_channel(args: argparse.Namespace) -> int:
    admin = resolve_admin(args.base_url, timeout=args.timeout)
    if args.channel_cmd == "list":
        _print([_channel_row(ch) for ch in admin.list_channels()], args.json)
        return 0

    if args.channel_cmd == "sync":
        specs = load_channels_config(Path(args.config))
        results = sync_channels(admin, specs, dry_run=args.dry_run)
        if args.json:
            _print(results, True)
        else:
            for row in results:
                fields = f" fields={','.join(row['fields'])}" if row.get("fields") else ""
                print(f"[{row['action']}] {row['name']}{fields}")
        return 0

    if args.channel_cmd == "test":
        channel = admin.find_channel(args.selector)
        if channel is None:
            raise AdminError(f"渠道不存在：{args.selector}")
        model = args.model or str(channel.get("models") or "").split(",")[0]
        result = admin.test_channel(int(channel["id"]), model)
        _print({"channel": channel.get("name"), "model": model, **result}, args.json)
        return 0 if result.get("success") else 1

    if args.channel_cmd == "delete":
        channel = admin.find_channel(args.selector)
        if channel is None:
            raise AdminError(f"渠道不存在：{args.selector}")
        name = str(channel.get("name") or "")
        if not args.force and not name.startswith(TEMP_CHANNEL_PREFIX):
            raise AdminError(
                f"拒绝删除非临时渠道 {name}；验收临时渠道需以 {TEMP_CHANNEL_PREFIX} 开头，"
                "确需删除请加 --force"
            )
        admin.delete_channel(int(channel["id"]))
        _print({"deleted": name, "id": channel.get("id")}, args.json)
        return 0

    raise AdminError(f"未知 channel 子命令：{args.channel_cmd}")


def cmd_option(args: argparse.Namespace) -> int:
    admin = resolve_admin(args.base_url, timeout=args.timeout)
    if args.option_cmd == "get":
        options = admin.get_options()
        if args.keys:
            missing = [k for k in args.keys if k not in options]
            if missing:
                raise AdminError(f"选项不存在：{', '.join(missing)}")
            options = {k: options[k] for k in args.keys}
        _print(options, args.json)
        return 0

    if args.option_cmd == "set":
        changed = {}
        for pair in args.pairs:
            if "=" not in pair:
                raise AdminError(f"选项赋值格式应为 key=value：{pair}")
            key, value = pair.split("=", 1)
            admin.set_option(key.strip(), value)
            changed[key.strip()] = value
        options = admin.get_options()
        mismatched = [k for k, v in changed.items() if options.get(k) != v]
        if mismatched:
            raise AdminError(f"选项回读不一致：{', '.join(mismatched)}")
        _print(changed, args.json)
        return 0

    if args.option_cmd == "apply":
        desired = load_options_config(Path(args.config))
        results = apply_options(admin, desired, dry_run=args.dry_run)
        if args.json:
            _print(results, True)
        else:
            for row in results:
                print(f"[{row['action']}] {row['key']}: {row['before']!r} -> {row['after']!r}")
        return 0

    raise AdminError(f"未知 option 子命令：{args.option_cmd}")


def cmd_log(args: argparse.Namespace) -> int:
    admin = resolve_admin(args.base_url, timeout=args.timeout)
    logs = admin.list_logs(since=args.since, model=args.model, channel=args.channel, limit=args.limit)
    if args.json:
        _print(logs, True)
    else:
        for entry in logs:
            print(
                f"{entry.get('created_at')} channel={entry.get('channel')} "
                f"name={entry.get('channel_name')} model={entry.get('model_name')} "
                f"request_id={entry.get('request_id')}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="碧游宫（NewAPI）管理员 CLI")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="NewAPI 地址（默认取 NEWAPI_BASE_URL）")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP 超时秒数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="cmd", required=True)

    channel = sub.add_parser("channel", help="渠道管理")
    channel_sub = channel.add_subparsers(dest="channel_cmd", required=True)

    channel_sub.add_parser("list", help="列出渠道")

    sync = channel_sub.add_parser("sync", help="按声明创建/更新渠道（不删除配置外渠道）")
    sync.add_argument("--config", default=str(DEFAULT_CHANNELS), help="渠道声明 JSON")
    sync.add_argument("--dry-run", action="store_true", help="只显示动作，不写入")

    test = channel_sub.add_parser("test", help="测试渠道连通性")
    test.add_argument("selector", help="渠道名或 id")
    test.add_argument("--model", default="", help="测试模型（默认取渠道第一个模型）")

    delete = channel_sub.add_parser("delete", help="删除渠道（默认只允许临时渠道）")
    delete.add_argument("selector", help="渠道名或 id")
    delete.add_argument("--force", action="store_true", help="允许删除非 biyou-verify- 渠道")

    option = sub.add_parser("option", help="网关选项")
    option_sub = option.add_subparsers(dest="option_cmd", required=True)
    opt_get = option_sub.add_parser("get", help="读取选项")
    opt_get.add_argument("keys", nargs="*", help="只显示指定键")
    opt_set = option_sub.add_parser("set", help="写入选项")
    opt_set.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    opt_apply = option_sub.add_parser("apply", help="应用基线选项声明")
    opt_apply.add_argument("--config", default=str(DEFAULT_OPTIONS), help="选项声明 JSON")
    opt_apply.add_argument("--dry-run", action="store_true", help="只显示动作，不写入")

    log = sub.add_parser("log", help="消费日志")
    log_sub = log.add_subparsers(dest="log_cmd", required=True)
    log_list = log_sub.add_parser("list", help="列出消费日志（type=2）")
    log_list.add_argument("--since", type=int, default=0, help="起始 Unix 秒（含）")
    log_list.add_argument("--model", default=None, help="按请求模型过滤")
    log_list.add_argument("--channel", type=int, default=None, help="按渠道 id 过滤")
    log_list.add_argument("--limit", type=int, default=200, help="最多返回条数")

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # --json 允许写在任意位置（argparse 的全局参数在子命令后会失效）。
    force_json = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    parser = build_parser()
    args = parser.parse_args(raw)
    args.json = force_json or bool(getattr(args, "json", False))
    try:
        if args.cmd == "channel":
            return cmd_channel(args)
        if args.cmd == "option":
            return cmd_option(args)
        if args.cmd == "log":
            return cmd_log(args)
        raise AdminError(f"未知命令：{args.cmd}")
    except AdminError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
