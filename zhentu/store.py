"""SQLite 持久化：节点快照、心跳租约与查询。

节点规模是十几台，读取时在 Python 侧过滤/排序即可；不做后台清扫，
`online/offline` 由读取时的 `last_seen_ms + ttl_s` 计算。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    models_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    models_rev TEXT NOT NULL,
    loaded_json TEXT NOT NULL DEFAULT '[]',
    resources_json TEXT NOT NULL DEFAULT '{}',
    limits_json TEXT NOT NULL DEFAULT '{}',
    heartbeat_interval_s INTEGER NOT NULL,
    ttl_s INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    registered_at_ms INTEGER NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    deregistered_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen_ms);
"""


class RegistryStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=3000")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- 写入

    def register(
        self,
        *,
        node: dict[str, Any],
        endpoint: str,
        status: str,
        models: list[dict[str, Any]],
        limits: dict[str, Any],
        resources: dict[str, Any],
        heartbeat_interval_s: int,
        ttl_s: int,
        boot_id: str,
        models_rev: str,
        capabilities: list[str],
        sent_at: str | None,
        now_ms: int,
    ) -> dict[str, Any]:
        loaded = [m["name"] for m in models if m.get("loaded")]
        payload = {
            "node": node,
            "endpoint": endpoint,
            "status": status,
            "models": models,
            "limits": limits,
            "resources": resources,
            "heartbeat_interval_s": heartbeat_interval_s,
            "boot_id": boot_id,
            "sent_at": sent_at,
        }
        sql = """
        INSERT INTO nodes (
            node_id, role, name, endpoint, boot_id, status, contract_version,
            payload_json, models_json, capabilities_json, models_rev,
            loaded_json, resources_json, limits_json,
            heartbeat_interval_s, ttl_s, revision,
            registered_at_ms, first_seen_ms, last_seen_ms, deregistered_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,NULL)
        ON CONFLICT(node_id) DO UPDATE SET
            role=excluded.role,
            name=excluded.name,
            endpoint=excluded.endpoint,
            boot_id=excluded.boot_id,
            status=excluded.status,
            contract_version=excluded.contract_version,
            payload_json=excluded.payload_json,
            models_json=excluded.models_json,
            capabilities_json=excluded.capabilities_json,
            models_rev=excluded.models_rev,
            loaded_json=excluded.loaded_json,
            resources_json=excluded.resources_json,
            limits_json=excluded.limits_json,
            heartbeat_interval_s=excluded.heartbeat_interval_s,
            ttl_s=excluded.ttl_s,
            revision=nodes.revision + 1,
            registered_at_ms=excluded.registered_at_ms,
            last_seen_ms=excluded.last_seen_ms,
            deregistered_at_ms=NULL
        """
        with self._lock:
            self._conn.execute(
                sql,
                (
                    node["id"],
                    node["role"],
                    node.get("name") or "",
                    endpoint,
                    boot_id,
                    status,
                    node.get("contract_version") or "1.0",
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(models, ensure_ascii=False),
                    json.dumps(capabilities, ensure_ascii=False),
                    models_rev,
                    json.dumps(loaded, ensure_ascii=False),
                    json.dumps(resources, ensure_ascii=False),
                    json.dumps(limits, ensure_ascii=False),
                    heartbeat_interval_s,
                    ttl_s,
                    now_ms,
                    now_ms,
                    now_ms,
                ),
            )
        return self.get(node["id"]) or {}

    def heartbeat(
        self,
        *,
        node_id: str,
        boot_id: str,
        status: str,
        models_rev: str,
        loaded_models: list[str],
        resources: dict[str, Any],
        limits: dict[str, Any],
        now_ms: int,
    ) -> tuple[str, dict[str, Any] | None, bool]:
        """返回 (结果, 行, 是否需要补注册)。

        结果：ok | unknown | boot_mismatch
        """
        with self._lock:
            row = self.get(node_id)
            if row is None or row.get("deregistered_at_ms"):
                return "unknown", None, False
            if row["boot_id"] != boot_id:
                return "boot_mismatch", row, False
            need_snapshot = bool(models_rev) and models_rev != row["models_rev"]
            self._conn.execute(
                """
                UPDATE nodes SET last_seen_ms=?, status=?, loaded_json=?,
                    resources_json=?, limits_json=?
                WHERE node_id=?
                """,
                (
                    now_ms,
                    status,
                    json.dumps(loaded_models, ensure_ascii=False),
                    json.dumps(resources, ensure_ascii=False),
                    json.dumps(limits, ensure_ascii=False),
                    node_id,
                ),
            )
            return "ok", self.get(node_id), need_snapshot

    def deregister(self, node_id: str, boot_id: str, now_ms: int) -> bool:
        with self._lock:
            row = self.get(node_id)
            if row is None or row.get("deregistered_at_ms"):
                return False
            if row["boot_id"] != boot_id:
                return False
            self._conn.execute(
                "UPDATE nodes SET deregistered_at_ms=? WHERE node_id=?",
                (now_ms, node_id),
            )
            return True

    # ---------------------------------------------------------------- 读取

    def get(self, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        return [self._row(row) for row in rows]

    def counts(self, now_ms: int) -> tuple[int, int]:
        """返回 (total, online)：total = 未注销的节点数（含租约过期但未注销），
        online = 其中租约仍有效的数量。

        **已注销的行不计入 total**：节点优雅下线后若仍被统计，灵脉看板上的
        「节点总数」会只增不减，与「在册节点」语义不符；历史行保留在库中供审计。
        """
        rows = self.list_all()
        active = [row for row in rows if row.get("deregistered_at_ms") is None]
        online = sum(
            1
            for row in active
            if now_ms - row["last_seen_ms"] <= row["ttl_s"] * 1000
        )
        return len(active), online

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in (
            "payload_json",
            "models_json",
            "capabilities_json",
            "loaded_json",
            "resources_json",
            "limits_json",
        ):
            data[key] = json.loads(data[key]) if data.get(key) else (
                {} if key in ("payload_json", "resources_json", "limits_json") else []
            )
        return data
