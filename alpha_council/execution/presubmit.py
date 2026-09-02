"""
Alpha Council v2.4 §17.4 - pre-submit refresh.

"No stale approval may be reused." Immediately before submission the
system re-fetches the underlying quote (must be fresh), re-fetches both
option legs, reprices the spread from CURRENT quotes through the same
deterministic builder that priced it originally, and hands the repriced
structure back so the Risk Constitution can be re-run against the limit
that will actually be submitted.

The council priced the structure 30-90 seconds ago on a derived feed.
This module is the difference between approving the trade that will be
submitted and approving the trade that used to exist.

Three outcomes, all explicit:

  OK        - repriced structure, same structure_id/rank, fresh quotes.
  STALE     - the underlying quote is not fresh enough to trade on.
  REPRICE   - current option quotes no longer form a valid structure
              (crossed, spread too wide, cost/width breached, ...).

Nothing here loosens a gate: the repriced spread passes through
SpreadBuilder._try_pair with the same tier filters, so a structure that
degraded since the council saw it is rejected, not submitted.

Place at: alpha_council/execution/presubmit.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.alpaca.rest_client import AlpacaError, AlpacaRestClient
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.options_engine.chain import ChainResult
from alpha_council.options_engine.spreads import SpreadBuilder, SpreadFilters, SpreadResult
from alpha_council.utils.math import safe_mid
from alpha_council.utils.time import age_seconds, parse_alpaca_ts, to_et, utc_now


@dataclass(slots=True)
class RefreshResult:
    ok: bool
    structure: OptionStructure | None = None
    underlying_price: float | None = None
    gate_id: str | None = None
    reason: str = ""


class PreSubmitRefresher:
    """Reprices an approved structure from live quotes before submission."""

    def __init__(self, api: AlpacaRestClient, market: MarketDataService,
                 config: dict[str, Any]):
        self.api = api
        self.market = market
        self.config = config
        eq = config.get("equity", {})
        self.max_underlying_age = float(eq.get("pre_submit_max_lag_seconds", 5.0))
        self.options_cfg = config.get("options", {})

    async def refresh(self, structure: OptionStructure,
                      tier_cfg: dict[str, Any]) -> RefreshResult:
        symbol = structure.symbol

        # --- 1. fresh underlying, or no trade -------------------------
        try:
            quote = await self.market.fresh_quote(symbol,
                                                  self.max_underlying_age)
        except Exception as exc:  # noqa: BLE001 - degrade to NO TRADE
            return RefreshResult(False, gate_id="EXEC_STALE_PRESUBMIT",
                                 reason=f"underlying refresh failed: {exc}"[:160])
        if quote is None:
            return RefreshResult(
                False, gate_id="EXEC_STALE_PRESUBMIT",
                reason=f"no underlying quote fresher than "
                       f"{self.max_underlying_age:.0f}s")
        underlying_now = quote.midpoint()
        if underlying_now is None or underlying_now <= 0:
            return RefreshResult(False, gate_id="EXEC_STALE_PRESUBMIT",
                                 reason="underlying quote has no usable mid")

        # --- 2. current option leg quotes -----------------------------
        long_leg, short_leg = structure.long_leg, structure.short_leg
        try:
            snaps = await self.api.get_option_snapshots(
                [long_leg.symbol, short_leg.symbol])
        except AlpacaError as exc:
            return RefreshResult(False, gate_id="EXEC_STALE_PRESUBMIT",
                                 reason=f"leg refresh failed: {exc}"[:160])

        now = utc_now()
        fresh_long = self._refresh_leg(long_leg, snaps.get(long_leg.symbol), now)
        fresh_short = self._refresh_leg(short_leg,
                                        snaps.get(short_leg.symbol), now)
        if fresh_long is None or fresh_short is None:
            bad = long_leg.symbol if fresh_long is None else short_leg.symbol
            return RefreshResult(
                False, gate_id="EXEC_REPRICE_FAILED",
                reason=f"{bad}: no valid two-sided quote at submit time")

        # --- 3. rebuild through the same deterministic builder --------
        filters = SpreadFilters.from_config(tier_cfg, self.options_cfg)

        # The per-leg spread gate normally lives in the chain fetch, which
        # this path bypasses; a leg whose quote widened past the tier's
        # ceiling since the council saw it is a degraded structure, not a
        # repriceable one.
        for fresh in (fresh_long, fresh_short):
            if fresh.spread_pct > filters.max_spread_pct:
                return RefreshResult(
                    False, gate_id="EXEC_REPRICE_FAILED",
                    reason=f"{fresh.symbol}: spread {fresh.spread_pct:.3f} "
                           f"now exceeds the tier ceiling "
                           f"{filters.max_spread_pct:.3f}")
        builder = SpreadBuilder(filters,
                                self.config.get("structure_weights"),
                                self.config.get("leg_liquidity_weights"))
        chain = ChainResult(symbol=symbol, underlying_price=underlying_now,
                            fetched_at=now)
        scratch = SpreadResult(symbol=symbol,
                               direction=structure.strategy.direction,
                               strategy=structure.strategy)
        repriced = builder._try_pair(
            fresh_long, fresh_short, structure.strategy, chain,
            to_et(now).date(), max_debit_allowed=None,
            fill_bias_buffer=0.0, result=scratch)
        if repriced is None:
            top = max(scratch.rejection_counts().items(),
                      key=lambda kv: kv[1], default=("UNKNOWN", 0))[0]
            return RefreshResult(
                False, gate_id="EXEC_REPRICE_FAILED",
                reason=f"repriced spread fails current filters: {top}")

        # The identity is the COUNCIL's structure; only the prices moved.
        # A new id here would orphan the risk row's foreign key and the
        # journal's structure record.
        repriced = repriced.model_copy(update={
            "structure_id": structure.structure_id,
            "rank": structure.rank,
        })
        return RefreshResult(True, structure=repriced,
                             underlying_price=underlying_now)

    @staticmethod
    def _refresh_leg(leg: OptionLeg, snapshot: dict[str, Any] | None,
                     now: Any) -> OptionLeg | None:
        """Current-quote copy of a leg, or None when unusable.

        Invalid bid/ask is rejected BEFORE any midpoint math, per §31.4.
        Greeks refresh when present; open interest keeps the chain-time
        value (OI is a prior-session figure either way).
        """
        if not snapshot:
            return None
        quote = snapshot.get("latestQuote") or {}
        bid, ask = quote.get("bp"), quote.get("ap")
        mid = safe_mid(bid, ask)
        if mid is None:
            return None

        quote_ts = parse_alpaca_ts(quote.get("t"))
        lag = age_seconds(quote_ts, now) or 0.0
        greeks = snapshot.get("greeks") or {}
        delta = greeks.get("delta")

        return leg.model_copy(update={
            "bid": float(bid),
            "ask": float(ask),
            "raw_mid": mid,
            # A freshly fetched quote needs no §5.4 delta adjustment; the
            # adjustment exists for quotes older than the fresh threshold.
            "adjusted_mid": mid,
            "quote_lag_seconds": lag,
            "quote_timestamp": quote_ts,
            "underlying_price_at_quote": None,
            "delta": float(delta) if delta is not None else leg.delta,
            "gamma": greeks.get("gamma", leg.gamma),
            "theta": greeks.get("theta", leg.theta),
            "vega": greeks.get("vega", leg.vega),
        })
