"""
Alpha Council v2.4 - additive database migration from v2.3.

Idempotent by design (spec §8.4): an already-present column or table is
treated as already-applied, not as a failure. Safe to run repeatedly.

Adds:
  * discovery_candidates, funnel_snapshots, execution_calibrations,
    discovery_source_status, fill_bias_estimates
  * discovery_source / candidate_track columns on candidate_scores and decisions
  * v_discovery_funnel and v_fill_bias views

Place at: scripts/migrate_v24.py

Usage:
    uv run python scripts/migrate_v24.py
    uv run python scripts/migrate_v24.py --dry
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.db.engine import Database, utc_now  # noqa: E402
from alpha_council.settings import get_settings  # noqa: E402

TARGET_SCHEMA_VERSION = "2.4.0"

NEW_TABLES: dict[str, str] = {
    "discovery_candidates": """
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            discovery_id     TEXT PRIMARY KEY,
            scan_id          TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            discovered_at    TEXT NOT NULL,
            expires_at       TEXT,
            source           TEXT NOT NULL,
            source_rank      INTEGER,
            discovery_reason TEXT NOT NULL,
            is_core          INTEGER NOT NULL CHECK(is_core IN (0,1)),
            asset_tradable   INTEGER NOT NULL CHECK(asset_tradable IN (0,1)),
            has_options      INTEGER NOT NULL CHECK(has_options IN (0,1)),
            data_density_ok  INTEGER NOT NULL CHECK(data_density_ok IN (0,1)),
            fast_score       REAL NOT NULL DEFAULT 0,
            discovery_boost  REAL NOT NULL DEFAULT 0,
            UNIQUE(scan_id, symbol, source)
        )
    """,
    "funnel_snapshots": """
        CREATE TABLE IF NOT EXISTS funnel_snapshots (
            scan_id             TEXT PRIMARY KEY,
            as_of               TEXT NOT NULL,
            discovery_count     INTEGER NOT NULL,
            stage0_survivors    INTEGER NOT NULL,
            prescore_survivors  INTEGER NOT NULL,
            options_prescreened INTEGER NOT NULL,
            final_candidates    INTEGER NOT NULL,
            councils_started    INTEGER NOT NULL,
            event_track_count   INTEGER NOT NULL DEFAULT 0,
            momentum_track_count INTEGER NOT NULL DEFAULT 0,
            source_counts_json  TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "discovery_source_status": """
        CREATE TABLE IF NOT EXISTS discovery_source_status (
            status_id           TEXT PRIMARY KEY,
            session_date        TEXT NOT NULL,
            source              TEXT NOT NULL,
            enabled             INTEGER NOT NULL CHECK(enabled IN (0,1)),
            probed_at           TEXT,
            disabled_at         TEXT,
            disable_reason      TEXT,
            symbols_contributed INTEGER NOT NULL DEFAULT 0,
            consecutive_errors  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_date, source)
        )
    """,
    "execution_calibrations": """
        CREATE TABLE IF NOT EXISTS execution_calibrations (
            calibration_id          TEXT PRIMARY KEY,
            decision_id             TEXT NOT NULL,
            symbol                  TEXT NOT NULL,
            side                    TEXT NOT NULL CHECK(side IN ('OPEN','CLOSE')),
            candidate_track         TEXT NOT NULL,
            direction               TEXT NOT NULL,
            submitted_at            TEXT NOT NULL,
            filled_at               TEXT,
            indicative_raw_mid      REAL NOT NULL,
            indicative_adjusted_mid REAL NOT NULL,
            natural_debit_estimate  REAL NOT NULL,
            initial_limit_debit     REAL NOT NULL,
            final_submitted_limit   REAL NOT NULL,
            actual_fill_debit       REAL,
            seconds_to_fill         REAL,
            limit_walk_steps        INTEGER NOT NULL DEFAULT 0,
            quote_lag_seconds       REAL NOT NULL,
            underlying_at_quote     REAL NOT NULL,
            underlying_at_submit    REAL NOT NULL,
            underlying_at_fill      REAL,
            fill_bias_vs_adjusted   REAL,
            fill_bias_vs_limit      REAL,
            fill_slippage_pct       REAL
        )
    """,
    "fill_bias_estimates": """
        CREATE TABLE IF NOT EXISTS fill_bias_estimates (
            estimate_id            TEXT PRIMARY KEY,
            computed_at            TEXT NOT NULL,
            side                   TEXT NOT NULL CHECK(side IN ('OPEN','CLOSE')),
            direction              TEXT,
            sample_size            INTEGER NOT NULL,
            median_bias            REAL NOT NULL DEFAULT 0,
            p80_bias               REAL NOT NULL DEFAULT 0,
            median_seconds_to_fill REAL,
            mean_limit_walk_steps  REAL NOT NULL DEFAULT 0,
            applied_buffer         REAL NOT NULL DEFAULT 0
        )
    """,
}

NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_discovery_scan_score "
    "ON discovery_candidates(scan_id, fast_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_discovery_symbol_time "
    "ON discovery_candidates(symbol, discovered_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_discovery_source "
    "ON discovery_candidates(source, discovered_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_exec_cal_symbol_time "
    "ON execution_calibrations(symbol, submitted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_exec_cal_side "
    "ON execution_calibrations(side, submitted_at DESC)",
]

# (table, column, type) - applied only when the column is absent.
NEW_COLUMNS = [
    ("candidate_scores", "discovery_source", "TEXT"),
    ("candidate_scores", "candidate_track", "TEXT"),
    ("candidate_scores", "fast_score", "REAL"),
    ("decisions", "discovery_source", "TEXT"),
    ("decisions", "candidate_track", "TEXT"),
    ("trade_journal", "candidate_track", "TEXT"),
    ("option_structures", "indicative_buffer", "REAL"),
    ("orders", "limit_walk_step", "INTEGER"),
]

NEW_VIEWS = {
    "v_discovery_funnel": """
        CREATE VIEW IF NOT EXISTS v_discovery_funnel AS
        SELECT
            f.scan_id,
            f.as_of,
            f.discovery_count,
            f.stage0_survivors,
            f.prescore_survivors,
            f.options_prescreened,
            f.final_candidates,
            f.councils_started,
            f.event_track_count,
            f.momentum_track_count,
            ROUND(CAST(f.councils_started AS REAL)
                  / NULLIF(f.discovery_count, 0), 5) AS survival_rate
        FROM funnel_snapshots f
        ORDER BY f.as_of DESC
    """,
    "v_fill_bias": """
        CREATE VIEW IF NOT EXISTS v_fill_bias AS
        SELECT
            side,
            direction,
            COUNT(*) AS n_fills,
            ROUND(AVG(fill_bias_vs_adjusted), 4) AS mean_bias,
            ROUND(AVG(fill_slippage_pct), 5) AS mean_slippage_pct,
            ROUND(AVG(seconds_to_fill), 1) AS mean_seconds_to_fill,
            ROUND(AVG(limit_walk_steps), 2) AS mean_walk_steps
        FROM execution_calibrations
        WHERE actual_fill_debit IS NOT NULL
          AND quote_lag_seconds <= 900
        GROUP BY side, direction
    """,
    "v_discovery_source_yield": """
        CREATE VIEW IF NOT EXISTS v_discovery_source_yield AS
        SELECT
            d.source,
            COUNT(DISTINCT d.symbol) AS symbols_discovered,
            COUNT(DISTINCT c.candidate_id) AS reached_candidate,
            COUNT(DISTINCT dec.decision_id) AS reached_council
        FROM discovery_candidates d
        LEFT JOIN candidate_scores c
               ON c.symbol = d.symbol AND c.scan_id = d.scan_id
        LEFT JOIN decisions dec
               ON dec.candidate_id = c.candidate_id
        GROUP BY d.source
        ORDER BY reached_council DESC, symbols_discovered DESC
    """,
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def existing_columns(db: Database, table: str) -> set[str]:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows}


async def run(dry: bool) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    db_path = Path(settings.database_path)

    say("=" * 66)
    say("ALPHA COUNCIL - MIGRATION v2.3 -> v2.4")
    say("=" * 66)
    say(f"  database : {db_path}")
    say(f"  mode     : {'DRY RUN (no writes)' if dry else 'APPLY'}")

    if not db_path.exists():
        say("  database does not exist. Run scripts/init_db.py first.")
        return 1

    applied, skipped = 0, 0

    async with Database(db_path) as db:
        before = await db.get_meta("schema_version")
        say(f"  current schema_version: {before}")

        tables = await db.table_names()

        say("")
        say("TABLES")
        for name, ddl in NEW_TABLES.items():
            if name in tables:
                say(f"  skip   {name} (exists)")
                skipped += 1
                continue
            say(f"  create {name}")
            if not dry:
                await db.execute(ddl)
            applied += 1

        say("")
        say("COLUMNS")
        for table, column, coltype in NEW_COLUMNS:
            if table not in tables:
                say(f"  skip   {table}.{column} (table absent)")
                skipped += 1
                continue
            cols = await existing_columns(db, table)
            if column in cols:
                say(f"  skip   {table}.{column} (exists)")
                skipped += 1
                continue
            say(f"  add    {table}.{column} {coltype}")
            if not dry:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            applied += 1

        say("")
        say("INDEXES AND VIEWS")
        if not dry:
            for ddl in NEW_INDEXES:
                await db.execute(ddl)
            for ddl in NEW_VIEWS.values():
                await db.execute(ddl)
        say(f"  {len(NEW_INDEXES)} indexes, {len(NEW_VIEWS)} views "
            f"({'skipped in dry run' if dry else 'applied'})")

        if not dry:
            await db.set_meta("schema_version", TARGET_SCHEMA_VERSION)
            await db.log_event(
                "INFO", "migrate_v24", "SCHEMA_MIGRATED",
                f"migrated {before} -> {TARGET_SCHEMA_VERSION}",
                {"applied": applied, "skipped": skipped},
            )

        say("")
        say("VERIFY")
        problems = await db.verify_schema()
        # verify_schema still expects 2.3.0; a version mismatch here is
        # expected until engine.py SCHEMA_VERSION is bumped.
        real = [p for p in problems if "schema_version" not in p]
        if real:
            for p in real:
                say(f"  FAIL: {p}")
            return 2

        final_tables = sorted(await db.table_names())
        views = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
        say(f"  tables: {len(final_tables)}")
        say(f"  views : {', '.join(v['name'] for v in views)}")

    say("")
    say("=" * 66)
    say(f"DONE - {applied} change(s) applied, {skipped} already present")
    if dry:
        say("Dry run only. Re-run without --dry to apply.")
    else:
        say("Next: bump SCHEMA_VERSION to '2.4.0' in alpha_council/db/engine.py")
        say("and add the new table names to REQUIRED_TABLES.")
    say("=" * 66)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="show changes, write nothing")
    return asyncio.run(run(ap.parse_args().dry))


if __name__ == "__main__":
    sys.exit(main())
