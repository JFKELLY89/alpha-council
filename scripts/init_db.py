"""
ALPHA COUNCIL v2.5 - DATABASE INITIALIZATION and verification.

Creates the SQLite database, applies the schema, seeds the initial config
version and source registry, then verifies the result.

Place at: scripts/init_db.py

Usage:
    uv run python scripts/init_db.py
    uv run python scripts/init_db.py --verify        # check only, no writes
    uv run python scripts/init_db.py --reset         # DESTRUCTIVE, asks first
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.db.engine import Database, utc_now  # noqa: E402
from alpha_council.settings import (  # noqa: E402
    ensure_directories,
    get_settings,
    load_yaml,
)

# Spec Section 10.1 base reliability values.
SEED_SOURCES = [
    ("sec_edgar", "SEC EDGAR", "sec.gov", "regulator",
     "TIER_1_PRIMARY", 100.0, "sec"),
    ("alpaca_news", "Alpaca News", "alpaca.markets", "news_aggregator",
     "TIER_2_MAJOR_NEWS", 85.0, "alpaca_news"),
]


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def seed_config_version(db: Database, version: str) -> None:
    existing = await db.fetchone(
        "SELECT config_version FROM config_versions WHERE config_version=?",
        (version,),
    )
    if existing:
        say(f"  config_version {version!r} already present")
        return
    scoring = load_yaml("scoring")
    risk = load_yaml("risk_constitution")
    await db.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES(?,?,?,?,?,?)",
        (version, utc_now(), 1,
         json.dumps(scoring, default=str),
         json.dumps(risk, default=str),
         "initial seed from init_db"),
    )
    note = "" if scoring else "  (scoring.yaml not present yet - stored empty)"
    say(f"  seeded config_version {version!r}{note}")


async def seed_sources(db: Database) -> None:
    inserted = 0
    for sid, name, domain, stype, tier, rel, collector in SEED_SOURCES:
        row = await db.fetchone(
            "SELECT source_id FROM source_registry WHERE source_id=?", (sid,)
        )
        if row:
            continue
        await db.execute(
            "INSERT INTO source_registry(source_id, name, domain, source_type, "
            "tier, base_reliability, collector, enabled, config_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,1,'{}',?)",
            (sid, name, domain, stype, tier, rel, collector, utc_now()),
        )
        inserted += 1
    say(f"  seeded {inserted} source(s); {len(SEED_SOURCES)} total defined")


async def report(db: Database) -> None:
    tables = sorted(await db.table_names())
    say("")
    say(f"  tables ({len(tables)}):")
    for i in range(0, len(tables), 3):
        say("    " + "  ".join(f"{t:<24}" for t in tables[i:i + 3]))

    views = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    )
    if views:
        say(f"  views: {', '.join(v['name'] for v in views)}")

    say("")
    for label, sql in (
        ("schema_version", "SELECT value FROM schema_meta WHERE key='schema_version'"),
        ("config_versions", "SELECT COUNT(*) FROM config_versions"),
        ("source_registry", "SELECT COUNT(*) FROM source_registry"),
        ("decisions", "SELECT COUNT(*) FROM decisions"),
        ("gate_rejections", "SELECT COUNT(*) FROM gate_rejections"),
    ):
        say(f"  {label:<18}: {await db.fetchvalue(sql)}")


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    ensure_directories()
    db_path = Path(settings.database_path)

    say("=" * 66)
    say("ALPHA COUNCIL v2.5 - DATABASE INITIALIZATION")
    say("=" * 66)
    say(f"  path           : {db_path}")
    say(f"  exists         : {db_path.exists()}")
    say(f"  config_version : {settings.config_version}")

    try:
        settings.assert_paper_only()
        say("  paper-only     : PASS")
    except Exception as exc:  # noqa: BLE001
        say(f"  paper-only     : FAIL - {exc}")
        return 1

    if args.reset:
        if db_path.exists():
            resp = input(f"\nDELETE {db_path} and all trade history? [type YES]: ")
            if resp.strip() != "YES":
                say("  aborted")
                return 1
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db_path) + suffix)
                if p.exists():
                    p.unlink()
            say("  database deleted")

    async with Database(db_path) as db:
        if not args.verify:
            say("")
            say("APPLYING SCHEMA")
            await db.apply_schema()
            say("  schema applied")
            await seed_config_version(db, settings.config_version)
            await seed_sources(db)
            await db.log_event(
                "INFO", "init_db", "SCHEMA_APPLIED",
                "database initialized",
                {"config_version": settings.config_version},
            )

        say("")
        say("VERIFYING")
        problems = await db.verify_schema()
        if problems:
            for p in problems:
                say(f"  FAIL: {p}")
            return 2
        say("  all checks passed")
        await report(db)

    say("")
    say("=" * 66)
    say("DONE")
    say("=" * 66)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="verify only, make no changes")
    ap.add_argument("--reset", action="store_true",
                    help="DESTRUCTIVE: delete the database first")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
