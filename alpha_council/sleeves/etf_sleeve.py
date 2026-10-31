"""
Alpha Council v2.7 - the ETF beta sleeve (operator rebalance, 2026-10-30).

Target allocation, not alpha: hold a configured basket of liquid ETFs at
fixed weights summing to sleeves.etf.target_pct_of_equity of the WHOLE
account, rebalancing only when a leg drifts outside its band. No LLM
touches this sleeve - the whole strategy is four numbers in scoring.yaml
and this file's arithmetic, which is exactly why it can deploy capital
the options desk's edge question doesn't gate.

Discipline shared with the rest of the desk:
  - whole shares, marketable DAY limits, everything journaled
    (sleeve_orders) so the isolation identity in accounting.py holds;
  - sells before buys, so a rebalance frees cash before it spends it;
  - a drift inside the band or under the notional floor is NOT traded -
    dust rebalancing is how a beta sleeve bleeds spread.

Place at: alpha_council/sleeves/etf_sleeve.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.sleeves.accounting import equity_positions, record_snapshots
from alpha_council.sleeves.equity_exec import submit_marketable
from alpha_council.utils.time import utc_now


@dataclass(slots=True)
class PlannedOrder:
    symbol: str
    side: str                 # buy | sell
    qty: int
    reference_price: float    # ask for buys, bid for sells
    target_value: float
    current_value: float

    @property
    def notional(self) -> float:
        return round(self.qty * self.reference_price, 2)


@dataclass(slots=True)
class RebalancePlan:
    equity: float
    sleeve_target: float
    current_value: float
    orders: list[PlannedOrder] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "sell"]

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "buy"]


def plan_rebalance(equity: float, weights: dict[str, float],
                   target_pct: float, holdings: dict[str, dict[str, float]],
                   quotes: dict[str, dict[str, float]],
                   band_rel: float = 0.20,
                   min_order_notional: float = 250.0,
                   max_order_pct_of_equity: float = 10.0) -> RebalancePlan:
    """Pure planning: targets vs holdings -> whole-share orders.

    ``holdings``: symbol -> {market_value, qty}. ``quotes``: symbol ->
    {bid, ask, price}. A leg trades only when its dollar drift exceeds
    both the relative band and the notional floor; a missing or crossed
    quote skips the leg loudly instead of pricing an order off fiction.
    """
    sleeve_target = equity * target_pct / 100.0
    plan = RebalancePlan(
        equity=equity, sleeve_target=round(sleeve_target, 2),
        current_value=round(sum(h.get("market_value", 0.0)
                                for h in holdings.values()), 2))
    max_order = equity * max_order_pct_of_equity / 100.0

    for symbol, weight in weights.items():
        target_value = sleeve_target * float(weight) / 100.0
        current = float(holdings.get(symbol, {}).get("market_value", 0.0))
        delta = target_value - current
        threshold = max(min_order_notional, band_rel * target_value)
        if abs(delta) < threshold:
            continue

        quote = quotes.get(symbol) or {}
        bid, ask = quote.get("bid"), quote.get("ask")
        price = ask if delta > 0 else bid
        if not price or price <= 0:
            plan.skipped.append(f"{symbol}: no usable quote")
            continue

        qty = math.floor(min(abs(delta), max_order) / float(price))
        if delta < 0:
            held = int(holdings.get(symbol, {}).get("qty", 0))
            qty = min(qty, held)
        if qty < 1:
            plan.skipped.append(f"{symbol}: drift ${delta:,.0f} under one share")
            continue
        plan.orders.append(PlannedOrder(
            symbol=symbol, side="buy" if delta > 0 else "sell", qty=qty,
            reference_price=float(price), target_value=round(target_value, 2),
            current_value=round(current, 2)))

    # Free cash before spending it.
    plan.orders.sort(key=lambda o: 0 if o.side == "sell" else 1)
    return plan


class ETFSleeve:
    def __init__(self, orders: Any, market: Any, db: Database,
                 scoring: dict[str, Any]):
        cfg = scoring.get("sleeves", {}).get("etf", {})
        self.orders = orders
        self.market = market
        self.db = db
        self.enabled = bool(cfg.get("enabled", False))
        self.target_pct = float(cfg.get("target_pct_of_equity", 30.0))
        self.weights = {str(s).upper(): float(w)
                        for s, w in (cfg.get("weights") or {}).items()}
        self.band_rel = float(cfg.get("rebalance_band_rel", 0.20))
        self.min_order_notional = float(cfg.get("min_order_notional", 250.0))
        self.max_order_pct = float(cfg.get("max_order_pct_of_equity", 10.0))

    @property
    def symbols(self) -> list[str]:
        return list(self.weights)

    async def _holdings_and_quotes(self) -> tuple[dict[str, dict[str, float]],
                                                  dict[str, dict[str, float]]]:
        holdings: dict[str, dict[str, float]] = {}
        for p in await equity_positions(self.orders):
            symbol = str(p.get("symbol", "")).upper()
            if symbol in self.weights:
                holdings[symbol] = {
                    "market_value": float(p.get("market_value") or 0.0),
                    "qty": float(p.get("qty") or 0.0),
                }
        quotes: dict[str, dict[str, float]] = {}
        snaps = await self.market.snapshots(self.symbols)
        for symbol, snap in snaps.items():
            # IEX quotes go one-sided on quieter ETFs; a marketable limit
            # padded off the signal price still crosses, and the executor's
            # timeout-cancel bounds the cost of being wrong.
            signal = float(snap.signal_price() or snap.mid or 0.0)
            bid = float(snap.quote.bid or 0.0) or (signal * 0.999)
            ask = float(snap.quote.ask or 0.0) or (signal * 1.001)
            quotes[symbol.upper()] = {"bid": bid, "ask": ask,
                                      "price": signal}
        return holdings, quotes

    async def plan(self, equity: float) -> RebalancePlan:
        holdings, quotes = await self._holdings_and_quotes()
        return plan_rebalance(
            equity, self.weights, self.target_pct, holdings, quotes,
            band_rel=self.band_rel,
            min_order_notional=self.min_order_notional,
            max_order_pct_of_equity=self.max_order_pct)

    async def rebalance(self, equity: float,
                        execute: bool = True) -> dict[str, Any]:
        """Plan and (optionally) execute. Returns a loggable summary."""
        plan = await self.plan(equity)
        summary: dict[str, Any] = {
            "sleeve_target": plan.sleeve_target,
            "current_value": plan.current_value,
            "orders_planned": len(plan.orders),
            "skipped": plan.skipped,
            "fills": [], "unfilled": [],
        }
        if not execute or not plan.orders:
            return summary
        for order in plan.orders:
            filled = await submit_marketable(
                self.orders, self.db, sleeve="ETF", symbol=order.symbol,
                side=order.side, qty=order.qty,
                reference_price=order.reference_price,
                reason=f"rebalance toward ${order.target_value:,.0f}")
            if filled is not None:
                summary["fills"].append(
                    f"{order.side} {int(float(filled.get('filled_qty') or 0))} "
                    f"{order.symbol} @ {filled.get('filled_avg_price')}")
            else:
                summary["unfilled"].append(f"{order.side} {order.qty} "
                                           f"{order.symbol}")
        return summary

    async def snapshot(self, now: datetime | None = None) -> int:
        """Nightly per-symbol record for attribution and the reviews."""
        now = now or utc_now()
        holdings, quotes = await self._holdings_and_quotes()
        rows = []
        for symbol in self.symbols:
            h = holdings.get(symbol, {})
            rows.append({
                "symbol": symbol,
                "qty": h.get("qty", 0.0),
                "price": (quotes.get(symbol) or {}).get("price"),
                "market_value": h.get("market_value", 0.0),
                "target_value": None,
            })
        return await record_snapshots(self.db, "ETF", rows, now)

    async def liquidate_all(self, execute: bool = False) -> list[str]:
        """Sell every sleeve holding at the bid. Used by close_all --sleeves."""
        holdings, quotes = await self._holdings_and_quotes()
        actions: list[str] = []
        for symbol, h in holdings.items():
            qty = int(h.get("qty", 0))
            bid = (quotes.get(symbol) or {}).get("bid") or 0.0
            if qty < 1 or bid <= 0:
                actions.append(f"{symbol}: SKIPPED (qty {qty}, bid {bid})")
                continue
            actions.append(f"{'SELL' if execute else 'would sell'} "
                           f"{qty} {symbol} @ {bid:.2f}")
            if execute:
                await submit_marketable(
                    self.orders, self.db, sleeve="ETF", symbol=symbol,
                    side="sell", qty=qty, reference_price=bid,
                    reason="liquidate_all")
        return actions
