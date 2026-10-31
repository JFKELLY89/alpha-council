"""
Alpha Council v2.7 - capital sleeves (operator rebalance, 2026-10-30).

Target allocation: 30% ETF beta sleeve, 30% council-driven stock sleeve,
the options desk unchanged under its existing hard caps (sizing review
2026-11-30), remainder cash. One facade so the scheduler and the scripts
hold a single handle; the isolation identity that keeps the options
Constitution honest lives in accounting.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.sleeves.accounting import (
    SLEEVES_DDL,
    SleeveAccounting,
    read_accounting,
)
from alpha_council.sleeves.etf_sleeve import ETFSleeve
from alpha_council.sleeves.stock_sleeve import StockSleeve


class SleeveManager:
    """One handle over both sleeves plus the shared accounting read."""

    def __init__(self, orders: Any, market: Any, db: Database,
                 scoring: dict[str, Any], council: Any = None,
                 journal: Any = None):
        self.db = db
        self.orders = orders
        cfg = scoring.get("sleeves", {})
        self.enabled = bool(cfg.get("enabled", False))
        self.etf = ETFSleeve(orders, market, db, scoring)
        self.stock = StockSleeve(council, orders, market, journal, db,
                                 scoring)
        # The stock sleeve never trades the beta basket or the priors
        # cohort: those symbols are the ETF sleeve's and the options
        # desk's territory.
        cohort = {str(s).upper()
                  for s in scoring.get("priors", {}).get("etf_cohort", [])}
        self.stock.excluded = cohort | set(self.etf.symbols)

    async def ensure_schema(self) -> None:
        for ddl in SLEEVES_DDL:
            await self.db.execute(ddl)

    async def restore(self) -> int:
        return await self.stock.restore()

    def all_symbols(self) -> set[str]:
        return set(self.etf.symbols) | self.stock.held_symbols()

    async def accounting(self) -> SleeveAccounting:
        if not self.enabled:
            return SleeveAccounting()
        return await read_accounting(self.orders, self.db,
                                     self.all_symbols())

    async def snapshot_all(self, now: datetime | None = None) -> int:
        written = 0
        if self.etf.enabled:
            written += await self.etf.snapshot(now)
        if self.stock.enabled and self.stock.tracked:
            written += await self.stock.snapshot(now)
        return written
