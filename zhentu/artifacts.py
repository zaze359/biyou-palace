"""藏经阁 · 产物存储与索引。

大产物按内容哈希落盘（`artifact://<sha256>`），任务板与账本只保存引用；
同一内容重复上传不重复写盘，索引行按 `sha256` 幂等。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    ticket_id TEXT,
    step_id TEXT,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_ticket ON artifacts(ticket_id, created_at_ms);
"""

DEFAULT_MEDIA_TYPE = "application/octet-stream"


class ArtifactStore:
    def __init__(self, db_path: str, artifact_dir: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._dir = Path(artifact_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
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

    def put(
        self,
        content: bytes,
        *,
        media_type: str | None = None,
        ticket_id: str | None = None,
        step_id: str | None = None,
        now_ms: int,
    ) -> tuple[dict, bool]:
        """写入产物；返回 (索引行, created)。同一内容幂等。"""
        digest = hashlib.sha256(content).hexdigest()
        relative = f"{digest[:2]}/{digest}"
        target = self._dir / relative
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (digest,)
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            self._conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, sha256, size_bytes, media_type, relative_path,
                    ticket_id, step_id, created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (
                    digest,
                    digest,
                    len(content),
                    media_type or DEFAULT_MEDIA_TYPE,
                    relative,
                    ticket_id,
                    step_id,
                    now_ms,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (digest,)
            ).fetchone()
        return dict(row), True

    def get(self, artifact_id: str) -> tuple[bytes, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        path = self._dir / row["relative_path"]
        if not path.is_file():
            return None
        return path.read_bytes(), str(row["media_type"])

    def stat(self, artifact_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, *, ticket_id: str | None = None, limit: int = 100) -> list[dict]:
        capped = max(1, min(limit, 500))
        with self._lock:
            if ticket_id:
                rows = self._conn.execute(
                    "SELECT * FROM artifacts WHERE ticket_id=? "
                    "ORDER BY created_at_ms DESC, artifact_id ASC LIMIT ?",
                    (ticket_id, capped),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM artifacts "
                    "ORDER BY created_at_ms DESC, artifact_id ASC LIMIT ?",
                    (capped,),
                ).fetchall()
        return [dict(row) for row in rows]
