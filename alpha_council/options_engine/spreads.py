"""
Alpha Council v2.4 - deterministic vertical spread construction.

Builds bull call and bear put debit verticals from normalized chain legs,
scores them, and returns the top five. No LLM ever sees a contract that did
not come out of here, and no LLM may modify one.

The cost/width constraint is the whole calibration story. For a debit
vertical, RR = (1 - c/w) / (c/w). A 0.60/0.33 delta spread typically costs
45-55% of its width, so v2.2's hard RR >= 1.20 (c/w <= 0.4545) eliminated
nearly every compliant structure before any other gate was consulted.
Constraining c/w directly expresses the same preference in a form the chain
can actually satisfy.

Place at: alpha_council/options_engine/spreads.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.models.enums import Direction, StrategyType
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.options_engine.chain import ChainResult
from alpha_council.utils.ids import structure_id
from alpha_council.utils.math import (
    clip,
    delta_fit_score,
    dte_fit_score,
    freshness_bucket_score,
    log_score,
    reward_risk_from_cost_width,
    rr_score,
)
from alpha_council.utils.time import to_et, utc_now


@dataclass(slots=True)
class SpreadFilters:
    long_delta_min: float = 0.52
    long_delta_max: float = 0.72
    short_delta_min: float = 0.22
    short_delta_max: float = 0.42
    long_delta_target: float = 0.60
    short_delta_target: float = 0.33
    max_cost_to_width: float = 0.55
    max_spread_pct: float = 0.15
    tick_size: float = 0.01
    indicative_buffer_min: float = 0.02
    indicative_buffer_pct: float = 0.05
    fresh_quote_seconds: float = 60.0
    structures_returned: int = 5
    min_distinct_strike_pairs: int = 2

    @classmethod
    def from_config(cls, tier_cfg: dict[str, Any],
                    options_cfg: dict[str, Any]) -> "SpreadFilters":
        long_d = tier_cfg.get("long_delta", [0.52, 0.72])
        short_d = tier_cfg.get("short_delta", [0.22, 0.42])
        return cls(
            long_delta_min=float(long_d[0]), long_delta_max=float(long_d[1]),
            short_delta_min=float(short_d[0]), short_delta_max=float(short_d[1]),
            long_delta_target=float(tier_cfg.get("long_delta_target", 0.60)),
            short_delta_target=float(tier_cfg.get("short_delta_target", 0.33)),
            max_cost_to_width=float(tier_cfg.get("max_cost_to_width", 0.55)),
            max_spread_pct=float(tier_cfg.get("max_leg_spread_pct", 0.15)),
            tick_size=float(options_cfg.get("tick_size", 0.01)),
            indicative_buffer_min=float(
                options_cfg.get("indicative_buffer_min", 0.02)),
            indicative_buffer_pct=float(
                options_cfg.get("indicative_buffer_pct", 0.05)),
            fresh_quote_seconds=float(
                options_cfg.get("fresh_quote_seconds", 60)),
            structures_returned=int(options_cfg.get("structures_returned", 5)),
            min_distinct_strike_pairs=int(
                options_cfg.get("min_distinct_strike_pairs", 2)),
        )


@dataclass(slots=True)
class SpreadResult:
    symbol: str
    direction: Direction
    strategy: StrategyType
    structures: list[OptionStructure] = field(default_factory=list)
    rejections: list[tuple[str, str, str]] = field(default_factory=list)
    combinations_tried: int = 0

    @property
    def best(self) -> OptionStructure | None:
        return self.structures[0] if self.structures else None

    @property
    def options_opportunity_score(self) -> float:
        return self.best.structure_score if self.best else 0.0

    @property
    def options_liquidity_score(self) -> float:
        return self.best.liquidity_score if self.best else 0.0

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, gate, _ in self.rejections:
            counts[gate] = counts.get(gate, 0) + 1
        return counts


def round_to_tick(value: float, tick: float = 0.01) -> float:
    return round(round(value / tick) * tick, 2)


def leg_liquidity_score(leg: OptionLeg, max_spread_pct: float,
                        weights: dict[str, float] | None = None) -> float:
    w = weights or {"spread": 0.45, "open_interest": 0.30,
                    "volume": 0.15, "freshness": 0.10}
    spread_score = 100.0 * clip(1.0 - leg.spread_pct / max_spread_pct, 0.0, 1.0)
    oi_score = log_score(leg.open_interest or 0, 5000)
    vol_score = log_score(leg.volume or 0, 2000)
    fresh = freshness_bucket_score(leg.quote_lag_seconds)
    return clip(w["spread"] * spread_score + w["open_interest"] * oi_score
                + w["volume"] * vol_score + w["freshness"] * fresh)


class SpreadBuilder:
    """Generates and ranks defined-risk debit verticals."""

    def __init__(self, filters: SpreadFilters,
                 structure_weights: dict[str, float] | None = None,
                 leg_weights: dict[str, float] | None = None):
        self.f = filters
        self.structure_weights = structure_weights or {
            "liquidity": 0.30, "reward_risk": 0.25, "delta_fit": 0.20,
            "dte_fit": 0.15, "cost_efficiency": 0.10,
        }
        self.leg_weights = leg_weights

    # ---- leg selection ---------------------------------------------

    def _candidate_legs(self, legs: Sequence[OptionLeg],
                        result: SpreadResult) -> tuple[list[OptionLeg],
                                                       list[OptionLeg]]:
        longs, shorts = [], []
        for leg in legs:
            d = abs(leg.delta)
            in_long = self.f.long_delta_min <= d <= self.f.long_delta_max
            in_short = self.f.short_delta_min <= d <= self.f.short_delta_max
            if in_long:
                longs.append(leg)
            if in_short:
                shorts.append(leg)
            if not in_long and not in_short:
                result.rejections.append(
                    (leg.symbol, "OPT_DELTA_OUT_OF_BAND", f"{d:.3f}"))
        return longs, shorts

    # ---- construction ----------------------------------------------

    def build(self, chain: ChainResult, direction: Direction,
              max_debit_allowed: float | None = None,
              fill_bias_buffer: float = 0.0,
              now: datetime | None = None) -> SpreadResult:
        now = now or utc_now()
        strategy = (StrategyType.BULL_CALL_DEBIT
                    if direction is Direction.BULLISH
                    else StrategyType.BEAR_PUT_DEBIT)
        result = SpreadResult(symbol=chain.symbol, direction=direction,
                              strategy=strategy)

        legs = chain.legs_for(strategy.option_type)
        if len(legs) < 2:
            result.rejections.append(
                (chain.symbol, "OPT_CHAIN_TOO_THIN", f"{len(legs)} legs"))
            return result

        longs, shorts = self._candidate_legs(legs, result)
        if not longs or not shorts:
            result.rejections.append(
                (chain.symbol, "OPT_NO_DELTA_PAIR",
                 f"{len(longs)} long / {len(shorts)} short candidates"))
            return result

        today = to_et(now).date()
        built: list[OptionStructure] = []

        for long_leg in longs:
            for short_leg in shorts:
                if long_leg.symbol == short_leg.symbol:
                    continue
                if long_leg.expiration != short_leg.expiration:
                    continue
                result.combinations_tried += 1

                structure = self._try_pair(
                    long_leg, short_leg, strategy, chain, today,
                    max_debit_allowed, fill_bias_buffer, result)
                if structure is not None:
                    built.append(structure)

        built.sort(key=lambda s: s.structure_score, reverse=True)
        result.structures = self._diversify(built)
        return result

    def _try_pair(self, long_leg: OptionLeg, short_leg: OptionLeg,
                  strategy: StrategyType, chain: ChainResult, today,
                  max_debit_allowed: float | None,
                  fill_bias_buffer: float,
                  result: SpreadResult) -> OptionStructure | None:
        tag = f"{long_leg.strike:g}/{short_leg.strike:g}"

        if strategy is StrategyType.BULL_CALL_DEBIT:
            if long_leg.strike >= short_leg.strike:
                return None
        else:
            if long_leg.strike <= short_leg.strike:
                return None

        width = abs(short_leg.strike - long_leg.strike)
        if width <= 0:
            return None

        adjusted_mid_debit = long_leg.adjusted_mid - short_leg.adjusted_mid
        raw_mid_debit = long_leg.raw_mid - short_leg.raw_mid
        natural_debit = long_leg.ask - short_leg.bid

        if adjusted_mid_debit <= 0 or natural_debit <= 0:
            result.rejections.append((tag, "OPT_DEBIT_NON_POSITIVE",
                                      f"{adjusted_mid_debit:.2f}"))
            return None
        if natural_debit >= width:
            result.rejections.append((tag, "OPT_NATURAL_EXCEEDS_WIDTH",
                                      f"{natural_debit:.2f}/{width:.2f}"))
            return None

        max_lag = max(long_leg.quote_lag_seconds, short_leg.quote_lag_seconds)
        stale = max_lag > self.f.fresh_quote_seconds
        # Padding the limit for age and having actually delta-adjusted the
        # price are different facts. chain.py only adjusts when an underlying
        # reference exists at the quote's timestamp, so the flag must follow
        # that, not the lag.
        was_adjusted = any(leg.underlying_price_at_quote is not None
                           for leg in (long_leg, short_leg))

        # The buffer is the price of trading on a derived, non-OPRA quote.
        # It is recorded separately so §17.5 can measure what it cost.
        buffer = 0.0
        if stale:
            buffer = max(self.f.indicative_buffer_min,
                         self.f.indicative_buffer_pct * adjusted_mid_debit)
        buffer += max(0.0, fill_bias_buffer)

        limit = adjusted_mid_debit + 0.25 * (natural_debit - adjusted_mid_debit)
        limit = round_to_tick(limit + buffer, self.f.tick_size)
        limit = min(limit, round_to_tick(natural_debit, self.f.tick_size))
        if max_debit_allowed is not None:
            limit = min(limit, round_to_tick(max_debit_allowed, self.f.tick_size))

        if limit <= 0 or limit >= width:
            result.rejections.append((tag, "OPT_LIMIT_INVALID", f"{limit:.2f}"))
            return None

        cost_to_width = limit / width
        if cost_to_width > self.f.max_cost_to_width:
            result.rejections.append(
                (tag, "OPT_COST_TO_WIDTH", f"{cost_to_width:.3f}"))
            return None

        max_loss = limit * 100.0
        max_profit = (width - limit) * 100.0
        if max_profit <= 0:
            result.rejections.append((tag, "OPT_NO_PROFIT_POTENTIAL", "0"))
            return None

        reward_risk = reward_risk_from_cost_width(cost_to_width)
        breakeven = (long_leg.strike + limit
                     if strategy is StrategyType.BULL_CALL_DEBIT
                     else long_leg.strike - limit)

        short_open = short_leg.model_copy(update={
            "side": "SELL", "position_intent": "sell_to_open"})

        liq = min(leg_liquidity_score(long_leg, self.f.max_spread_pct,
                                      self.leg_weights),
                  leg_liquidity_score(short_open, self.f.max_spread_pct,
                                      self.leg_weights))
        dte = (long_leg.expiration - today).days
        dfit = delta_fit_score(long_leg.delta, short_leg.delta,
                               self.f.long_delta_target,
                               self.f.short_delta_target)
        tfit = dte_fit_score(dte)
        ceff = 100.0 * clip(1.0 - cost_to_width, 0.0, 1.0)
        w = self.structure_weights
        score = clip(w["liquidity"] * liq + w["reward_risk"] * rr_score(reward_risk)
                     + w["delta_fit"] * dfit + w["dte_fit"] * tfit
                     + w["cost_efficiency"] * ceff)

        try:
            return OptionStructure(
                structure_id=structure_id(chain.symbol, 1),
                symbol=chain.symbol, strategy=strategy, rank=1,
                expiration=long_leg.expiration, dte=max(1, dte),
                legs=[long_leg, short_open],
                width=width,
                net_delta=long_leg.delta - short_leg.delta,
                raw_mid_debit=raw_mid_debit,
                adjusted_mid_debit=adjusted_mid_debit,
                natural_debit=natural_debit,
                staleness_buffer=round(buffer, 4),
                initial_limit_debit=limit,
                cost_to_width_ratio=cost_to_width,
                max_loss_per_spread=max_loss,
                max_profit_per_spread=max_profit,
                reward_risk_ratio=max_profit / max_loss,
                breakeven=breakeven,
                max_quote_lag_seconds=max_lag,
                underlying_price=chain.underlying_price,
                underlying_move=(
                    chain.underlying_price - long_leg.underlying_price_at_quote
                    if long_leg.underlying_price_at_quote else None),
                stale_adjusted=was_adjusted,
                liquidity_score=liq, delta_fit_score=dfit,
                dte_fit_score=tfit, cost_efficiency_score=ceff,
                structure_score=score,
            )
        except Exception as exc:  # noqa: BLE001 - model validation is the gate
            result.rejections.append(
                (tag, "OPT_STRUCTURE_INVALID", str(exc)[:100]))
            return None

    def _diversify(self, built: Sequence[OptionStructure]
                   ) -> list[OptionStructure]:
        """Return the top N, preferring distinct strike pairs.

        Three near-identical structures give the PM no real choice, which
        makes the selection step theatre rather than a decision.
        """
        chosen: list[OptionStructure] = []
        seen_pairs: set[tuple[float, float, str]] = set()

        for s in built:
            # Expiration is part of the identity: the same strikes a week
            # apart are different trades, and deduping them away starved
            # the PM of real choices on thin chains.
            pair = (s.long_leg.strike, s.short_leg.strike,
                    s.expiration.isoformat())
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            chosen.append(s)
            if len(chosen) >= self.f.structures_returned:
                break

        if len(chosen) < self.f.min_distinct_strike_pairs:
            for s in built:
                if s not in chosen:
                    chosen.append(s)
                if len(chosen) >= self.f.structures_returned:
                    break

        for rank, s in enumerate(chosen, start=1):
            object.__setattr__(s, "rank", rank)
        return chosen
