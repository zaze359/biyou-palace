"""因果簿 · 任务板（SQLite 起步）。

任务板只存放「谁在做什么、谁依赖谁、租约何时到期」的紧凑状态；
完整历史、回禀原文与大产物不进入任务板：
- 结果只追加（`board_results`），不更新、不删除；
- 大内容必须落藏经阁（`ArtifactStore`），任务板只保留 `artifact_ref`；
- 紧凑视图不返回历史列表，供注入模型上下文。

CAS 以 `revision` 为准：认领与更新都要求 `expected_revision` 与当前值一致。
`blocked_by` 未完成时不可认领；`write_scopes` 只产生冲突提示，不构成锁。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

TASK_STATUSES = ("pending", "in_progress", "completed", "deleted")
RISK_LEVELS = ("qingxiu", "rushi", "pozhen", "chuandao")
MAX_RESULT_CONTENT = 8_192

_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_tasks (
    ticket_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    tier INTEGER NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    owner TEXT,
    blocked_by_json TEXT NOT NULL DEFAULT '[]',
    write_scopes_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1,
    lease_expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_board_tasks_status ON board_tasks(status, updated_at_ms);

CREATE TABLE IF NOT EXISTS board_results (
    result_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    agent_id TEXT,
    kind TEXT NOT NULL,
    content TEXT,
    artifact_ref TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(ticket_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_board_results_ticket_seq ON board_results(ticket_id, seq);
"""


class BoardError(Exception):
    """任务板领域错误；由 HTTP 层转换为统一错误体。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _rfc3339(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat()


def _scope_overlap(left: str, right: str) -> bool:
    a = left.rstrip("/")
    b = right.rstrip("/")
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class TaskBoardStore:
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

    # ---------------------------------------------------------------- 写入

    def create_task(
        self,
        *,
        ticket_id: str,
        title: str,
        goal: str,
        tier: int,
        risk: str,
        blocked_by: Iterable[str] = (),
        write_scopes: Iterable[str] = (),
        now_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        """按 `ticket_id` 幂等创建；重复创建返回已有任务。"""
        if not 1 <= tier <= 8:
            raise BoardError(422, "invalid_tier", "tier 必须在 1..8")
        if risk not in RISK_LEVELS:
            raise BoardError(422, "invalid_risk", f"risk 只能是 {list(RISK_LEVELS)}")
        deps = [item for item in blocked_by if item]
        if ticket_id in deps:
            raise BoardError(422, "invalid_blocked_by", "blocked_by 不能包含自身")
        scopes = [item for item in write_scopes if item]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO board_tasks (
                    ticket_id, title, goal, tier, risk, status, owner,
                    blocked_by_json, write_scopes_json, revision,
                    created_at_ms, updated_at_ms
                ) VALUES (?,?,?,?,?,'pending',NULL,?,?,1,?,?)
                ON CONFLICT(ticket_id) DO NOTHING
                """,
                (
                    ticket_id,
                    title,
                    goal,
                    tier,
                    risk,
                    json.dumps(sorted(set(deps)), ensure_ascii=False),
                    json.dumps(sorted(set(scopes)), ensure_ascii=False),
                    now_ms,
                    now_ms,
                ),
            )
            created = cur.rowcount == 1
        task = self.get_task(ticket_id)
        assert task is not None
        return task, created

    def claim(
        self,
        *,
        ticket_id: str,
        owner: str,
        expected_revision: int,
        lease_ttl_s: int,
        now_ms: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """CAS 认领；返回 (task, write_scope_hints)。

        - 已完成/已删除：409 `ticket_state_invalid`
        - 被阻塞：409 `task_blocked`
        - revision 不一致：409 `revision_conflict`
        - 他人持有未过期租约：409 `task_owned`
        - 租约已过期：允许接管，revision 继续递增
        """
        if lease_ttl_s < 1:
            raise BoardError(422, "invalid_lease", "lease_ttl_s 必须大于 0")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM board_tasks WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if row is None:
                    raise BoardError(404, "ticket_unknown", f"任务 {ticket_id} 不存在")
                if row["status"] in ("completed", "deleted"):
                    raise BoardError(
                        409, "ticket_state_invalid", f"任务状态 {row['status']} 不可认领"
                    )
                if int(row["revision"]) != expected_revision:
                    raise BoardError(
                        409,
                        "revision_conflict",
                        f"revision 不匹配：期望 {expected_revision}，当前 {row['revision']}",
                    )
                if self._is_blocked_conn(row):
                    raise BoardError(409, "task_blocked", "前置依赖未完成，暂不可认领")
                lease_alive = (
                    row["status"] == "in_progress"
                    and row["lease_expires_at_ms"] is not None
                    and int(row["lease_expires_at_ms"]) > now_ms
                )
                if lease_alive and row["owner"] and row["owner"] != owner:
                    raise BoardError(
                        409, "task_owned", f"任务由 {row['owner']} 持有且租约未过期"
                    )
                hints = self._scope_hints_conn(ticket_id, row["write_scopes_json"])
                expires = now_ms + lease_ttl_s * 1000
                self._conn.execute(
                    """
                    UPDATE board_tasks
                    SET status='in_progress', owner=?, lease_expires_at_ms=?,
                        revision=revision+1, updated_at_ms=?
                    WHERE ticket_id=?
                    """,
                    (owner, expires, now_ms, ticket_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(ticket_id)
        assert task is not None
        return task, hints

    def update(
        self,
        *,
        ticket_id: str,
        expected_revision: int,
        status: str | None,
        owner: str | None,
        lease_ttl_s: int | None,
        now_ms: int,
    ) -> dict[str, Any]:
        if status is not None and status not in TASK_STATUSES:
            raise BoardError(422, "invalid_status", f"status 只能是 {list(TASK_STATUSES)}")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM board_tasks WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if row is None:
                    raise BoardError(404, "ticket_unknown", f"任务 {ticket_id} 不存在")
                if int(row["revision"]) != expected_revision:
                    raise BoardError(
                        409,
                        "revision_conflict",
                        f"revision 不匹配：期望 {expected_revision}，当前 {row['revision']}",
                    )
                sets = ["revision=revision+1", "updated_at_ms=?"]
                values: list[Any] = [now_ms]
                if status is not None:
                    sets.append("status=?")
                    values.append(status)
                    if status == "completed":
                        sets.append("completed_at_ms=?")
                        values.append(now_ms)
                        sets.append("lease_expires_at_ms=NULL")
                    elif status == "pending":
                        sets.append("owner=NULL")
                        sets.append("lease_expires_at_ms=NULL")
                    elif status == "deleted":
                        sets.append("lease_expires_at_ms=NULL")
                if owner is not None:
                    sets.append("owner=?")
                    values.append(owner or None)
                if lease_ttl_s is not None:
                    if lease_ttl_s < 1:
                        raise BoardError(422, "invalid_lease", "lease_ttl_s 必须大于 0")
                    sets.append("lease_expires_at_ms=?")
                    values.append(now_ms + lease_ttl_s * 1000)
                self._conn.execute(
                    f"UPDATE board_tasks SET {', '.join(sets)} WHERE ticket_id=?",
                    (*values, ticket_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(ticket_id)
        assert task is not None
        return task

    def append_result(
        self,
        *,
        ticket_id: str,
        result_id: str,
        agent_id: str | None,
        kind: str,
        content: str | None,
        artifact_ref: str | None,
        now_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        """追加结果；`result_id` 幂等。返回 (result, created)。"""
        if not content and not artifact_ref:
            raise BoardError(422, "invalid_result", "content 与 artifact_ref 至少提供一个")
        if content and len(content) > MAX_RESULT_CONTENT:
            raise BoardError(
                422,
                "result_too_large",
                f"内联结果不得超过 {MAX_RESULT_CONTENT} 字符，请改用藏经阁 artifact_ref",
            )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                task = self._conn.execute(
                    "SELECT ticket_id FROM board_tasks WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if task is None:
                    raise BoardError(404, "ticket_unknown", f"任务 {ticket_id} 不存在")
                existing = self._conn.execute(
                    "SELECT * FROM board_results WHERE result_id=?", (result_id,)
                ).fetchone()
                if existing is not None:
                    self._conn.execute("COMMIT")
                    return self._result_row(existing), False
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM board_results WHERE ticket_id=?",
                    (ticket_id,),
                ).fetchone()
                seq = int(row["seq"]) + 1
                self._conn.execute(
                    """
                    INSERT INTO board_results (
                        result_id, ticket_id, seq, agent_id, kind, content,
                        artifact_ref, created_at_ms
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        result_id,
                        ticket_id,
                        seq,
                        agent_id,
                        kind,
                        content,
                        artifact_ref,
                        now_ms,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        result = self.get_result(ticket_id, result_id)
        assert result is not None
        return result, True

    def reap_expired(self, now_ms: int) -> list[str]:
        """回收过期租约：回到 pending、清空 owner，revision 递增。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ticket_id FROM board_tasks
                WHERE status='in_progress' AND lease_expires_at_ms IS NOT NULL
                  AND lease_expires_at_ms<=?
                """,
                (now_ms,),
            ).fetchall()
            ids = [str(row["ticket_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""
                    UPDATE board_tasks
                    SET status='pending', owner=NULL, lease_expires_at_ms=NULL,
                        revision=revision+1, updated_at_ms=?
                    WHERE ticket_id IN ({placeholders})
                    """,
                    (now_ms, *ids),
                )
        return ids

    # ---------------------------------------------------------------- 读取

    def get_task(self, ticket_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM board_tasks WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(
        self,
        *,
        status: str | None = None,
        owner: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if owner:
            clauses.append("owner=?")
            values.append(owner)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        capped = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM board_tasks {where} "
                "ORDER BY updated_at_ms DESC, ticket_id ASC LIMIT ?",
                (*values, capped),
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def get_result(self, ticket_id: str, result_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM board_results WHERE ticket_id=? AND result_id=?",
                (ticket_id, result_id),
            ).fetchone()
        return self._result_row(row) if row else None

    def list_results(
        self, ticket_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 1_000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM board_results WHERE ticket_id=? AND seq>?
                ORDER BY seq ASC LIMIT ?
                """,
                (ticket_id, after_seq, capped),
            ).fetchall()
        return [self._result_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM board_tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # ---------------------------------------------------------------- 视图

    def compact_view(self, task: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        """紧凑视图：不含历史、不含大产物；供列表与模型注入。"""
        blocked = self._is_blocked(task)
        expires = task.get("lease_expires_at_ms")
        return {
            "ticket_id": task["ticket_id"],
            "title": task["title"],
            "goal": task["goal"],
            "tier": task["tier"],
            "risk": task["risk"],
            "status": task["status"],
            "owner": task["owner"],
            "blocked": blocked,
            "blocked_by": list(task["blocked_by"]),
            "write_scopes": list(task["write_scopes"]),
            "revision": int(task["revision"]),
            "lease": {
                "expires_at": _rfc3339(expires),
                "expired": bool(expires is not None and int(expires) <= now_ms),
            },
            "updated_at": _rfc3339(int(task["updated_at_ms"])),
            "created_at": _rfc3339(int(task["created_at_ms"])),
        }

    def _is_blocked(self, task: dict[str, Any]) -> bool:
        deps = list(task.get("blocked_by") or [])
        if not deps:
            return False
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ticket_id, status FROM board_tasks "
                f"WHERE ticket_id IN ({','.join('?' for _ in deps)})",
                tuple(deps),
            ).fetchall()
        statuses = {str(row["ticket_id"]): str(row["status"]) for row in rows}
        return any(statuses.get(dep) != "completed" for dep in deps)

    def _is_blocked_conn(self, row: sqlite3.Row) -> bool:
        deps = json.loads(row["blocked_by_json"] or "[]")
        if not deps:
            return False
        rows = self._conn.execute(
            f"SELECT ticket_id, status FROM board_tasks "
            f"WHERE ticket_id IN ({','.join('?' for _ in deps)})",
            tuple(deps),
        ).fetchall()
        statuses = {str(item["ticket_id"]): str(item["status"]) for item in rows}
        return any(statuses.get(dep) != "completed" for dep in deps)

    def _scope_hints_conn(
        self, ticket_id: str, scopes_json: str
    ) -> list[dict[str, Any]]:
        scopes = json.loads(scopes_json or "[]")
        if not scopes:
            return []
        rows = self._conn.execute(
            "SELECT ticket_id, write_scopes_json FROM board_tasks "
            "WHERE status='in_progress' AND ticket_id<>?",
            (ticket_id,),
        ).fetchall()
        hints: list[dict[str, Any]] = []
        for row in rows:
            other = json.loads(row["write_scopes_json"] or "[]")
            overlaps = sorted(
                {
                    mine
                    for mine in scopes
                    for candidate in other
                    if _scope_overlap(mine, candidate)
                }
            )
            if overlaps:
                hints.append({"ticket_id": row["ticket_id"], "scopes": overlaps})
        hints.sort(key=lambda item: item["ticket_id"])
        return hints

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["blocked_by"] = json.loads(data.pop("blocked_by_json") or "[]")
        data["write_scopes"] = json.loads(data.pop("write_scopes_json") or "[]")
        return data

    @staticmethod
    def _result_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        return data
