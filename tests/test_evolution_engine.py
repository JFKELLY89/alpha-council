"""
Alpha Council v2.5 - Champion/Challenger engine tests.

The named cases from the v2.5 §27 test plan: the change validator as the
safety boundary, the registry's structural rules, deterministic promotion,
and the shadow runner's re-scoring. Everything here runs without a network
or an LLM — the engine is deterministic by design and the tests prove it.

Run:
    uv run pytest tests/test_evolution_engine.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError

from alpha_council.db.engine import Database
from alpha_council.evolution.champion import ChampionRegistry, PromotionRefused
from alpha_council.evolution.change_validator import (
    apply_changes,
    resolve_path,
    validate_changes,
)
from alpha_council.evolution.performance import (
    challenger_performance,
    champion_performance,
)
from alpha_council.evolution.promotion import recommend
from alpha_council.evolution.shadow_runner import ShadowRunner
from alpha_council.models.evolution import (
    ChallengerProposal,
    ChangeCategory,
    EvolutionDecision,
    ParameterChange,
    PromotionRecommendation,
    StrategyPerformance,
)

NOW = datetime(2026, 9, 1, 20, 30, tzinfo=timezone.utc)

CHAMPION_CONFIG = {
    "hard": {"max_risk_per_trade_pct": 2.0},
    "paper_only": True,
    "liquidity_floor": {"min_volume": 5},
    "tiers": {1: {"pre_score_floor": 58.0, "final_score_floor": 62.0,
                  "min_volume": 25, "max_councils_per_scan": 3}},
    "tracks": {"final_quota": {"EVENT": 3, "MOMENTUM": 2}},
    "discovery": {"final_candidate_top_n": 5},
    "opportunity_weights_momentum": {"momentum": 0.22,
                                     "relative_volume": 0.22,
                                     "trend_regime": 0.14,
                                     "relative_strength": 0.14,
                                     "options_opportunity": 0.14,
                                     "options_liquidity": 0.14},
    "pre_score_weights_momentum": {"momentum": 0.30, "relative_volume": 0.30,
                                   "trend_regime": 0.20,
                                   "relative_strength": 0.20},
}

EVOLUTION_CFG = {
    "max_changes_per_challenger": 3,
    "hysteresis": {"max_threshold_change_per_version_pct": 10.0},
    "immutable_paths": ["hard.", "paper_only", "liquidity_floor.",
                        "budget.", "rate_limits.", "activity_target.",
                        "risk.", "execution.", "liquidity."],
    "competition": {"min_observations_to_recommend_promotion": 12,
                    "min_sessions_to_recommend_promotion": 2},
}


def _change(path: str, old, new,
            category: ChangeCategory = ChangeCategory.QUALITY_THRESHOLD
            ) -> ParameterChange:
    return ParameterChange(category=category, parameter_path=path,
                           champion_value=old, challenger_value=new,
                           rationale="test rationale for this change")


def _proposal(*changes: ParameterChange) -> ChallengerProposal:
    return ChallengerProposal(
        challenger_id="alpha_v2_5_c1", parent_champion_id="alpha_v2_5_c0",
        created_at=NOW, hypothesis="a small bounded hypothesis for testing",
        evidence_summary=["eight observations of the pattern"],
        changes=list(changes),
        expected_benefit="slightly better candidate flow",
        expected_failure_mode="more marginal candidates reach council",
        minimum_shadow_observations=8, confidence="LOW")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.apply_schema()
    yield database
    await database.close()


# ======================================================================
# change validator - the safety boundary (v2.5 §27)
# ======================================================================

def test_reject_risk_limit_change():
    problems = validate_changes(
        _proposal(_change("hard.max_risk_per_trade_pct", 2.0, 1.8)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("immutable" in p for p in problems)


def test_reject_paper_mode_change():
    problems = validate_changes(
        _proposal(_change("paper_only", True, False)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("immutable" in p for p in problems)


def test_reject_liquidity_floor_change():
    problems = validate_changes(
        _proposal(_change("liquidity_floor.min_volume", 5, 0)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("immutable" in p for p in problems)


def test_reject_tier_liquidity_leaf():
    """Per-tier liquidity values are hard-floor territory even though the
    tiers. prefix is otherwise mutable."""
    problems = validate_changes(
        _proposal(_change("tiers.1.min_volume", 25, 20)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("hard floor" in p for p in problems)


def test_allow_bounded_scoring_weight_change():
    problems = validate_changes(
        _proposal(_change("tiers.1.final_score_floor", 62.0, 60.0)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert problems == []


def test_reject_change_over_10_percent_bound():
    problems = validate_changes(
        _proposal(_change("tiers.1.final_score_floor", 62.0, 50.0)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("per-version bound" in p for p in problems)


def test_reject_misstated_champion_baseline():
    """A model that misstates the current value is proposing against a
    strategy that does not exist."""
    problems = validate_changes(
        _proposal(_change("tiers.1.final_score_floor", 70.0, 68.0)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert any("config holds" in p for p in problems)


def test_reject_unknown_parameter_path():
    problems = validate_changes(
        _proposal(_change("tracks.final_quota.MYSTERY", 1, 2)),
        CHAMPION_CONFIG, EVOLUTION_CFG)
    assert problems


def test_schema_rejects_more_than_three_changes():
    with pytest.raises(ValidationError):
        _proposal(
            _change("tiers.1.final_score_floor", 62.0, 60.0),
            _change("tiers.1.pre_score_floor", 58.0, 56.0),
            _change("tracks.final_quota.EVENT", 3, 2),
            _change("tracks.final_quota.MOMENTUM", 2, 3))


def test_apply_changes_deep_copies_and_sets():
    proposal = _proposal(_change("tiers.1.final_score_floor", 62.0, 60.0))
    out = apply_changes(CHAMPION_CONFIG, proposal.changes)
    assert out["tiers"][1]["final_score_floor"] == 60.0
    assert CHAMPION_CONFIG["tiers"][1]["final_score_floor"] == 62.0

    assert resolve_path(out, "tiers.1.final_score_floor") == 60.0


def test_evolution_decision_requires_reason_or_proposal():
    with pytest.raises(ValidationError):
        EvolutionDecision(propose=False, no_change_reason=None)
    with pytest.raises(ValidationError):
        EvolutionDecision(propose=True, proposal=None)
    ok = EvolutionDecision(propose=False,
                           no_change_reason="sample far too small")
    assert not ok.propose


# ======================================================================
# champion registry (v2.5 §27)
# ======================================================================

@pytest.mark.asyncio
async def test_single_champion_created_once(db):
    registry = ChampionRegistry(db, EVOLUTION_CFG)
    first = await registry.ensure_champion("v-test", CHAMPION_CONFIG)
    second = await registry.ensure_champion("v-test", CHAMPION_CONFIG)
    assert first == second
    rows = await db.fetchall(
        "SELECT * FROM strategy_versions WHERE status='CHAMPION'")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_single_active_challenger_limit(db):
    registry = ChampionRegistry(db, EVOLUTION_CFG)
    await registry.ensure_champion("v-test", CHAMPION_CONFIG)
    proposal = _proposal(_change("tiers.1.final_score_floor", 62.0, 60.0))
    await registry.store_challenger(proposal, CHAMPION_CONFIG)

    second = proposal.model_copy(update={"challenger_id": "alpha_v2_5_c2"})
    with pytest.raises(PromotionRefused, match="one hypothesis at a time"):
        await registry.store_challenger(second, CHAMPION_CONFIG)


@pytest.mark.asyncio
async def test_competition_promotion_requires_operator_approval(db):
    registry = ChampionRegistry(db, EVOLUTION_CFG)
    await registry.ensure_champion("v-test", CHAMPION_CONFIG)
    proposal = _proposal(_change("tiers.1.final_score_floor", 62.0, 60.0))
    await registry.store_challenger(proposal, CHAMPION_CONFIG)

    with pytest.raises(PromotionRefused, match="operator approval"):
        await registry.promote("alpha_v2_5_c1", operator_approved=False)
    champion = await registry.current_champion()
    assert champion["strategy_id"] == "alpha_v2_5_c0"

    # With approval the same call succeeds - the gate is the flag.
    await registry.promote("alpha_v2_5_c1", operator_approved=True)
    champion = await registry.current_champion()
    assert champion["strategy_id"] == "alpha_v2_5_c1"


@pytest.mark.asyncio
async def test_stored_version_config_is_immutable(db):
    registry = ChampionRegistry(db, EVOLUTION_CFG)
    await registry.ensure_champion("v-test", CHAMPION_CONFIG)
    before = (await registry.current_champion())["config_json"]
    proposal = _proposal(_change("tiers.1.final_score_floor", 62.0, 60.0))
    await registry.store_challenger(
        proposal, apply_changes(CHAMPION_CONFIG, proposal.changes))
    after = (await registry.current_champion())["config_json"]
    assert before == after


# ======================================================================
# promotion rules (v2.5 §27)
# ======================================================================

def _perf(strategy_id: str, pnl: float, drawdown: float, observations: int,
          expectancy: float | None = None,
          unmeasured: int = 0) -> StrategyPerformance:
    return StrategyPerformance(
        strategy_id=strategy_id, observations=observations,
        closed_trades=max(0, observations - 2), total_pnl=pnl,
        return_pct=pnl / 1000.0, max_drawdown_pct=drawdown,
        expectancy=expectancy, unmeasured_observations=unmeasured)


def test_insufficient_sample_continues_shadow():
    rec = recommend(_perf("c0", 100.0, 1.0, 30),
                    _perf("c1", 500.0, 0.5, 5),
                    sessions_observed=1, evolution_cfg=EVOLUTION_CFG)
    assert rec.recommendation == "CONTINUE_SHADOW"
    assert rec.evidence_strength == "INSUFFICIENT"
    assert rec.operator_approval_required


def test_higher_return_but_higher_drawdown_does_not_promote():
    rec = recommend(_perf("c0", 100.0, 1.0, 30),
                    _perf("c1", 500.0, 3.0, 20),
                    sessions_observed=3, evolution_cfg=EVOLUTION_CFG)
    assert rec.recommendation == "CONTINUE_SHADOW"
    assert any("drawdown" in r for r in rec.failed_promotion_rules)


def test_clearly_worse_challenger_keeps_champion():
    rec = recommend(_perf("c0", 500.0, 1.0, 30),
                    _perf("c1", -200.0, 2.0, 20),
                    sessions_observed=3, evolution_cfg=EVOLUTION_CFG)
    assert rec.recommendation == "KEEP_CHAMPION"


def test_all_rules_passing_promotes_with_operator_gate():
    rec = recommend(_perf("c0", 100.0, 2.0, 30, expectancy=10.0),
                    _perf("c1", 500.0, 1.0, 20, expectancy=25.0),
                    sessions_observed=3, evolution_cfg=EVOLUTION_CFG)
    assert rec.recommendation == "PROMOTE_CHALLENGER"
    assert rec.failed_promotion_rules == []
    assert rec.operator_approval_required


def test_unmeasured_share_blocks_promotion():
    rec = recommend(_perf("c0", 100.0, 2.0, 30),
                    _perf("c1", 500.0, 1.0, 20, unmeasured=10),
                    sessions_observed=3, evolution_cfg=EVOLUTION_CFG)
    assert rec.recommendation == "CONTINUE_SHADOW"
    assert rec.evidence_strength == "INSUFFICIENT"


def test_model_cannot_promote_past_failed_rules():
    """The schema itself refuses PROMOTE_CHALLENGER with failed rules."""
    with pytest.raises(ValidationError, match="failed promotion rules"):
        PromotionRecommendation(
            champion_id="c0", challenger_id="c1", generated_at=NOW,
            champion_performance=_perf("c0", 100.0, 1.0, 30),
            challenger_performance=_perf("c1", 50.0, 2.0, 20),
            recommendation="PROMOTE_CHALLENGER",
            evidence_strength="MEDIUM",
            failed_promotion_rules=["challenger drawdown worse"])


# ======================================================================
# shadow runner - deterministic re-scoring
# ======================================================================

async def _seed_scan(db: Database, scan_id: str, symbol: str,
                     final_score: float) -> None:
    await db.execute(
        "INSERT INTO scan_runs(scan_id, mode, started_at, status) "
        "VALUES(?, 'FULL', ?, 'COMPLETE')",
        (scan_id, "2026-09-01T14:00:00+00:00"))
    await db.execute(
        "INSERT INTO candidate_scores(candidate_id, scan_id, symbol, "
        "direction, as_of, momentum_score, relative_volume_score, "
        "trend_regime_score, relative_strength_score, "
        "options_opportunity_score, options_liquidity_score, "
        "data_confidence_factor, regime_factor, event_risk_factor, "
        "pre_score, raw_opportunity_score, final_opportunity_score, "
        "candidate_track, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"cand_{symbol}", scan_id, symbol, "BULLISH",
         "2026-09-01T14:00:00+00:00",
         70.0, 68.0, 75.0, 66.0, 65.0, 60.0, 1.0, 1.0, 1.0,
         69.0, 67.0, final_score, "MOMENTUM", "2026-09-01T14:00:00+00:00"))


@pytest.mark.asyncio
async def test_shadow_runner_floor_change_flips_would_trade(db):
    await _seed_scan(db, "scan_a", "NVDA", final_score=63.0)
    runner = ShadowRunner(db)

    tight = apply_changes(CHAMPION_CONFIG, _proposal(
        _change("tiers.1.final_score_floor", 62.0, 68.0)).changes)
    # 68 exceeds the 10% bound in validation, but apply_changes trusts its
    # caller; here it stands in for any tighter configuration.
    written = await runner.evaluate_scan("scan_a", "strict", tight)
    assert written == 1
    row = await db.fetchone(
        "SELECT would_trade FROM strategy_shadow_decisions "
        "WHERE strategy_id='strict'")
    assert row["would_trade"] == 0

    written = await runner.evaluate_scan("scan_a", "loose", CHAMPION_CONFIG)
    assert written == 1
    row = await db.fetchone(
        "SELECT would_trade, rationale_json FROM strategy_shadow_decisions "
        "WHERE strategy_id='loose'")
    assert row["would_trade"] == 1
    rationale = json.loads(row["rationale_json"])
    assert rationale["symbol"] == "NVDA"


@pytest.mark.asyncio
async def test_shadow_runner_is_idempotent_per_source(db):
    await _seed_scan(db, "scan_a", "NVDA", final_score=63.0)
    runner = ShadowRunner(db)
    assert await runner.evaluate_scan("scan_a", "s1", CHAMPION_CONFIG) == 1
    assert await runner.evaluate_scan("scan_a", "s1", CHAMPION_CONFIG) == 0


# ======================================================================
# performance - the common-set comparison states its limits
# ======================================================================

@pytest.mark.asyncio
async def test_challenger_performance_common_set_and_unmeasured(db):
    now = "2026-09-01T14:00:00+00:00"
    # A champion decision with a closed trade of +$250.
    await db.execute(
        "INSERT INTO decisions(decision_id, symbol, state, created_at, "
        "updated_at) VALUES('d1','NVDA','POSITION_CLOSED',?,?)", (now, now))
    await db.execute(
        "INSERT INTO trade_journal(trade_id, decision_id, status, qty, "
        "entry_debit, realized_pnl, thesis, candidate_track, opened_at, "
        "closed_at) VALUES('t1','d1','CLOSED',1,5.0,250.0,'x','MOMENTUM',"
        "?,?)", (now, now))
    # Challenger agreed with d1, and would also have traded a candidate
    # that never reached council (unmeasurable).
    for source, would in (("d1", 1), ("cand:cand_XYZ", 1)):
        await db.execute(
            "INSERT INTO strategy_shadow_decisions(shadow_decision_id, "
            "source_decision_id, strategy_id, evaluated_at, would_trade, "
            "rationale_json) VALUES(?,?,?,?,?,?)",
            (f"ssd_{source}", source, "c1", now, would,
             json.dumps({"track": "MOMENTUM"})))

    perf = await challenger_performance(db, "c1")
    assert perf.total_pnl == pytest.approx(250.0)
    assert perf.observations == 2
    assert perf.unmeasured_observations == 1

    champ = await champion_performance(db, "c0")
    assert champ.total_pnl == pytest.approx(250.0)
    assert champ.closed_trades == 1
