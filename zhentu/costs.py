"""香火账本 · 模型调用成本归因与预算。

规则（与父 PRD R12 一致）：
- 缓存命中/未命中分开计价；拿不到缓存字段必须 `cache_unknown=true`，按未命中价估算；
- 每条用量必须归因到 `ticket_id`、`step_id`、`agent_id`、`model`、`provider` 与 `price_version`；
- 预算 80% 进入 `warning`，100% 进入 `paused` 并写入告警（供观星台/功德殿消费）；
- 记录以 `entry_id` 幂等，重复回禀不会重复记账。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

BUDGET_WARNING_RATIO = 0.8
BUDGET_PAUSED_RATIO = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_prices (
    price_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    hit_price REAL NOT NULL,
    miss_price REAL NOT NULL,
    output_price REAL NOT NULL,
    currency TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    PRIMARY KEY (price_version, provider, model)
);

CREATE TABLE IF NOT EXISTS cost_entries (
    entry_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    step_id TEXT,
    agent_id TEXT,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    cache_unknown INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL,
    currency TEXT NOT NULL,
    price_version TEXT NOT NULL,
    cache_savings REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_entries_ticket ON cost_entries(ticket_id, created_at_ms);
CREATE INDEX IF NOT EXISTS idx_cost_entries_agent ON cost_entries(ticket_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_cost_entries_step ON cost_entries(ticket_id, step_id);

CREATE TABLE IF NOT EXISTS ticket_budgets (
    ticket_id TEXT PRIMARY KEY,
    max_workers INTEGER NOT NULL,
    max_input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_cost REAL NOT NULL,
    max_wall_time_s INTEGER NOT NULL,
    currency TEXT,
    started_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_alerts (
    alert_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    level TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(ticket_id, level)
);
"""


class CostError(Exception):
    """香火账本领域错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class CostLedger:
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

    # ---------------------------------------------------------------- 价格

    def put_prices(
        self, *, price_version: str, prices: list[dict[str, Any]], now_ms: int
    ) -> int:
        if not price_version:
            raise CostError(422, "invalid_price_version", "price_version 不能为空")
        with self._lock:
            for price in prices:
                provider = str(price.get("provider") or "")
                model = str(price.get("model") or "")
                if not provider or not model:
                    raise CostError(422, "invalid_price", "provider 与 model 不能为空")
                for key in ("hit_price", "miss_price", "output_price"):
                    value = price.get(key)
                    if value is None or float(value) < 0:
                        raise CostError(422, "invalid_price", f"{key} 必须是非负数")
                self._conn.execute(
                    """
                    INSERT INTO cost_prices (
                        price_version, provider, model, hit_price, miss_price,
                        output_price, currency, effective_at_ms
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(price_version, provider, model) DO UPDATE SET
                        hit_price=excluded.hit_price,
                        miss_price=excluded.miss_price,
                        output_price=excluded.output_price,
                        currency=excluded.currency,
                        effective_at_ms=excluded.effective_at_ms
                    """,
                    (
                        price_version,
                        provider,
                        model,
                        float(price["hit_price"]),
                        float(price["miss_price"]),
                        float(price["output_price"]),
                        str(price.get("currency") or "CNY"),
                        now_ms,
                    ),
                )
        return len(prices)

    def list_prices(self, price_version: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if price_version:
                rows = self._conn.execute(
                    "SELECT * FROM cost_prices WHERE price_version=? "
                    "ORDER BY provider, model",
                    (price_version,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM cost_prices ORDER BY price_version, provider, model"
                ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 预算

    def set_budget(
        self, *, ticket_id: str, budget: dict[str, Any], now_ms: int
    ) -> dict[str, Any]:
        for key in (
            "max_workers",
            "max_input_tokens",
            "max_output_tokens",
            "max_cost",
            "max_wall_time_s",
        ):
            value = budget.get(key)
            if value is None or float(value) < 0:
                raise CostError(422, "invalid_budget", f"{key} 必须是非负数")
        if int(budget["max_workers"]) < 1:
            raise CostError(422, "invalid_budget", "max_workers 必须大于 0")
        if int(budget["max_wall_time_s"]) < 1:
            raise CostError(422, "invalid_budget", "max_wall_time_s 必须大于 0")
        with self._lock:
            existing = self._conn.execute(
                "SELECT started_at_ms FROM ticket_budgets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            started = int(existing["started_at_ms"]) if existing else now_ms
            self._conn.execute(
                """
                INSERT INTO ticket_budgets (
                    ticket_id, max_workers, max_input_tokens, max_output_tokens,
                    max_cost, max_wall_time_s, currency, started_at_ms, updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    max_workers=excluded.max_workers,
                    max_input_tokens=excluded.max_input_tokens,
                    max_output_tokens=excluded.max_output_tokens,
                    max_cost=excluded.max_cost,
                    max_wall_time_s=excluded.max_wall_time_s,
                    currency=excluded.currency,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    ticket_id,
                    int(budget["max_workers"]),
                    int(budget["max_input_tokens"]),
                    int(budget["max_output_tokens"]),
                    float(budget["max_cost"]),
                    int(budget["max_wall_time_s"]),
                    budget.get("currency"),
                    started,
                    now_ms,
                ),
            )
        status = self.budget_status(ticket_id, now_ms=now_ms)
        assert status is not None
        return status

    def get_budget(self, ticket_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ticket_budgets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 记账

    def record_usage(
        self, *, entry: dict[str, Any], now_ms: int
    ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        """返回 (entry, created, budget_status)。`entry_id` 幂等。"""
        entry_id = str(entry.get("entry_id") or "")
        if not entry_id:
            raise CostError(422, "invalid_entry", "entry_id 不能为空")
        ticket_id = str(entry.get("ticket_id") or "")
        if not ticket_id:
            raise CostError(422, "invalid_entry", "ticket_id 不能为空")
        provider = str(entry.get("provider") or "")
        model = str(entry.get("model") or "")
        price_version = str(entry.get("price_version") or "")
        input_tokens = int(entry.get("input_tokens") or 0)
        output_tokens = int(entry.get("output_tokens") or 0)
        if input_tokens < 0 or output_tokens < 0:
            raise CostError(422, "invalid_usage", "tokens 不能为负")
        cache_unknown = bool(entry.get("cache_unknown"))

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM cost_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if existing is not None:
                status = self._budget_status_conn(ticket_id, now_ms)
                return self._entry_row(existing), False, status

            price = self._conn.execute(
                """
                SELECT * FROM cost_prices
                WHERE price_version=? AND provider=? AND model=?
                """,
                (price_version, provider, model),
            ).fetchone()
            if price is None:
                raise CostError(
                    422,
                    "price_unknown",
                    f"未找到价格：{price_version}/{provider}/{model}",
                )
            budget = self._conn.execute(
                "SELECT * FROM ticket_budgets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if (
                budget is not None
                and budget["currency"]
                and budget["currency"] != price["currency"]
            ):
                raise CostError(
                    422,
                    "currency_mismatch",
                    f"预算币种 {budget['currency']} 与价格币种 {price['currency']} 不一致",
                )

            if cache_unknown:
                hit = 0
                miss = input_tokens
                savings = 0.0
            else:
                hit_value = entry.get("cache_hit_tokens")
                miss_value = entry.get("cache_miss_tokens")
                if hit_value is None or miss_value is None:
                    raise CostError(
                        422,
                        "cache_fields_required",
                        "非 cache_unknown 时必须提供 cache_hit_tokens 与 cache_miss_tokens",
                    )
                hit = int(hit_value)
                miss = int(miss_value)
                if hit < 0 or miss < 0 or hit + miss != input_tokens:
                    raise CostError(
                        422,
                        "invalid_usage",
                        "cache_hit_tokens + cache_miss_tokens 必须等于 input_tokens",
                    )
                savings = round(hit * (price["miss_price"] - price["hit_price"]), 8)

            cost = round(
                hit * price["hit_price"]
                + miss * price["miss_price"]
                + output_tokens * price["output_price"],
                8,
            )
            self._conn.execute(
                """
                INSERT INTO cost_entries (
                    entry_id, ticket_id, step_id, agent_id, model, provider,
                    input_tokens, output_tokens, cache_hit_tokens,
                    cache_miss_tokens, cache_unknown, cost, currency,
                    price_version, cache_savings, latency_ms, created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry_id,
                    ticket_id,
                    entry.get("step_id"),
                    entry.get("agent_id"),
                    model,
                    provider,
                    input_tokens,
                    output_tokens,
                    hit,
                    miss,
                    1 if cache_unknown else 0,
                    cost,
                    price["currency"],
                    price_version,
                    savings,
                    entry.get("latency_ms"),
                    now_ms,
                ),
            )
            status = self._budget_status_conn(ticket_id, now_ms)
            if status and status["state"] in ("warning", "paused"):
                self._raise_alert_conn(ticket_id, status, now_ms)
                status = self._budget_status_conn(ticket_id, now_ms)
            row = self._conn.execute(
                "SELECT * FROM cost_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return self._entry_row(row), True, status

    # ---------------------------------------------------------------- 查询

    def list_entries(
        self,
        *,
        ticket_id: str | None = None,
        agent_id: str | None = None,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("ticket_id", ticket_id),
            ("agent_id", agent_id),
            ("step_id", step_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        capped = max(1, min(limit, 1_000))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM cost_entries {where} "
                "ORDER BY created_at_ms ASC, entry_id ASC LIMIT ?",
                (*values, capped),
            ).fetchall()
        return [self._entry_row(row) for row in rows]

    def budget_status(
        self, ticket_id: str, *, now_ms: int
    ) -> dict[str, Any] | None:
        with self._lock:
            return self._budget_status_conn(ticket_id, now_ms)

    def admission(
        self, *, ticket_id: str, estimated_cost: float, now_ms: int
    ) -> dict[str, Any]:
        """派差/开工前的准入判断；`paused` 时拒绝新香火。"""
        status = self.budget_status(ticket_id, now_ms=now_ms)
        if status is None:
            return {"admitted": True, "reason": None, "budget": None}
        if status["state"] == "paused":
            return {"admitted": False, "reason": "budget_exhausted", "budget": status}
        if estimated_cost > 0:
            projected = status["used"]["cost"] + estimated_cost
            if projected > status["limits"]["max_cost"]:
                return {
                    "admitted": False,
                    "reason": "insufficient_budget",
                    "budget": status,
                }
        return {"admitted": True, "reason": None, "budget": status}

    def summary(self, ticket_id: str, *, now_ms: int) -> dict[str, Any]:
        entries = self.list_entries(ticket_id=ticket_id, limit=1_000)
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "cost": 0.0,
            "cache_savings": 0.0,
        }
        by_agent: dict[str, dict[str, Any]] = {}
        by_step: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        cache_unknown_entries = 0
        for item in entries:
            for key in totals:
                totals[key] += item[key]
            if item["cache_unknown"]:
                cache_unknown_entries += 1
            for bucket, key in (
                (by_agent, item.get("agent_id") or "unassigned"),
                (by_step, item.get("step_id") or "unassigned"),
                (by_model, f"{item['provider']}/{item['model']}"),
            ):
                slot = bucket.setdefault(
                    key,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_hit_tokens": 0,
                        "cache_miss_tokens": 0,
                        "cost": 0.0,
                    },
                )
                for field in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens"):
                    slot[field] += item[field]
                slot["cost"] += item["cost"]
        totals["cost"] = round(totals["cost"], 8)
        totals["cache_savings"] = round(totals["cache_savings"], 8)
        for bucket in (by_agent, by_step, by_model):
            for slot in bucket.values():
                slot["cost"] = round(slot["cost"], 8)
        known = totals["cache_hit_tokens"] + totals["cache_miss_tokens"]
        return {
            "ticket_id": ticket_id,
            "entries": len(entries),
            "cache_unknown_entries": cache_unknown_entries,
            "cache_hit_rate": round(totals["cache_hit_tokens"] / known, 4)
            if known
            else None,
            "totals": totals,
            "by_agent": dict(sorted(by_agent.items())),
            "by_step": dict(sorted(by_step.items())),
            "by_model": dict(sorted(by_model.items())),
            "budget": self.budget_status(ticket_id, now_ms=now_ms),
        }

    def list_alerts(self, ticket_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if ticket_id:
                rows = self._conn.execute(
                    "SELECT * FROM cost_alerts WHERE ticket_id=? "
                    "ORDER BY created_at_ms, CASE level WHEN 'warning' THEN 0 ELSE 1 END",
                    (ticket_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM cost_alerts "
                    "ORDER BY created_at_ms, CASE level WHEN 'warning' THEN 0 ELSE 1 END"
                ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            out.append(item)
        return out

    # ---------------------------------------------------------------- 内部

    def _budget_status_conn(
        self, ticket_id: str, now_ms: int
    ) -> dict[str, Any] | None:
        budget = self._conn.execute(
            "SELECT * FROM ticket_budgets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if budget is None:
            return None
        agg = self._conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cost),0) AS cost,
                   COUNT(DISTINCT agent_id) AS workers
            FROM cost_entries WHERE ticket_id=?
            """,
            (ticket_id,),
        ).fetchone()
        used = {
            "input_tokens": int(agg["input_tokens"]),
            "output_tokens": int(agg["output_tokens"]),
            "cost": round(float(agg["cost"]), 8),
            "workers": int(agg["workers"]),
            "wall_time_s": max(0, (now_ms - int(budget["started_at_ms"])) // 1000),
        }
        limits = {
            "max_workers": int(budget["max_workers"]),
            "max_input_tokens": int(budget["max_input_tokens"]),
            "max_output_tokens": int(budget["max_output_tokens"]),
            "max_cost": float(budget["max_cost"]),
            "max_wall_time_s": int(budget["max_wall_time_s"]),
            "currency": budget["currency"],
        }
        ratios = {
            "workers": _ratio(used["workers"], limits["max_workers"]),
            "input_tokens": _ratio(used["input_tokens"], limits["max_input_tokens"]),
            "output_tokens": _ratio(used["output_tokens"], limits["max_output_tokens"]),
            "cost": _ratio(used["cost"], limits["max_cost"]),
            "wall_time": _ratio(used["wall_time_s"], limits["max_wall_time_s"]),
        }
        peak = max(ratios.values()) if ratios else 0.0
        if peak >= BUDGET_PAUSED_RATIO:
            state = "paused"
        elif peak >= BUDGET_WARNING_RATIO:
            state = "warning"
        else:
            state = "ok"
        alerts = self._conn.execute(
            "SELECT level, created_at_ms FROM cost_alerts WHERE ticket_id=? "
            "ORDER BY created_at_ms, CASE level WHEN 'warning' THEN 0 ELSE 1 END",
            (ticket_id,),
        ).fetchall()
        return {
            "ticket_id": ticket_id,
            "state": state,
            "used": used,
            "limits": limits,
            "ratios": {key: round(value, 4) for key, value in ratios.items()},
            "peak_ratio": round(peak, 4),
            "alerts": [
                {"level": row["level"], "created_at_ms": int(row["created_at_ms"])}
                for row in alerts
            ],
            "started_at_ms": int(budget["started_at_ms"]),
        }

    def _raise_alert_conn(
        self, ticket_id: str, status: dict[str, Any], now_ms: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO cost_alerts (alert_id, ticket_id, level, detail_json, created_at_ms)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticket_id, level) DO NOTHING
            """,
            (
                "alert-" + uuid.uuid4().hex[:16],
                ticket_id,
                status["state"],
                json.dumps(
                    {
                        "peak_ratio": status["peak_ratio"],
                        "used": status["used"],
                        "limits": status["limits"],
                    },
                    ensure_ascii=False,
                ),
                now_ms,
            ),
        )

    @staticmethod
    def _entry_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["cache_unknown"] = bool(data["cache_unknown"])
        return data


def _ratio(used: float, limit: float) -> float:
    if limit <= 0:
        return 1.0 if used > 0 else 0.0
    return used / limit
