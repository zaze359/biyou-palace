"""阵图任务账本：tasks / task_events / task_steps / route_decisions。

与 `RegistryStore` 共用同一个 SQLite 文件，各自持有连接；WAL + busy_timeout
保证单进程内并发读写安全。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = ("queued", "running", "aggregating")
TERMINAL_STATUSES = ("completed", "failed", "canceled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    origin_node_id TEXT NOT NULL,
    session_id TEXT,
    mode TEXT NOT NULL,
    requested_mode TEXT,
    privacy TEXT NOT NULL,
    status TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    terminal_reason TEXT,
    request_json TEXT NOT NULL,
    plan_json TEXT,
    planner_model TEXT,
    result_json TEXT,
    error_json TEXT,
    lease_owner TEXT,
    lease_expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    started_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    deadline_at_ms INTEGER NOT NULL,
    UNIQUE(origin_node_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at_ms);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS task_steps (
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    capability TEXT NOT NULL,
    input_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    parallel_group TEXT,
    exclusive INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 1,
    failure_policy TEXT NOT NULL DEFAULT 'fail_task',
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    output_ref TEXT,
    error_code TEXT,
    started_at_ms INTEGER,
    finished_at_ms INTEGER,
    UNIQUE(task_id, step_id)
);

CREATE TABLE IF NOT EXISTS route_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    selected_json TEXT,
    rejected_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_task_step ON route_decisions(task_id, step_id);
"""


class TaskStore:
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

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---------------------------------------------------------------- 任务

    def create_task(
        self,
        *,
        request_id: str,
        origin_node_id: str,
        session_id: str | None,
        mode: str,
        requested_mode: str | None,
        privacy: str,
        request: dict[str, Any],
        deadline_at_ms: int,
        now_ms: int,
        task_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """返回 (task, created)。重复 (origin, request_id) 时返回已有任务。"""
        new_id = task_id or "task-" + uuid.uuid4().hex
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, request_id, origin_node_id, session_id, mode,
                    requested_mode, privacy, status, request_json,
                    created_at_ms, updated_at_ms, deadline_at_ms
                ) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)
                ON CONFLICT(origin_node_id, request_id) DO NOTHING
                """,
                (
                    new_id,
                    request_id,
                    origin_node_id,
                    session_id,
                    mode,
                    requested_mode,
                    privacy,
                    json.dumps(request, ensure_ascii=False),
                    now_ms,
                    now_ms,
                    deadline_at_ms,
                ),
            )
            created = cur.rowcount == 1
        row = self.get_task(new_id if created else "") or self.get_by_request(
            origin_node_id, request_id
        )
        assert row is not None
        return row, created

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if not task_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task_row(row) if row else None

    def get_by_request(
        self, origin_node_id: str, request_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE origin_node_id=? AND request_id=?",
                (origin_node_id, request_id),
            ).fetchone()
        return self._task_row(row) if row else None

    def update_task(self, task_id: str, *, now_ms: int, **fields: Any) -> None:
        allowed = {
            "status",
            "cancel_requested",
            "terminal_reason",
            "plan_json",
            "planner_model",
            "result_json",
            "error_json",
            "lease_owner",
            "lease_expires_at_ms",
            "started_at_ms",
            "finished_at_ms",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = [
            json.dumps(value, ensure_ascii=False)
            if key in ("plan_json", "result_json", "error_json")
            else value
            for key, value in updates.items()
        ]
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at_ms=? WHERE task_id=?",
                (*values, now_ms, task_id),
            )

    def count_active_tasks(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            ).fetchone()
        return int(row["n"])

    def request_cancel(self, task_id: str, *, now_ms: int) -> bool:
        """置取消位；返回是否更新了未终态任务。"""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            cur = self._conn.execute(
                f"""
                UPDATE tasks SET cancel_requested=1, updated_at_ms=?
                WHERE task_id=? AND status IN ({placeholders})
                """,
                (now_ms, task_id, *ACTIVE_STATUSES),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------------- 事件

    def append_event(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        now_ms: int,
        max_events: int | None = None,
        retention_ms: int | None = None,
    ) -> dict[str, Any]:
        event_id = "evt-" + uuid.uuid4().hex
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM task_events WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                seq = int(row["seq"]) + 1
                self._conn.execute(
                    """
                    INSERT INTO task_events (event_id, task_id, seq, type, at_ms, data_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        event_id,
                        task_id,
                        seq,
                        event_type,
                        now_ms,
                        json.dumps(data, ensure_ascii=False),
                    ),
                )
                if max_events and seq > max_events:
                    self._conn.execute(
                        "DELETE FROM task_events WHERE task_id=? AND seq<=?",
                        (task_id, seq - max_events),
                    )
                if retention_ms:
                    self._conn.execute(
                        "DELETE FROM task_events WHERE task_id=? AND at_ms<?",
                        (task_id, now_ms - retention_ms),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {
            "event_id": event_id,
            "task_id": task_id,
            "seq": seq,
            "type": event_type,
            "at_ms": now_ms,
            "data": data,
        }

    def list_events(self, task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM task_events WHERE task_id=? AND seq>?
                ORDER BY seq ASC
                """,
                (task_id, after_seq),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def get_event(self, task_id: str, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND event_id=?",
                (task_id, event_id),
            ).fetchone()
        return self._event_row(row) if row else None

    def last_event_seq(self, task_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM task_events WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return int(row["seq"])

    # ---------------------------------------------------------------- 步骤

    def upsert_step(
        self,
        task_id: str,
        step: dict[str, Any],
        *,
        status: str,
        now_ms: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO task_steps (
                    task_id, step_id, goal, capability, input_json,
                    depends_on_json, parallel_group, exclusive, required,
                    failure_policy, status, attempt
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(task_id, step_id) DO UPDATE SET
                    goal=excluded.goal,
                    capability=excluded.capability,
                    input_json=excluded.input_json,
                    depends_on_json=excluded.depends_on_json,
                    parallel_group=excluded.parallel_group,
                    exclusive=excluded.exclusive,
                    required=excluded.required,
                    failure_policy=excluded.failure_policy
                """,
                (
                    task_id,
                    step["id"],
                    step.get("goal") or "",
                    step.get("capability") or "chat",
                    json.dumps(step.get("input") or {}, ensure_ascii=False),
                    json.dumps(step.get("depends_on") or [], ensure_ascii=False),
                    step.get("parallel_group"),
                    1 if step.get("exclusive") else 0,
                    1 if step.get("required", True) else 0,
                    step.get("failure_policy") or "fail_task",
                    status,
                ),
            )

    def update_step(
        self,
        task_id: str,
        step_id: str,
        *,
        status: str,
        now_ms: int,
        attempt: int | None = None,
        output_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        sets = ["status=?", "finished_at_ms=?"]
        values: list[Any] = [status, now_ms]
        if status == "running":
            sets = ["status=?", "started_at_ms=?", "attempt=attempt+1"]
            values = [status, now_ms]
        if attempt is not None:
            sets.append("attempt=?")
            values.append(attempt)
        if output_ref is not None:
            sets.append("output_ref=?")
            values.append(output_ref)
        if error_code is not None:
            sets.append("error_code=?")
            values.append(error_code)
        with self._lock:
            self._conn.execute(
                f"UPDATE task_steps SET {', '.join(sets)} WHERE task_id=? AND step_id=?",
                (*values, task_id, step_id),
            )

    # ---------------------------------------------------------------- 选路

    def record_route_decision(
        self,
        *,
        task_id: str,
        step_id: str,
        request: dict[str, Any],
        selected: dict[str, Any] | None,
        rejected: list[Any],
        now_ms: int,
    ) -> str:
        decision_id = "dec-" + uuid.uuid4().hex[:16]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO route_decisions (
                    decision_id, task_id, step_id, request_json,
                    selected_json, rejected_json, created_at_ms
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    decision_id,
                    task_id,
                    step_id,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(selected, ensure_ascii=False) if selected else None,
                    json.dumps(rejected, ensure_ascii=False),
                    now_ms,
                ),
            )
        return decision_id

    # ---------------------------------------------------------------- 转换

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("request_json", "plan_json", "result_json", "error_json"):
            raw = data.get(key)
            data[key] = json.loads(raw) if raw else None
        return data

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["data"] = json.loads(data.pop("data_json") or "{}")
        return data
