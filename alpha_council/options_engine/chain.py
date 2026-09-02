"""
Alpha Council v2.4 - option chain acquisition and normalization.

Turns raw Indicative-feed snapshots into validated OptionLeg objects. Three
things happen here that the rest of the system depends on:

  1. Invalid quotes are rejected before any midpoint is computed.
  2. Stale quotes are delta-adjusted against underlying movement (§5.4),
     with the underlying price read from stored bars at the quote's own
     timestamp rather than guessed.
  3. Every rejection is attributed to a named gate, so the options funnel
     is as measurable as every other stage.

Indicative quotes are derived, not OPRA NBBO. A fresh timestamp does not
make one executable, which is why §17.5 measures the fill bias separately.

Place at: alpha_council/options_engine/chain.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.alpaca.rest_client import AlpacaRestClient
from alpha_council.models.trading import OptionLeg
from alpha_council.utils.math import delta_adjust, safe_mid, spread_pct
from alpha_council.utils.time import age_seconds, parse_alpaca_ts, to_et, utc_now


@dataclass(slots=True)
class ChainFilters:
    """Tier-driven per-leg hard filters (§13.1)."""

    dte_min: int = 7
    dte_max: int = 21
    min_open_interest: int = 250
    min_volume: int = 25
    max_spread_pct: float = 0.15
    fresh_quote_seconds: float = 60.0
    max_quote_lag_seconds: float = 1200.0
    max_underlying_drift_pct: float = 0.010
    require_greeks: bool = True

    @classmethod
    def from_tier(cls, tier_cfg: dict[str, Any],
                  options_cfg: dict[str, Any]) -> "ChainFilters":
        dte = tier_cfg.get("dte", [7, 21])
        return cls(
            dte_min=int(dte[0]), dte_max=int(dte[1]),
            min_open_interest=int(tier_cfg.get("min_open_interest", 250)),
            min_volume=int(tier_cfg.get("min_volume", 25)),
            max_spread_pct=float(tier_cfg.get("max_leg_spread_pct", 0.15)),
            fresh_quote_seconds=float(options_cfg.get("fresh_quote_seconds", 60)),
            max_quote_lag_seconds=float(
                options_cfg.get("max_quote_lag_seconds", 1200)),
            max_underlying_drift_pct=float(
                options_cfg.get("max_underlying_drift_pct", 0.010)),
        )


@dataclass(slots=True)
class ChainResult:
    symbol: str
    underlying_price: float
    fetched_at: datetime
    calls: list[OptionLeg] = field(default_factory=list)
    puts: list[OptionLeg] = field(default_factory=list)
    rejections: list[tuple[str, str, str]] = field(default_factory=list)
    contracts_seen: int = 0
    max_quote_lag: float = 0.0
    any_stale_adjusted: bool = False

    def legs_for(self, option_type: str) -> list[OptionLeg]:
        return self.calls if option_type.upper() == "CALL" else self.puts

    @property
    def usable(self) -> int:
        return len(self.calls) + len(self.puts)

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, gate, _ in self.rejections:
            counts[gate] = counts.get(gate, 0) + 1
        return counts


def parse_occ_symbol(occ: str) -> tuple[str, date, str, float] | None:
    """Decode an OCC symbol: SPY260918C00750000.

    Underlying is variable length, then YYMMDD, then C/P, then a strike in
    thousandths on 8 digits.
    """
    if len(occ) < 16:
        return None
    tail = occ[-15:]
    underlying = occ[: len(occ) - 15]
    try:
        expiry = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        opt_type = "CALL" if tail[6].upper() == "C" else "PUT"
        strike = int(tail[7:15]) / 1000.0
    except (ValueError, IndexError):
        return None
    if not underlying or strike <= 0:
        return None
    return underlying.upper(), expiry, opt_type, strike


class ChainService:
    """Fetches and normalizes option chains. One cache per underlying."""

    # Open interest is prior-session information; refetching it with every
    # 60-second chain cycle would spend requests on a number that changes
    # once a day.
    OI_CACHE_SECONDS = 900

    def __init__(self, api: AlpacaRestClient, market: MarketDataService,
                 cache_seconds: int = 60):
        self.api = api
        self.market = market
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        # symbol -> (fetched_at, occ -> (open_interest, oi_date) | None)
        # None means the contracts endpoint was unavailable: degraded, the
        # OI gate stands down for that fetch rather than zeroing the chain.
        self._oi_cache: dict[str, tuple[datetime,
                                        dict[str, tuple[int, str | None]]
                                        | None]] = {}
        self.fetches = 0
        self.cache_hits = 0

    async def _raw_chain(self, symbol: str, filters: ChainFilters,
                         now: datetime) -> dict[str, Any]:
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < self.cache_seconds:
            self.cache_hits += 1
            return cached[1]

        today = to_et(now).date()
        payload = await self.api.get_option_chain(
            symbol,
            expiration_gte=(today + timedelta(days=filters.dte_min)).isoformat(),
            expiration_lte=(today + timedelta(days=filters.dte_max)).isoformat(),
        )
        self.fetches += 1
        self._cache[symbol] = (now, payload)
        return payload

    async def _open_interest_map(self, symbol: str, filters: ChainFilters,
                                 now: datetime
                                 ) -> dict[str, tuple[int, str | None]] | None:
        """OCC symbol -> (open_interest, open_interest_date).

        Measured 2026-09-02 09:56 ET on the live account: the Indicative
        market-data snapshots carry NO open-interest field at all (12,746
        SPY contracts, zero with an OI key), so the §13.1 OI gate can only
        be evaluated from the Trading API contracts endpoint — which is
        the source §5.2's probe confirmed OI from in the first place.

        Returns None when the endpoint is unavailable: the OI gate stands
        down for that fetch (logged as degradation) instead of rejecting
        every contract on a data outage.
        """
        cached = self._oi_cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < self.OI_CACHE_SECONDS:
            return cached[1]

        today = to_et(now).date()
        try:
            contracts = await self.api.get_option_contracts(
                symbol,
                expiration_gte=(today + timedelta(days=filters.dte_min)
                                ).isoformat(),
                expiration_lte=(today + timedelta(days=filters.dte_max)
                                ).isoformat(),
            )
            oi_map: dict[str, tuple[int, str | None]] = {}
            for contract in contracts:
                occ = str(contract.get("symbol") or "")
                if not occ:
                    continue
                raw_oi = contract.get("open_interest")
                oi_map[occ] = (
                    int(raw_oi) if raw_oi is not None else 0,
                    contract.get("open_interest_date"),
                )
        except Exception as exc:  # noqa: BLE001 - degrade, never zero the chain
            self._oi_cache[symbol] = (now, None)
            try:
                await self.market.db.log_event(
                    "WARN", "chain", "CHAIN_OI_UNAVAILABLE",
                    f"{symbol}: contracts endpoint failed; OI gate stands "
                    f"down this fetch: {exc}"[:220])
            except Exception:  # noqa: BLE001
                pass
            return None

        self._oi_cache[symbol] = (now, oi_map)
        return oi_map

    async def fetch(self, symbol: str, underlying_price: float,
                    filters: ChainFilters,
                    now: datetime | None = None) -> ChainResult:
        now = now or utc_now()
        result = ChainResult(symbol=symbol, underlying_price=underlying_price,
                             fetched_at=now)

        raw = await self._raw_chain(symbol, filters, now)
        oi_map = await self._open_interest_map(symbol, filters, now)
        result.contracts_seen = len(raw)
        today = to_et(now).date()

        for occ, snap in raw.items():
            parsed = parse_occ_symbol(occ)
            if parsed is None:
                result.rejections.append((occ, "OPT_SYMBOL_UNPARSEABLE", occ))
                continue
            underlying, expiry, opt_type, strike = parsed

            dte = (expiry - today).days
            if dte < filters.dte_min or dte > filters.dte_max:
                result.rejections.append((occ, "OPT_DTE_OUT_OF_WINDOW", str(dte)))
                continue
            if dte < 3:
                result.rejections.append((occ, "OPT_DTE_TOO_SHORT", str(dte)))
                continue

            leg = await self._build_leg(
                occ, symbol, expiry, opt_type, strike, snap,
                underlying_price, filters, now, result, oi_map)
            if leg is None:
                continue

            (result.calls if opt_type == "CALL" else result.puts).append(leg)
            result.max_quote_lag = max(result.max_quote_lag,
                                       leg.quote_lag_seconds)

        result.calls.sort(key=lambda leg: leg.strike)
        result.puts.sort(key=lambda leg: leg.strike)
        return result

    async def _build_leg(self, occ: str, underlying: str, expiry: date,
                         opt_type: str, strike: float, snap: dict[str, Any],
                         underlying_price: float, filters: ChainFilters,
                         now: datetime, result: ChainResult,
                         oi_map: dict[str, tuple[int, str | None]]
                         | None = None) -> OptionLeg | None:
        quote = snap.get("latestQuote") or {}
        greeks = snap.get("greeks") or {}
        trade = snap.get("latestTrade") or {}

        bid, ask = quote.get("bp"), quote.get("ap")
        raw_mid = safe_mid(bid, ask)
        if raw_mid is None:
            result.rejections.append(
                (occ, "OPT_QUOTE_INVALID", f"bid={bid} ask={ask}"))
            return None

        delta = greeks.get("delta")
        if filters.require_greeks and delta is None:
            result.rejections.append((occ, "OPT_GREEKS_MISSING", "no delta"))
            return None

        quote_ts = parse_alpaca_ts(quote.get("t"))
        lag = age_seconds(quote_ts, now) or 0.0
        if lag > filters.max_quote_lag_seconds:
            result.rejections.append((occ, "OPT_QUOTE_BLOCKED", f"{lag:.0f}s"))
            return None

        sp = spread_pct(bid, ask) or 1.0
        if sp > filters.max_spread_pct:
            result.rejections.append((occ, "OPT_SPREAD_TOO_WIDE", f"{sp:.3f}"))
            return None

        # OI source of truth: snapshot fields when present (they are not,
        # on the Indicative feed — measured live 2026-09-02), else the
        # contracts-endpoint map. Only when the map itself is unavailable
        # does the gate stand down (degradation, logged once per fetch).
        oi = snap.get("openInterest") or snap.get("open_interest")
        oi_date = snap.get("openInterestDate") or snap.get("open_interest_date")
        if oi is None and oi_map is not None:
            entry = oi_map.get(occ)
            if entry is not None:
                oi, oi_date = entry
        if filters.min_open_interest > 0 and oi_map is not None:
            # A missing OI with the authoritative source available is not
            # evidence of liquidity — it usually means a brand-new listing.
            if oi is None:
                result.rejections.append((occ, "OPT_OI_MISSING", "absent"))
                return None
            if int(oi) < filters.min_open_interest:
                result.rejections.append((occ, "OPT_OI_TOO_LOW", str(oi)))
                return None

        vol = (snap.get("dailyBar") or {}).get("v") or trade.get("s") or 0
        if filters.min_volume > 0 and int(vol or 0) < filters.min_volume:
            result.rejections.append((occ, "OPT_VOLUME_TOO_LOW", str(vol)))
            return None

        # §5.4 delta adjustment. Only engages once the quote is stale, and
        # only when the underlying price at the quote's own timestamp is
        # actually available. No interpolation, no fallback to spot.
        adjusted_mid = raw_mid
        underlying_at_quote: float | None = None
        if lag > filters.fresh_quote_seconds and quote_ts is not None:
            underlying_at_quote = await self.market.underlying_at(
                underlying, quote_ts)
            if underlying_at_quote is None:
                result.rejections.append(
                    (occ, "OPT_NO_UNDERLYING_REFERENCE", f"{lag:.0f}s stale"))
                return None
            move = underlying_price - underlying_at_quote
            drift = abs(move / underlying_price) if underlying_price else 1.0
            if drift > filters.max_underlying_drift_pct:
                result.rejections.append(
                    (occ, "OPT_STALE_DRIFT", f"{drift:.4f}"))
                return None
            adjusted_mid = delta_adjust(raw_mid, float(delta or 0.0), move)
            result.any_stale_adjusted = True

        try:
            return OptionLeg(
                symbol=occ, underlying=underlying, expiration=expiry,
                option_type=opt_type, strike=strike,
                side="BUY", position_intent="buy_to_open",
                bid=float(bid), ask=float(ask),
                raw_mid=raw_mid, adjusted_mid=adjusted_mid,
                quote_lag_seconds=lag,
                underlying_price_at_quote=underlying_at_quote,
                delta=float(delta) if delta is not None else 0.0,
                gamma=greeks.get("gamma"), theta=greeks.get("theta"),
                vega=greeks.get("vega"),
                implied_volatility=snap.get("impliedVolatility"),
                open_interest=int(oi) if oi is not None else None,
                open_interest_date=(date.fromisoformat(oi_date)
                                    if isinstance(oi_date, str) else None),
                volume=int(vol) if vol else None,
                quote_timestamp=quote_ts,
            )
        except Exception as exc:  # noqa: BLE001 - model validation is the gate
            result.rejections.append((occ, "OPT_LEG_INVALID", str(exc)[:80]))
            return None

    def stats(self) -> dict[str, int]:
        return {"chain_fetches": self.fetches, "cache_hits": self.cache_hits}
