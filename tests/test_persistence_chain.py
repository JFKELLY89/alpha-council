"""
Alpha Council v2.5 - persistence chain integrity tests.

Four foreign-key failures in two days, all in the same place: a row was
written with an identifier derived independently by two callers, or taken
from model output rather than from us. Each one was found only when a live
council reached that stage, at roughly four cents a discovery.

These tests walk the entire chain — decision, proposal, structures, review,
risk evaluation, shadow variants, order, journal, attribution — against a
real schema with foreign keys enforced. Every write that the orchestrator
performs is exercised here, so the next mismatch fails in CI rather than in
the last hour of a competition.

Place at: tests/test_persistence_chain.py

Run:
    uv run pytest tests/test_persistence_chain.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from alpha_council.db.engine import Database
from alpha_council.journal.shadow_book import ShadowBook
from alpha_council.journal.trade_journal import TradeJournal
from alpha_council.models.enums import (
    CandidateTrack,
    DecisionState,
    Direction,
    ExitReason,
    MarkMethod,
    RiskDecision,
    Severity,
    ShadowVariant,
    StrategyType,
    Verdict,
)
from alpha_council.models.risk import RiskEvaluation, RiskViolation
from alpha_council.models.trading import (
    InvalidationRule,
    OptionLeg,
    OptionStructure,
    PortfolioProposal,
    RedTeamProblem,
    RedTeamReview,
)
from alpha_council.utils.ids import candidate_id, decision_id, scan_id

NOW = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)
TS = "2026-09-01T14:30:00+00:00"
SCHEMA = Path("alpha_council/db/schema.sql")


# ======================================================================
# fixtures
# ======================================================================

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "chain.db")
    await database.connect()
    await database.apply_schema(SCHEMA)
    await database.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES('v2.5',?,1,'{}','{}','test')",
        (TS,))
    yield database
    await database.close()


class StubMarks:
    async def spread_mark(self, structure, method):
        return 6.40


def _leg(strike: float, delta: float, side: str = "BUY") -> OptionLeg:
    return OptionLeg(
        symbol=f"UNH260918C{int(strike * 1000):08d}", underlying="UNH",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure(rank: int = 1) -> OptionStructure:
    limit, width = 5.20, 10.0
    return OptionStructure(
        structure_id=f"st_UNH_{rank}_abc", symbol="UNH",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=rank, expiration=EXP,
        dte=17, legs=[_leg(300.0, 0.60), _leg(310.0, 0.33, side="SELL")],
        width=width, net_delta=0.27, raw_mid_debit=5.10,
        adjusted_mid_debit=5.10, natural_debit=5.60, staleness_buffer=0.0,
        initial_limit_debit=limit, cost_to_width_ratio=limit / width,
        max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=305.20,
        max_quote_lag_seconds=4.0, underlying_price=304.0,
        liquidity_score=82.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


def _proposal(decision: str, revision: int = 0,
              trade: bool = True) -> PortfolioProposal:
    """A proposal carrying a decision_id the model invented.

    This is what actually happens: the PM returns a readable identifier of
    its own, not the one we assigned. Every test here uses that shape so a
    regression to trusting it fails immediately.
    """
    return PortfolioProposal(
        decision_id="UNH-20260901-143000-TRADE", revision=revision,
        symbol="UNH", trade=trade, direction=Direction.BULLISH,
        confidence=0.74, expected_horizon_days=8,
        desired_portfolio_risk_pct=1.2 if trade else 0.0,
        thesis="t", catalyst_summary="c",
        key_supporting_evidence=["a"], key_contrary_evidence=["b"],
        invalidation=[InvalidationRule(rule_type="PRICE", description="d",
                                       threshold=298.0, comparator="LT")]
        if trade else [],
        selected_structure_rank=1 if trade else None,
        abstain_reason=None if trade else "insufficient edge")


def _review(verdict: Verdict = Verdict.MODIFY) -> RedTeamReview:
    return RedTeamReview(
        decision_id="UNH-CLAUDE-INVENTED-ID", verdict=verdict,
        risk_score=6, fatal_flaw=verdict is Verdict.VETO,
        confidence_adjustment=-0.1,
        recommended_max_risk_pct=0.0 if verdict is Verdict.VETO else 0.8,
        problems=[RedTeamProblem(category="EXPRESSION", severity=6,
                                 description="breakeven needs 2.6%")],
        strongest_counterargument="x", information_to_reverse_verdict=[],
        summary="s")


def _evaluation(decision: str) -> RiskEvaluation:
    return RiskEvaluation(
        decision_id=decision, evaluated_at=NOW,
        decision=RiskDecision.RESIZE, config_version="v2.5", tier=1,
        account_equity=100000.0, requested_qty=2, approved_qty=1,
        requested_max_loss=1040.0, approved_max_loss=520.0,
        total_open_risk_pct_after=0.52, sector_risk_pct_after=0.52,
        daily_drawdown_pct=0.0, competition_drawdown_pct=0.0,
        violations=[RiskViolation(rule_id="RISK_RESIZED",
                                  severity=Severity.WARN,
                                  message="reduced 2 -> 1",
                                  observed_value=2, allowed_value=1)])


async def seed_chain(db: Database) -> tuple[str, str]:
    """Create scan_runs -> candidate_scores -> decisions.

    Returns (decision_id, candidate_id) using the SAME id the scanner would
    have written, which is the mismatch that broke production.
    """
    scan = scan_id()
    cid = candidate_id(scan, "UNH")
    did = decision_id()

    await db.execute(
        "INSERT INTO scan_runs(scan_id, mode, config_version, started_at, "
        "universe_size, candidate_count, status) "
        "VALUES(?,'FULL','v2.5',?,120,1,'COMPLETE')", (scan, TS))
    await db.execute(
        "INSERT INTO candidate_scores(candidate_id, scan_id, config_version, "
        "symbol, direction, as_of, momentum_score, relative_volume_score, "
        "trend_regime_score, relative_strength_score, data_confidence_factor, "
        "regime_factor, event_risk_factor, pre_score, discovery_source, "
        "candidate_track, created_at) "
        "VALUES(?,?,'v2.5','UNH','BULLISH',?,70,70,70,70,1,1,1,70,'CORE',"
        "'EVENT',?)", (cid, scan, TS, TS))
    await TradeJournal(db).open_decision(did, cid, "UNH", "v2.5", "CORE",
                                         CandidateTrack.EVENT)
    return did, cid


# ======================================================================
# the chain, stage by stage
# ======================================================================

@pytest.mark.asyncio
async def test_decision_requires_a_real_candidate_id(db):
    """candidate_id() carries a random suffix.

    Two callers deriving it independently get different values, which is
    what silently killed every scheduled scan for two days.
    """
    scan = scan_id()
    first = candidate_id(scan, "UNH")
    second = candidate_id(scan, "UNH")
    assert first != second, "candidate_id is not deterministic"

    did, cid = await seed_chain(db)
    row = await db.fetchone(
        "SELECT candidate_id FROM decisions WHERE decision_id=?", (did,))
    assert row["candidate_id"] == cid


@pytest.mark.asyncio
async def test_proposal_uses_our_decision_id_not_the_models(db):
    """The PM returns an identifier it invented. Ours must win."""
    did, _ = await seed_chain(db)
    journal = TradeJournal(db)
    proposal = _proposal(did)
    assert proposal.decision_id != did      # the model's value differs

    await journal.record_proposal(proposal, did)

    row = await db.fetchone(
        "SELECT decision_id FROM trade_proposals WHERE decision_id=?", (did,))
    assert row is not None


@pytest.mark.asyncio
async def test_proposal_without_our_id_would_break(db):
    """Passing the model's id must fail loudly rather than silently."""
    await seed_chain(db)
    journal = TradeJournal(db)
    with pytest.raises(Exception):
        await journal.record_proposal(_proposal("ignored"))


@pytest.mark.asyncio
async def test_structures_persist_against_the_decision(db):
    did, cid = await seed_chain(db)
    journal = TradeJournal(db)
    structures = [_structure(r) for r in range(1, 6)]

    await journal.record_structures(did, structures, cid)

    n = await db.fetchvalue(
        "SELECT COUNT(*) FROM option_structures WHERE decision_id=?", (did,))
    assert n == 5


@pytest.mark.asyncio
async def test_review_uses_our_decision_id(db):
    """Claude also invents an identifier. Same trap, same fix."""
    did, cid = await seed_chain(db)
    journal = TradeJournal(db)
    await journal.record_proposal(_proposal(did), did)
    review = _review()
    assert review.decision_id != did

    await journal.record_review(did, f"prop_{did[-8:]}_r0", review)

    row = await db.fetchone(
        "SELECT verdict, decision_id FROM red_team_reviews "
        "WHERE decision_id=?", (did,))
    assert row is not None
    assert row["verdict"] == "MODIFY"


@pytest.mark.asyncio
async def test_risk_evaluation_persists(db):
    did, cid = await seed_chain(db)
    journal = TradeJournal(db)
    await journal.record_proposal(_proposal(did), did)
    await journal.record_structures(did, [_structure(1)], cid)

    await journal.record_risk(_evaluation(did), f"prop_{did[-8:]}_r0",
                              "st_UNH_1_abc")

    row = await db.fetchone(
        "SELECT decision, approved_qty FROM risk_evaluations "
        "WHERE decision_id=?", (did,))
    assert row["decision"] == "RESIZE"
    assert row["approved_qty"] == 1


@pytest.mark.asyncio
async def test_shadow_variants_persist(db):
    did, _ = await seed_chain(db)
    book = ShadowBook(db, StubMarks(), MarkMethod.ADJUSTED_MID)

    await book.create(did, ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await book.create(did, ShadowVariant.CLAUDE_MODIFIED, _structure(3), qty=2)
    await book.create(did, ShadowVariant.EXECUTED, _structure(3), qty=1)

    n = await db.fetchvalue(
        "SELECT COUNT(*) FROM shadow_trades WHERE decision_id=?", (did,))
    assert n == 3


@pytest.mark.asyncio
async def test_marks_and_attribution_persist(db):
    did, _ = await seed_chain(db)
    book = ShadowBook(db, StubMarks(), MarkMethod.ADJUSTED_MID)
    await book.create(did, ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await book.create(did, ShadowVariant.EXECUTED, _structure(1), qty=1)

    await book.mark_all(did, NOW)
    result = book.compute(did, NOW)
    assert result is not None
    await book.persist(result)

    n = await db.fetchvalue(
        "SELECT COUNT(*) FROM decision_attribution WHERE decision_id=?", (did,))
    assert n == 1


@pytest.mark.asyncio
async def test_trade_opens_and_closes(db):
    did, _ = await seed_chain(db)
    journal = TradeJournal(db)

    await journal.open_trade(did, "UNH", qty=1, entry_debit=5.20,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.EVENT, opened_at=NOW)
    closed = await journal.close_trade(did, exit_credit=6.40,
                                       reason=ExitReason.PROFIT_TARGET)

    assert closed.realized_pnl == pytest.approx(120.0)
    assert await journal.state_of(did) == "POSITION_CLOSED"


# ======================================================================
# the whole path at once
# ======================================================================

@pytest.mark.asyncio
async def test_full_chain_end_to_end(db):
    """Every write the orchestrator performs on a trading decision.

    If this passes and production still raises IntegrityError, the
    orchestrator is passing an identifier this test is not.
    """
    did, cid = await seed_chain(db)
    journal = TradeJournal(db)
    book = ShadowBook(db, StubMarks(), MarkMethod.ADJUSTED_MID)
    structures = [_structure(r) for r in range(1, 6)]

    await journal.transition(did, DecisionState.COUNCIL_STARTED)
    await journal.record_proposal(_proposal(did), did)
    await journal.transition(did, DecisionState.PM_PROPOSED)
    await journal.record_structures(did, structures, cid)

    await book.create(did, ShadowVariant.GPT_ORIGINAL, structures[0], qty=3)

    await journal.record_review(did, f"prop_{did[-8:]}_r0", _review())
    await journal.transition(did, DecisionState.RED_TEAMED)
    await book.create(did, ShadowVariant.CLAUDE_MODIFIED, structures[2], qty=2)

    await journal.record_proposal(_proposal(did, revision=1), did)
    await journal.record_risk(_evaluation(did), f"prop_{did[-8:]}_r0",
                              structures[2].structure_id)
    await journal.transition(did, DecisionState.RISK_APPROVED)

    await db.execute(
        "INSERT INTO orders(order_pk, decision_id, structure_id, "
        "client_order_id, alpaca_order_id, intent, status, qty, limit_price, "
        "attempt, submitted_at, updated_at) "
        "VALUES('op1',?,?,'ac_test_r0_1','alp1','OPEN','filled',1,5.35,1,?,?)",
        (did, structures[2].structure_id, TS, TS))

    await journal.transition(did, DecisionState.FILLED)
    await journal.open_trade(did, "UNH", qty=1, entry_debit=5.35,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.EVENT, opened_at=NOW)
    await book.create(did, ShadowVariant.EXECUTED, structures[2], qty=1,
                      entry_debit=5.35)

    await book.mark_all(did, NOW)
    await book.persist(book.compute(did, NOW))

    counts = {}
    for table in ["decisions", "trade_proposals", "option_structures",
                  "red_team_reviews", "risk_evaluations", "shadow_trades",
                  "orders", "trade_journal", "decision_attribution"]:
        counts[table] = await db.fetchvalue(
            f"SELECT COUNT(*) FROM {table} WHERE decision_id=?", (did,))

    assert counts == {
        "decisions": 1, "trade_proposals": 2, "option_structures": 5,
        "red_team_reviews": 1, "risk_evaluations": 1, "shadow_trades": 3,
        "orders": 1, "trade_journal": 1, "decision_attribution": 1,
    }, counts

    # open_trade transitions to POSITION_OPEN itself, so that is the state
    # after a fill, not FILLED.
    assert await journal.state_of(did) == "POSITION_OPEN"

@pytest.mark.asyncio
async def test_foreign_keys_are_actually_enforced(db):
    """If PRAGMA foreign_keys were off, every test above would pass
    regardless and prove nothing."""
    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO trade_proposals(proposal_id, decision_id, revision, "
            "symbol, trade, direction, confidence, expected_horizon_days, "
            "desired_portfolio_risk_pct, thesis, catalyst_summary, "
            "created_at) VALUES('p','nonexistent',0,'X',1,'BULLISH',0.5,5,"
            "1.0,'t','c',?)", (TS,))
