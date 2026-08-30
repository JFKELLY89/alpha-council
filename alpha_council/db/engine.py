"""
Alpha Council v2.3 - async SQLite engine.

One shared aiosqlite connection guarded by an asyncio lock. SQLite in WAL mode
handles concurrent readers well but serializes writers, and the whole system is
a single process, so a connection pool would add complexity for no gain.

Spec Section 8.2: each pipeline stage persists its output before invoking the
next stage. If the process dies, the database shows the last completed state.

Place at: alpha_council/db/engine.py
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = "2.5.0"

REQUIRED_TABLES = {
    "schema_meta", "system_state", "config_versions",
    "market_observations", "market_bars", "data_quality",
    "discovery_candidates", "discovery_source_status", "funnel_snapshots",
    "source_registry", "intelligence_items", "intelligence_events",
    "scan_runs", "candidate_scores", "decisions", "agent_runs",
    "trade_proposals", "option_structures", "red_team_reviews",
    "risk_evaluations", "orders", "fills", "position_snapshots",
    "execution_calibrations", "fill_bias_estimates",
    "trade_journal", "shadow_trades", "shadow_marks",
    "decision_attribution", "gate_rejections", "rejected_shadows",
    "scenario_sets", "scenario_payoffs", "premarket_briefs",
    "strategy_lessons", "strategy_versions", "challenger_proposals",
    "strategy_shadow_decisions", "strategy_performance_snapshots",
    "promotion_recommendations",
    "api_usage", "system_events",
}


def utc_now() -> str:
    """Canonical timestamp format for every TEXT timestamp column."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SchemaError(RuntimeError):
    pass


class Database:
    """Thin async wrapper. Deliberately not an ORM."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited.")
        return self._conn

    # ------------------------------------------------------------------
    # core operations
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        async with self._write_lock:
            cur = await self.conn.execute(sql, params or ())
            rows = cur.rowcount
            await cur.close()
            return rows

    async def executemany(self, sql: str,
                          rows: Iterable[Sequence[Any]]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        async with self._write_lock:
            cur = await self.conn.executemany(sql, batch)
            n = cur.rowcount
            await cur.close()
            return n

    async def fetchone(self, sql: str,
                       params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, params or ())
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def fetchall(self, sql: str,
                       params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params or ())
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def fetchvalue(self, sql: str,
                         params: Sequence[Any] | None = None) -> Any:
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Explicit transaction. Rolls back on any exception.

        Holds the write lock for its whole duration, so keep the body short
        and never await a network call inside it.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    async def apply_schema(self, schema_path: Path | None = None) -> None:
        path = schema_path or SCHEMA_PATH
        if not path.exists():
            raise SchemaError(f"schema file not found: {path}")
        sql = path.read_text(encoding="utf-8")
        async with self._write_lock:
            await self.conn.executescript(sql)
        await self.set_meta("schema_version", SCHEMA_VERSION)

    async def table_names(self) -> set[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        return {r["name"] for r in rows}

    async def verify_schema(self) -> list[str]:
        """Returns a list of problems. Empty list means healthy."""
        problems: list[str] = []
        tables = await self.table_names()
        missing = REQUIRED_TABLES - tables
        if missing:
            problems.append(f"missing tables: {sorted(missing)}")

        fk = await self.fetchvalue("PRAGMA foreign_keys")
        if not fk:
            problems.append("foreign_keys pragma is OFF")

        mode = await self.fetchvalue("PRAGMA journal_mode")
        if str(mode).lower() != "wal":
            problems.append(f"journal_mode is {mode}, expected wal")

        violations = await self.fetchall("PRAGMA foreign_key_check")
        if violations:
            problems.append(f"{len(violations)} foreign key violation(s)")

        version = await self.get_meta("schema_version")
        if version != SCHEMA_VERSION:
            problems.append(f"schema_version is {version!r}, expected {SCHEMA_VERSION!r}")
        return problems

    # ------------------------------------------------------------------
    # metadata and state helpers
    # ------------------------------------------------------------------

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, utc_now()),
        )

    async def get_meta(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM schema_meta WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_state(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO system_state(key, value_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), utc_now()),
        )

    async def get_state(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone(
            "SELECT value_json FROM system_state WHERE key=?", (key,)
        )
        return json.loads(row["value_json"]) if row else default

    async def log_event(self, level: str, component: str, event_type: str,
                        message: str, context: dict[str, Any] | None = None,
                        decision_id: str | None = None) -> None:
        import uuid

        await self.execute(
            "INSERT INTO system_events(system_event_id, occurred_at, level, "
            "component, event_type, decision_id, message, context_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, utc_now(), level.upper(), component, event_type,
             decision_id, message, json.dumps(context or {}, default=str)),
        )


# ----------------------------------------------------------------------

_db: Database | None = None


def get_database(path: str | Path | None = None) -> Database:
    """Process-wide singleton. Pass an explicit path in tests."""
    global _db
    if path is not None:
        return Database(path)
    if _db is None:
        from alpha_council.settings import get_settings

        _db = Database(get_settings().database_path)
    return _db
