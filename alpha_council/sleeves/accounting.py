"""
Alpha Council v2.7 - sleeve accounting (operator rebalance, 2026-10-30).

The account now holds three books: the options desk, an ETF beta sleeve,
and a council-driven stock sleeve. One cash pool, one broker equity
number - so every risk judgment the options Constitution makes must see
the OPTIONS DESK'S capital, not the whole account, or a red market day
on 60% beta exposure halts an options desk that did nothing wrong.

The isolation identity:

    sleeve_pnl_total = sleeve_market_value - sleeve_net_invested
    options_nav      = account_equity - sleeve_pnl_total

Buying $28k of ETFs moves cash into market value and leaves options_nav
unchanged; only sleeve PROFIT or LOSS moves it. Net invested is the sum
of filled sleeve order cash flows (buys positive, sells negative), so
realized sleeve P&L stays captured after a rebalance sell.

Known approximation, documented rather than hidden: the intraday
decomposition uses the broker's per-position unrealized_intraday_pl and
ignores same-day REALIZED sleeve P&L (a rebalance sell booked this
morning); the error is bounded by that day's sold notional drift and
corrects itself at the next session boundary.

Place at: alpha_council/sleeves/accounting.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from alpha_council.db.engine import Database
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now

# Idempotent DDL, applied at startup so a live database predating the
# sleeves gains the tables without a migration. schema.sql carries the
# same definitions for fresh init_db runs.
SLEEVES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS sleeve_orders (
        order_pk        TEXT PRIMARY KEY,
        sleeve          TEXT NOT NULL CHECK(sleeve IN ('ETF','STOCK')),
        decision_id     TEXT,
        client_order_id TEXT NOT NULL UNIQUE,
        alpaca_order_id TEXT,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL CHECK(side IN ('buy','sell')),
        qty             REAL NOT NULL,
        limit_price     REAL NOT NULL,
        status          TEXT NOT NULL,
        filled_qty      REAL NOT NULL DEFAULT 0,
        fill_price      REAL,
        notional        REAL,
        reason          TEXT NOT NULL DEFAULT '',
        submitted_at    TEXT,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sleeve_orders_sleeve
        ON sleeve_orders(sleeve, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS sleeve_snapshots (
        snapshot_id  TEXT PRIMARY KEY,
        as_of        TEXT NOT NULL,
        sleeve       TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        qty          REAL NOT NULL,
        price        REAL,
        market_value REAL NOT NULL,
        target_value REAL,
        raw_json     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sleeve_snapshots
        ON sleeve_snapshots(sleeve, as_of DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS sleeve_trades (
        trade_id          TEXT PRIMARY KEY,
        decision_id       TEXT UNIQUE,
        symbol            TEXT NOT NULL,
        qty               INTEGER NOT NULL,
        entry_price       REAL NOT NULL,
        stop_price        REAL NOT NULL,
        target_price      REAL,
        horizon_days      INTEGER NOT NULL,
        opened_at         TEXT NOT NULL,
        closed_at         TEXT,
        status            TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
        exit_reason       TEXT,
        exit_price        REAL,
        realized_pnl      REAL,
        thesis            TEXT NOT NULL DEFAULT '',
        invalidation_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sleeve_trades_status
        ON sleeve_trades(status, opened_at DESC)
    """,
)


@dataclass(slots=True)
class SleeveAccounting:
    """One consistent read of the sleeves against the broker."""

    sleeve_market_value: float = 0.0
    net_invested: float = 0.0
    intraday_pl: float = 0.0
    by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def sleeve_pnl_total(self) -> float:
        return self.sleeve_market_value - self.net_invested

    def options_nav(self, account_equity: float) -> float:
        return account_equity - self.sleeve_pnl_total


async def equity_positions(orders: Any) -> list[dict[str, Any]]:
    """Every us_equity position at the broker (ETFs are us_equity too)."""
    payload = await orders.api._get(f"{orders.api.trade_base}/v2/positions")
    rows = payload if isinstance(payload, list) else []
    return [p for p in rows if p.get("asset_class") == "us_equity"]


async def read_accounting(orders: Any, db: Database,
                          sleeve_symbols: Iterable[str]) -> SleeveAccounting:
    """Market value and intraday P&L from the broker, flows from our book."""
    symbols = {s.upper() for s in sleeve_symbols}
    out = SleeveAccounting()
    if symbols:
        for p in await equity_positions(orders):
            symbol = str(p.get("symbol", "")).upper()
            if symbol not in symbols:
                continue
            mv = float(p.get("market_value") or 0.0)
            intraday = float(p.get("unrealized_intraday_pl") or 0.0)
            out.sleeve_market_value += mv
            out.intraday_pl += intraday
            out.by_symbol[symbol] = {
                "qty": float(p.get("qty") or 0.0),
                "market_value": mv,
                "price": float(p.get("current_price") or 0.0),
                "intraday_pl": intraday,
            }
    row = await db.fetchone(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN notional "
        "ELSE -notional END), 0) AS invested "
        "FROM sleeve_orders WHERE status='filled' AND notional IS NOT NULL")
    out.net_invested = float((row or {}).get("invested") or 0.0)
    return out


async def record_snapshots(db: Database, sleeve: str,
                           rows: list[dict[str, Any]],
                           now: datetime | None = None) -> int:
    now = now or utc_now()
    inserted = 0
    for r in rows:
        await db.execute(
            "INSERT INTO sleeve_snapshots(snapshot_id, as_of, sleeve, symbol, "
            "qty, price, market_value, target_value) VALUES(?,?,?,?,?,?,?,?)",
            (f"slvsnap_{new_uuid()[:12]}", iso_utc(now), sleeve,
             r.get("symbol"), float(r.get("qty") or 0.0), r.get("price"),
             float(r.get("market_value") or 0.0), r.get("target_value")))
        inserted += 1
    return inserted
