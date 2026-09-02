"""
Alpha Council v2.4 - the funnel scanner.

Runs discovery -> Stage-0 -> PreScore -> options pre-screen -> final rank,
and records a FunnelSnapshot proving the funnel narrowed. Option chains are
fetched for at most 12 symbols; that cap is the entire reason 250-symbol
breadth is affordable.

Nothing in this module calls an LLM. The Council runs downstream of it.

Place at: alpha_council/quant/scanner.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.db.engine import Database
from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.discovery import FunnelSnapshot
from alpha_council.models.enums import CandidateTrack, Direction, GateStage
from alpha_council.models.trading import OptionStructure
from alpha_council.options_engine.chain import ChainFilters, ChainService
from alpha_council.options_engine.spreads import SpreadBuilder, SpreadFilters
from alpha_council.quant.discovery import DiscoveryService, Stage0Result
from alpha_council.quant.scoring import (
    IntelSummary,
    build_candidate,
    classify_track,
    rank_by_track,
    rank_for_prescreen,
    regime_factor,
    summarize_intel,
)
from alpha_council.utils.ids import candidate_id, rejection_id
from alpha_council.utils.time import iso_utc, utc_now


@dataclass(slots=True)
class ScanResult:
    scan_id: str
    as_of: datetime
    tier: int
    discovery_symbols: list[str] = field(default_factory=list)
    stage0: list[Stage0Result] = field(default_factory=list)
    prescored: list[CandidateFeatures] = field(default_factory=list)
    prescreened: list[CandidateFeatures] = field(default_factory=list)
    final: list[CandidateFeatures] = field(default_factory=list)
    structures: dict[str, list[OptionStructure]] = field(default_factory=dict)
    rejections: list[tuple[str, str, str, GateStage]] = field(default_factory=list)
    chain_fetches: int = 0
    # symbol -> the candidate_id actually written to candidate_scores.
    # candidate_id() carries a random suffix, so a second caller deriving
    # it independently gets a different value and the foreign key fails.
    candidate_ids: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> FunnelSnapshot:
        sources: dict[str, int] = {}
        for c in self.final:
            key = str(c.discovery_source)
            sources[key] = sources.get(key, 0) + 1
        return FunnelSnapshot(
            scan_id=self.scan_id, as_of=self.as_of,
            discovery_count=len(self.discovery_symbols),
            stage0_survivors=len(self.stage0),
            prescore_survivors=len(self.prescored),
            options_prescreened=len(self.prescreened),
            final_candidates=len(self.final),
            councils_started=0,
            event_track_count=sum(1 for c in self.final
                                  if c.track is CandidateTrack.EVENT),
            momentum_track_count=sum(1 for c in self.final
                                     if c.track is CandidateTrack.MOMENTUM),
            source_counts=sources,
        )

    def structures_for(self, symbol: str) -> list[OptionStructure]:
        return self.structures.get(symbol, [])


class FunnelScanner:
    def __init__(self, discovery: DiscoveryService, chains: ChainService,
                 db: Database, config: dict[str, Any]):
        self.discovery = discovery
        self.chains = chains
        self.db = db
        self.config = config

    def _tier_cfg(self, tier: int) -> dict[str, Any]:
        return self.config.get("tiers", {}).get(tier, {})

    async def run(self, scan_id: str, tier: int = 1,
                  intel_by_symbol: dict[str, IntelSummary] | None = None,
                  benchmark_return: float = 0.0,
                  data_confidence: dict[str, float] | None = None,
                  blocked_symbols: set[str] | None = None,
                  now: datetime | None = None) -> ScanResult:
        now = now or utc_now()
        intel_by_symbol = intel_by_symbol or {}
        data_confidence = data_confidence or {}
        blocked = blocked_symbols or set()

        disc_cfg = self.config.get("discovery", {})
        tier_cfg = self._tier_cfg(tier)
        result = ScanResult(scan_id=scan_id, as_of=now, tier=tier)

        # ---- discovery + stage 0 (cheap) --------------------------
        result.discovery_symbols = await self.discovery.refresh(now=now)
        result.stage0 = await self.discovery.stage0(
            result.discovery_symbols,
            top_n=int(disc_cfg.get("stage0_top_n", 30)), now=now)

        # ---- stage 1: prescore ------------------------------------
        for s in result.stage0:
            intel = intel_by_symbol.get(s.symbol, IntelSummary())
            direction = (Direction.BULLISH if s.direction > 0
                         else Direction.BEARISH)
            track = classify_track(intel, s.source, direction)

            # Previously restricted to MOMENTUM, which let an EVENT
            # candidate reach council with its catalyst pointing the
            # opposite way to its technical direction. UNH scanned BEARISH
            # on a bullish catalyst and the PM correctly refused to trade
            # against its own evidence. The check belongs on both tracks.
            if intel.contradicts(direction):
                result.rejections.append(
                    (s.symbol, "INTEL_CONTRADICTS_DIRECTION",
                     f"{intel.direction} vs {direction}", GateStage.PRESCORE))
                continue

            candidate = build_candidate(
                s, intel, track, as_of=now,
                data_confidence_factor=data_confidence.get(s.symbol, 1.0),
                regime=regime_factor(direction, benchmark_return,
                                     intel.has_material_catalyst,
                                     self.config.get("regime_factors")),
                event_risk=0.0 if s.symbol in blocked else 1.0,
                weights=self.config, tier=tier,
                config_version=self.config.get("config_version", "v2.4"),
            )
            result.prescored.append(candidate)

        pre_floor = float(tier_cfg.get("pre_score_floor", 62.0))
        top_n = int(disc_cfg.get("options_prescreen_top_n", 12))
        staged = rank_for_prescreen(result.prescored, pre_floor, top_n)
        for sym, gate, detail in staged.rejected:
            result.rejections.append((sym, gate, detail, GateStage.PRESCORE))

        # ---- stage 2: options pre-screen (expensive) --------------
        options_cfg = self.config.get("options", {})
        cfilters = ChainFilters.from_tier(tier_cfg, options_cfg)
        sfilters = SpreadFilters.from_config(tier_cfg, options_cfg)
        builder = SpreadBuilder(sfilters,
                                self.config.get("structure_weights"),
                                self.config.get("leg_liquidity_weights"))

        for candidate in staged.selected:
            stage0 = next(s for s in result.stage0
                          if s.symbol == candidate.symbol)
            spot = stage0.indicators.last_price
            chain = await self.chains.fetch(candidate.symbol, spot, cfilters,
                                            now=now)
            result.chain_fetches += 1

            if chain.usable < 2:
                result.rejections.append(
                    (candidate.symbol, "OPT_CHAIN_UNUSABLE",
                     f"{chain.usable} legs", GateStage.OPTIONS_CHAIN))
                continue

            spreads = builder.build(chain, candidate.direction, now=now)
            if not spreads.structures:
                top_gate = max(spreads.rejection_counts().items(),
                               key=lambda kv: kv[1], default=("NONE", 0))[0]
                result.rejections.append(
                    (candidate.symbol, "OPT_NO_STRUCTURE", top_gate,
                     GateStage.OPTIONS_STRUCTURE))
                continue

            result.structures[candidate.symbol] = spreads.structures
            scored = build_candidate(
                stage0,
                IntelSummary() if candidate.track is CandidateTrack.MOMENTUM
                else intel_by_symbol.get(candidate.symbol, IntelSummary()),
                candidate.track, as_of=now,
                data_confidence_factor=candidate.data_confidence_factor,
                regime=candidate.regime_factor,
                event_risk=candidate.event_risk_factor,
                options_opportunity=spreads.options_opportunity_score,
                options_liquidity=spreads.options_liquidity_score,
                weights=self.config, tier=tier,
                config_version=candidate.config_version,
            )
            result.prescreened.append(scored)

        # ---- stage 3: final ranking -------------------------------
        final_floor = float(tier_cfg.get("final_score_floor", 68.0))
        event_floor = float(
            tier_cfg.get("final_score_floor_event", final_floor))
        tracks_cfg = self.config.get("tracks", {})
        ranked = rank_by_track(
            result.prescreened,
            {"MOMENTUM": final_floor, "EVENT": event_floor},
            total=int(disc_cfg.get("final_candidate_top_n", 5)),
            quota=tracks_cfg.get("final_quota", {"EVENT": 3, "MOMENTUM": 2}),
            backfill=bool(tracks_cfg.get("backfill_across_tracks", True)),
        )
        result.final = ranked.selected
        for sym, gate, detail in ranked.rejected:
            result.rejections.append(
                (sym, gate, detail, GateStage.OPPORTUNITY_SCORE))

        return result

    # ---- persistence ---------------------------------------------

    async def persist(self, result: ScanResult) -> None:
        snapshot = result.snapshot()

        await self.db.execute(
            "INSERT OR REPLACE INTO scan_runs("
            "scan_id, mode, config_version, started_at, completed_at, "
            "universe_size, candidate_count, status) VALUES(?,?,?,?,?,?,?,?)",
            (result.scan_id, "FULL",
             self.config.get("config_version", "v2.4"),
             iso_utc(result.as_of), iso_utc(),
             len(result.discovery_symbols), len(result.final), "COMPLETE"))

        rows = []
        for c in result.final + [x for x in result.prescreened
                                 if x not in result.final]:
            cid = result.candidate_ids.setdefault(
                c.symbol, candidate_id(result.scan_id, c.symbol))
            rows.append((
                cid, result.scan_id,
                c.config_version, c.symbol, str(c.direction), iso_utc(c.as_of),
                c.momentum_score, c.relative_volume_score,
                c.trend_regime_score, c.relative_strength_score,
                c.options_opportunity_score, c.options_liquidity_score,
                c.catalyst_score or 0.0, c.corroboration_score or 0.0,
                c.novelty_score or 0.0, c.data_confidence_factor,
                c.regime_factor, c.event_risk_factor, c.pre_score,
                c.raw_opportunity_score, c.final_opportunity_score,
                json.dumps(c.key_metrics, default=str), iso_utc(),
                str(c.discovery_source), str(c.track),
                c.fast_score,
            ))
        await self.db.executemany(
            "INSERT OR IGNORE INTO candidate_scores("
            "candidate_id, scan_id, config_version, symbol, direction, as_of, "
            "momentum_score, relative_volume_score, trend_regime_score, "
            "relative_strength_score, options_opportunity_score, "
            "options_liquidity_score, catalyst_score, corroboration_score, "
            "novelty_score, data_confidence_factor, regime_factor, "
            "event_risk_factor, pre_score, raw_opportunity_score, "
            "final_opportunity_score, key_metrics_json, created_at, "
            "discovery_source, candidate_track, fast_score) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

        await self.db.execute(
            "INSERT OR REPLACE INTO funnel_snapshots("
            "scan_id, as_of, discovery_count, stage0_survivors, "
            "prescore_survivors, options_prescreened, final_candidates, "
            "councils_started, event_track_count, momentum_track_count, "
            "source_counts_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot.scan_id, iso_utc(snapshot.as_of),
             snapshot.discovery_count, snapshot.stage0_survivors,
             snapshot.prescore_survivors, snapshot.options_prescreened,
             snapshot.final_candidates, snapshot.councils_started,
             snapshot.event_track_count, snapshot.momentum_track_count,
             json.dumps(snapshot.source_counts)))

        if result.rejections:
            await self.db.executemany(
                "INSERT INTO gate_rejections("
                "rejection_id, occurred_at, config_version, scan_id, "
                "decision_id, symbol, direction, stage, gate_id, "
                "observed_value, threshold_value, tier, hard_gate, "
                "shadow_eligible, shadow_structure_json, note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rejection_id(), iso_utc(), self.config.get(
                    "config_version", "v2.4"), result.scan_id, None, sym,
                  "NEUTRAL", str(stage), gate, str(detail), None,
                  result.tier, 0, 0, None, "")
                 for sym, gate, detail, stage in result.rejections])

        await self.discovery.persist(result.scan_id, result.discovery_symbols,
                                     result.stage0)
        await self.discovery.persist_rejections(
            result.scan_id, self.config.get("config_version", "v2.4"),
            result.tier)
