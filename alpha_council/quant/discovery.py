"""
Alpha Council v2.4 - dynamic discovery and the Stage-0 fast screen.

The funnel: 250 -> 30 -> 12 -> 5 -> at most 3 councils. This module owns
the first two arrows. Two rules are absolute here:

  * Stage 0 fetches NO option chains and calls NO LLM. Breadth is only
    affordable because the wide end of the funnel is cheap.
  * Optional sources fail open. A 403 from a screener costs breadth, never
    a scan.

Place at: alpha_council/quant/discovery.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.alpaca.screeners import (
    AssetCatalog,
    ScreenerService,
    is_blocked_symbol,
)
from alpha_council.db.engine import Database
from alpha_council.models.discovery import DiscoveryCandidate
from alpha_council.models.enums import DiscoverySource, GateStage
from alpha_council.quant import indicators as ind
from alpha_council.utils.ids import discovery_id, rejection_id
from alpha_council.utils.math import (
    fast_score,
    momentum_score,
    relative_strength_score,
    relative_volume_score,
    technical_direction,
    trend_regime_score,
)
from alpha_council.utils.time import iso_utc, utc_now

DEFAULT_TTL_MINUTES = 90
DEFAULT_CAP = 250
DEFAULT_STAGE0_TOP_N = 30


@dataclass(slots=True)
class Injection:
    """A symbol proposed by a non-core source, with its justification."""

    symbol: str
    source: DiscoverySource
    reason: str
    rank: int | None = None
    discovered_at: datetime | None = None


@dataclass(slots=True)
class Stage0Result:
    symbol: str
    fast_score: float
    direction: int
    combined_direction: float
    momentum: float
    relative_volume: float
    relative_strength: float
    trend_regime: float
    discovery_boost: float
    source: DiscoverySource
    indicators: ind.IndicatorSet
    metrics: dict[str, Any] = field(default_factory=dict)


class UniverseManager:
    """Owns Core, screener-injected, and event-injected membership.

    Dynamic membership expires. Core does not. The cap is enforced
    deterministically by priority so a screener burst can never push Core
    symbols out of the scan.
    """

    def __init__(self, core: Sequence[str], *, cap: int = DEFAULT_CAP,
                 ttl_minutes: int = DEFAULT_TTL_MINUTES,
                 exclusions: Iterable[str] = (),
                 name_lookup: Any = None):
        # name_lookup(symbol) -> str. Warrant/unit/preferred detection needs
        # the instrument name; ticker shape alone matched LOW, NOW and SNOW.
        self.name_lookup = name_lookup
        self.core = [s.upper() for s in core]
        self.core_set = set(self.core)
        self.cap = cap
        self.ttl_minutes = ttl_minutes
        self.exclusions = {s.upper() for s in exclusions}
        self._injected: dict[str, Injection] = {}
        self._expiry: dict[str, datetime] = {}
        self.rejected: list[tuple[str, str, str]] = []   # symbol, gate, detail

    # ---- membership -----------------------------------------------

    def inject(self, injection: Injection, now: datetime | None = None) -> bool:
        """Add or refresh a dynamic symbol. Returns False if not admitted."""
        now = now or utc_now()
        sym = injection.symbol.upper()

        if sym in self.exclusions:
            self.rejected.append((sym, "DISC_EXCLUDED", "permanent exclusion list"))
            return False
        if sym in self.core_set:
            return False        # already permanent; nothing to do

        name = ""
        if self.name_lookup is not None:
            try:
                name = self.name_lookup(sym) or ""
            except Exception:  # noqa: BLE001 - hygiene must never break a scan
                name = ""
        blocked = is_blocked_symbol(sym, name)
        if blocked:
            detail = f"{blocked}: {name[:60]}" if name else blocked
            self.rejected.append((sym, "DISC_BLOCKED_CLASS", detail))
            return False

        existing = self._injected.get(sym)
        if existing is None or self._boost_of(injection.source) > self._boost_of(
                existing.source):
            self._injected[sym] = injection
        self._expiry[sym] = DiscoveryCandidate.ttl_expiry(now, self.ttl_minutes)
        return True

    def expire(self, now: datetime | None = None) -> list[str]:
        now = now or utc_now()
        gone = [s for s, exp in self._expiry.items() if now >= exp]
        for s in gone:
            self._injected.pop(s, None)
            self._expiry.pop(s, None)
        return gone

    def members(self, now: datetime | None = None) -> list[str]:
        self.expire(now)
        return self.core + [s for s in self._injected if s not in self.core_set]

    def source_of(self, symbol: str) -> DiscoverySource:
        sym = symbol.upper()
        if sym in self.core_set:
            return DiscoverySource.CORE
        inj = self._injected.get(sym)
        return inj.source if inj else DiscoverySource.OTHER_DYNAMIC

    def reason_of(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym in self.core_set:
            return "core universe"
        inj = self._injected.get(sym)
        return inj.reason if inj else "dynamic discovery"

    def expiry_of(self, symbol: str) -> datetime | None:
        return self._expiry.get(symbol.upper())

    @staticmethod
    def _boost_of(source: DiscoverySource) -> float:
        return {
            DiscoverySource.SEC_EVENT: 100.0,
            DiscoverySource.ALPACA_NEWS: 100.0,
            DiscoverySource.MOVER: 80.0,
            DiscoverySource.MOST_ACTIVE: 80.0,
            DiscoverySource.OTHER_DYNAMIC: 50.0,
            DiscoverySource.CORE: 40.0,
        }.get(source, 50.0)

    # ---- cap ------------------------------------------------------

    def cap_members(self, symbols: Sequence[str],
                    density: dict[str, int] | None = None) -> list[str]:
        """Deterministic retention order when over the cap (§12.1).

        Core first, then fresh event injections, then screener rank, then
        data density. A screener burst can never evict Core.
        """
        if len(symbols) <= self.cap:
            return list(symbols)

        density = density or {}
        core = [s for s in symbols if s in self.core_set]
        dynamic = [s for s in symbols if s not in self.core_set]

        def sort_key(sym: str) -> tuple[int, int, int]:
            inj = self._injected.get(sym)
            source = inj.source if inj else DiscoverySource.OTHER_DYNAMIC
            tier = 0 if source.is_event_bearing else (
                1 if source in (DiscoverySource.MOVER,
                                DiscoverySource.MOST_ACTIVE) else 2)
            rank = inj.rank if (inj and inj.rank) else 9999
            return (tier, rank, -density.get(sym, 0))

        dynamic.sort(key=sort_key)
        room = max(0, self.cap - len(core))
        dropped = dynamic[room:]
        for s in dropped:
            self.rejected.append((s, "DISC_CAP_EXCEEDED",
                                  f"universe cap {self.cap} reached"))
        return core + dynamic[:room]


class DiscoveryService:
    """Assembles the discovery universe and runs the Stage-0 fast screen."""

    def __init__(self, market: MarketDataService, catalog: AssetCatalog,
                 screeners: ScreenerService, universe: UniverseManager,
                 db: Database, config: dict[str, Any] | None = None):
        self.market = market
        self.catalog = catalog
        self.screeners = screeners
        self.universe = universe
        self.db = db
        self.config = config or {}
        disc = (config or {}).get("discovery", {})
        self.backfill_limit = int(disc.get("backfill_new_symbols_per_scan", 40))
        self.backfill_sessions = int(disc.get("backfill_sessions", 20))
        self.last_backfill: dict[str, int] = {}

    # ---- universe assembly ----------------------------------------

    async def refresh(self, news_symbols: Sequence[Injection] = (),
                      now: datetime | None = None) -> list[str]:
        """Core + optional screeners + event injections, filtered and capped."""
        now = now or utc_now()
        disc_cfg = self.config.get("discovery", {})

        for inj in news_symbols:
            self.universe.inject(inj, now)

        if disc_cfg.get("enable_most_active", True):
            res = await self.screeners.most_actives(
                top=disc_cfg.get("most_active_top", 100))
            for sym, rank in res.symbols:
                self.universe.inject(Injection(
                    symbol=sym, source=DiscoverySource.MOST_ACTIVE,
                    reason=f"most-active rank {rank}", rank=rank), now)

        if disc_cfg.get("enable_movers", True):
            res = await self.screeners.movers(
                top=disc_cfg.get("movers_top", 50))
            for sym, rank in res.symbols:
                self.universe.inject(Injection(
                    symbol=sym, source=DiscoverySource.MOVER,
                    reason=f"market mover rank {rank}", rank=rank), now)

        candidates = self.universe.members(now)
        eligible = await self._filter_eligible(candidates)

        # Screener symbols arrive mid-session with no stored history, so they
        # would fail the Stage-0 sufficiency check and the breadth would be
        # decorative. Backfill them before ranking. Capped per scan because a
        # 200-symbol cold start would blow the request budget.
        newcomers = [s for s in eligible if s not in self.universe.core_set]
        if newcomers and self.backfill_limit > 0:
            self.last_backfill = await self.market.backfill_missing(
                newcomers[: self.backfill_limit],
                sessions=self.backfill_sessions,
            )

        density = {s: (await self.market.bar_coverage(s))["bars"]
                   for s in eligible if s not in self.universe.core_set}
        return self.universe.cap_members(eligible, density)

    async def _filter_eligible(self, symbols: Sequence[str]) -> list[str]:
        """Asset eligibility. Core is exempt from the has_options check only
        if the catalog failed to load, so a catalog outage degrades to Core
        rather than to nothing."""
        catalog_ok = self.catalog.size > 0
        out = []
        for sym in symbols:
            if sym in self.universe.core_set and not catalog_ok:
                out.append(sym)
                continue
            if not catalog_ok:
                continue
            if self.catalog.is_eligible(sym, require_options=True):
                out.append(sym)
            else:
                asset = self.catalog.get(sym)
                detail = ("unknown symbol" if asset is None
                          else f"tradable={asset.tradable} "
                               f"options={asset.has_options} "
                               f"status={asset.status}")
                self.universe.rejected.append((sym, "DISC_NOT_ELIGIBLE", detail))
        return out

    # ---- stage 0 --------------------------------------------------

    async def stage0(self, symbols: Sequence[str], benchmark: str = "SPY",
                     top_n: int = DEFAULT_STAGE0_TOP_N,
                     now: datetime | None = None) -> list[Stage0Result]:
        """Cheap deterministic screen over the whole discovery universe.

        Batch equity snapshots and cached bars only. No option chains, no
        LLM calls, by construction: this method never touches the options
        client and never imports an agent.
        """
        now = now or utc_now()
        eq_cfg = self.config.get("equity", {})
        ambiguity_floor = self.config.get(
            "tiers", {}).get(1, {}).get("direction_ambiguity_floor", 0.15)

        bench_bars = await self.market.load_bars(benchmark, limit=400)
        bench = ind.compute(benchmark, bench_bars, now=now)
        if bench is None:
            return []

        results: list[Stage0Result] = []
        for sym in symbols:
            bars = await self.market.load_bars(sym, limit=400)
            metrics = ind.compute(sym, bars, now=now)
            if metrics is None or not metrics.sufficient_history:
                self.universe.rejected.append(
                    (sym, "DISC_INSUFFICIENT_HISTORY",
                     f"{metrics.bars_used if metrics else 0} bars"))
                continue

            rvol = await self.market.rvol(sym, now=now)
            if rvol is not None:
                metrics.rvol = rvol

            rs15, rs60 = ind.relative_strength(metrics, bench)
            direction_signal = technical_direction(
                metrics.r5, metrics.r15, metrics.r60, rs15, rs60,
                metrics.above_vwap, metrics.ema_aligned_bullish,
                metrics.above_day_open,
                ind.benchmark_aligned(1 if metrics.r15 >= 0 else -1, bench),
            )
            if abs(direction_signal) < ambiguity_floor:
                self.universe.rejected.append(
                    (sym, "DIR_AMBIGUOUS", f"{direction_signal:+.3f}"))
                continue

            d = 1 if direction_signal > 0 else -1
            mom = momentum_score(d, metrics.r5, metrics.r15, metrics.r60)
            rvol_score = relative_volume_score(rvol) if rvol is not None else 40.0
            rs_score = relative_strength_score(d, rs15, rs60)
            trend = trend_regime_score(
                metrics.above_vwap, metrics.ema_aligned_bullish,
                metrics.above_day_open,
                ind.benchmark_aligned(d, bench), bearish=(d < 0))

            source = self.universe.source_of(sym)
            boost = UniverseManager._boost_of(source)
            score = fast_score(mom, rvol_score, rs_score, trend, boost,
                               self.config.get("fast_score_weights"))

            results.append(Stage0Result(
                symbol=sym, fast_score=score, direction=d,
                combined_direction=direction_signal, momentum=mom,
                relative_volume=rvol_score, relative_strength=rs_score,
                trend_regime=trend, discovery_boost=boost, source=source,
                indicators=metrics,
                metrics={"rvol": rvol, "rs15": rs15, "rs60": rs60,
                         "data_gaps": metrics.data_gaps},
            ))

        results.sort(key=lambda r: r.fast_score, reverse=True)
        for r in results[top_n:]:
            self.universe.rejected.append(
                (r.symbol, "STAGE0_CUT", f"fast_score {r.fast_score:.1f}"))
        return results[:top_n]

    # ---- persistence ----------------------------------------------

    async def persist(self, scan_id: str, symbols: Sequence[str],
                      stage0: Sequence[Stage0Result],
                      now: datetime | None = None) -> None:
        now = now or utc_now()
        scored = {r.symbol: r.fast_score for r in stage0}
        rows = []
        for sym in symbols:
            source = self.universe.source_of(sym)
            is_core = sym in self.universe.core_set
            expiry = self.universe.expiry_of(sym)
            coverage = await self.market.bar_coverage(sym)
            rows.append((
                discovery_id(scan_id, sym, str(source)), scan_id, sym,
                iso_utc(now), iso_utc(expiry) if expiry else None,
                str(source), None, self.universe.reason_of(sym),
                1 if is_core else 0, 1, 1,
                1 if coverage["bars"] >= 40 else 0,
                scored.get(sym, 0.0), UniverseManager._boost_of(source),
            ))
        await self.db.executemany(
            "INSERT OR IGNORE INTO discovery_candidates("
            "discovery_id, scan_id, symbol, discovered_at, expires_at, source, "
            "source_rank, discovery_reason, is_core, asset_tradable, "
            "has_options, data_density_ok, fast_score, discovery_boost) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    async def persist_rejections(self, scan_id: str, config_version: str,
                                 tier: int = 1) -> int:
        """Every discovery-stage rejection becomes a measurable row."""
        if not self.universe.rejected:
            return 0
        rows = []
        for sym, gate, detail in self.universe.rejected:
            stage = (GateStage.STAGE0 if gate in ("STAGE0_CUT", "DIR_AMBIGUOUS")
                     else GateStage.DISCOVERY)
            rows.append((
                rejection_id(), iso_utc(), config_version, scan_id, None,
                sym, "NEUTRAL", str(stage), gate, str(detail), None,
                tier, 0, 0, None, "",
            ))
        await self.db.executemany(
            "INSERT INTO gate_rejections("
            "rejection_id, occurred_at, config_version, scan_id, decision_id, "
            "symbol, direction, stage, gate_id, observed_value, "
            "threshold_value, tier, hard_gate, shadow_eligible, "
            "shadow_structure_json, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        n = len(rows)
        self.universe.rejected.clear()
        return n