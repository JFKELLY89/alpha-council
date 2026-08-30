"""
Alpha Council v2.5 - journal and attribution tests.

Runs against a real in-memory SQLite database with the production schema,
so the SQL and the foreign keys are exercised rather than mocked.

Place at: tests/test_journal.py

Run:
    uv run pytest tests/test_journal.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from alpha_council.db.engine import Database
from alpha_council.journal.shadow_book import (
    RejectedShadowBook,
    ShadowBook,
    describe,
)
from alpha_council.journal.trade_journal import RejectionLog, TradeJournal
from alpha_council.models.enums import (
    CandidateTrack,
    ExitReason,
    GateStage,
    MarkMethod,
    ShadowVariant,
    StrategyType,
)
from alpha_council.models.execution import AttributionSnapshot
from alpha_council.models.trading import OptionLeg, OptionStructure

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)
TS = "2026-08-31T14:30:00+00:00"
SCHEMA = Path("alpha_council/db/schema.sql")


# ======================================================================
# fixtures
# ======================================================================

def _leg(strike: float, delta: float, side: str = "BUY") -> OptionLeg:
    return OptionLeg(
        symbol=f"NVDA260918C{int(strike * 1000):08d}", underlying="NVDA",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure(rank: int = 1, limit: float = 5.20) -> OptionStructure:
    width = 10.0
    return OptionStructure(
        structure_id=f"st_{rank}", symbol="NVDA",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=rank, expiration=EXP,
        dte=18, legs=[_leg(200.0, 0.60), _leg(210.0, 0.33, side="SELL")],
        width=width, net_delta=0.27, raw_mid_debit=limit - 0.10,
        adjusted_mid_debit=limit - 0.10, natural_debit=limit + 0.40,
        staleness_buffer=0.0, initial_limit_debit=limit,
        cost_to_width_ratio=limit / width, max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=200.0 + limit,
        max_quote_lag_seconds=4.0, liquidity_score=82.0,
        delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


class StubMarks:
    """Returns a scripted mark per structure_id. A model never does this."""

    def __init__(self, marks: dict[str, float]):
        self.marks = marks
        self.calls: list[tuple[str, str]] = []

    async def spread_mark(self, structure: OptionStructure,
                          method: MarkMethod) -> float | None:
        self.calls.append((structure.structure_id, str(method)))
        return self.marks.get(structure.structure_id)


async def seed_decision(db: Database, decision_id: str,
                        symbol: str = "NVDA") -> str:
    """Create the full foreign-key chain a decision depends on.

    scan_runs -> candidate_scores -> decisions. Real chain, real
    constraints: a broken FK in production code fails here first.
    """
    scan = f"s_{decision_id}"
    candidate = f"c_{decision_id}"
    await db.execute(
        "INSERT OR IGNORE INTO scan_runs(scan_id, mode, started_at, "
        "universe_size, candidate_count, status) VALUES(?,'FULL',?,1,1,'OK')",
        (scan, TS))
    await db.execute(
        "INSERT OR IGNORE INTO candidate_scores(candidate_id, scan_id, "
        "symbol, direction, as_of, momentum_score, relative_volume_score, "
        "trend_regime_score, relative_strength_score, "
        "data_confidence_factor, regime_factor, event_risk_factor, "
        "pre_score, created_at) "
        "VALUES(?,?,?,'BULLISH',?,70,70,70,70,1,1,1,70,?)",
        (candidate, scan, symbol, TS, TS))
    await db.execute(
        "INSERT OR IGNORE INTO decisions(decision_id, candidate_id, "
        "config_version, symbol, state, discovery_source, candidate_track, "
        "created_at, updated_at) "
        "VALUES(?,?,'v2.5',?,'CANDIDATE','MOVER','MOMENTUM',?,?)",
        (decision_id, candidate, symbol, TS, TS))
    return candidate


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.apply_schema(SCHEMA)
    await database.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES('v2.5',?,1,'{}','{}','test')",
        (TS,))
    yield database
    await database.close()


# ======================================================================
# schema integrity
# ======================================================================

@pytest.mark.asyncio
async def test_schema_is_self_sufficient(db):
    """Applying schema.sql alone must produce a current database.

    Before consolidation the v2.4 columns only existed via a migration, so
    a fresh reset silently produced a stale schema.
    """
    problems = await db.verify_schema()
    assert problems == [], problems

    columns = {r["name"] for r in
               await db.fetchall("PRAGMA table_info(decisions)")}
    assert {"discovery_source", "candidate_track", "config_version",
            "strategy_id"} <= columns


@pytest.mark.asyncio
async def test_alpha_evolution_tables_exist(db):
    """v2.5 schema ships ahead of v2.5 logic, so no second migration."""
    tables = await db.table_names()
    assert {"scenario_sets", "scenario_payoffs", "premarket_briefs",
            "strategy_lessons", "strategy_versions", "challenger_proposals",
            "strategy_shadow_decisions", "strategy_performance_snapshots",
            "promotion_recommendations"} <= tables


# ======================================================================
# trade journal
# ======================================================================

@pytest.mark.asyncio
async def test_open_and_close_a_winning_trade(db):
    journal = TradeJournal(db)
    candidate = await seed_decision(db, "d1")

    await journal.open_decision("d1", candidate, "NVDA", "v2.5", "MOVER",
                                CandidateTrack.MOMENTUM)
    await journal.open_trade("d1", "NVDA", qty=2, entry_debit=5.20,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.MOMENTUM, opened_at=NOW)

    closed = await journal.close_trade(
        "d1", exit_credit=6.40, reason=ExitReason.PROFIT_TARGET,
        closed_at=NOW + timedelta(hours=3))

    # (6.40 - 5.20) * 100 * 2 = $240
    assert closed.realized_pnl == pytest.approx(240.0)
    assert closed.realized_return_pct == pytest.approx(23.0769, abs=1e-3)
    assert await journal.state_of("d1") == "POSITION_CLOSED"


@pytest.mark.asyncio
async def test_losing_trade_is_recorded_as_negative(db):
    journal = TradeJournal(db)
    await seed_decision(db, "d2")
    await journal.open_trade("d2", "NVDA", qty=1, entry_debit=5.20,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.MOMENTUM, opened_at=NOW)
    closed = await journal.close_trade("d2", exit_credit=3.10,
                                       reason=ExitReason.PREMIUM_STOP)
    assert closed.realized_pnl == pytest.approx(-210.0)


@pytest.mark.asyncio
async def test_state_transitions_are_persisted(db):
    journal = TradeJournal(db)
    candidate = await seed_decision(db, "d3")
    await journal.open_decision("d3", candidate, "NVDA", "v2.5", "CORE",
                                CandidateTrack.EVENT)
    assert await journal.state_of("d3") == "CANDIDATE"

    await journal.open_trade("d3", "NVDA", qty=1, entry_debit=5.0,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.EVENT)
    assert await journal.state_of("d3") == "POSITION_OPEN"

    events = await db.fetchall(
        "SELECT * FROM system_events WHERE event_type='STATE_TRANSITION'")
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_performance_summary_arithmetic(db):
    journal = TradeJournal(db)
    for i, (entry, exit_, qty) in enumerate([(5.0, 6.0, 1), (5.0, 4.0, 1),
                                             (5.0, 7.0, 2)]):
        decision = f"dp{i}"
        await seed_decision(db, decision)
        await journal.open_trade(decision, "NVDA", qty=qty, entry_debit=entry,
                                 thesis="t", invalidation=[],
                                 track=CandidateTrack.MOMENTUM, opened_at=NOW)
        await journal.close_trade(decision, exit_credit=exit_,
                                  reason=ExitReason.TIME_STOP)

    perf = await journal.performance()
    assert perf["closed_trades"] == 3
    assert perf["total_pnl"] == pytest.approx(400.0)     # +100 -100 +400
    assert perf["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert perf["average_win"] == pytest.approx(250.0)
    assert perf["average_loss"] == pytest.approx(-100.0)
    assert perf["expectancy"] == pytest.approx(2 / 3 * 250 + 1 / 3 * -100,
                                                abs=0.5)
    assert perf["profit_factor"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_profit_factor_is_null_not_infinite(db):
    """No losses yet must store NULL, never infinity."""
    journal = TradeJournal(db)
    await seed_decision(db, "dw")
    await journal.open_trade("dw", "NVDA", qty=1, entry_debit=5.0,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.MOMENTUM, opened_at=NOW)
    await journal.close_trade("dw", exit_credit=6.0,
                              reason=ExitReason.PROFIT_TARGET)
    assert (await journal.performance())["profit_factor"] is None


@pytest.mark.asyncio
async def test_empty_journal_returns_zeroes(db):
    perf = await TradeJournal(db).performance()
    assert perf["closed_trades"] == 0
    assert perf["expectancy"] is None


@pytest.mark.asyncio
async def test_open_trades_lists_only_open(db):
    journal = TradeJournal(db)
    for i in range(2):
        await seed_decision(db, f"do{i}")
        await journal.open_trade(f"do{i}", "NVDA", qty=1, entry_debit=5.0,
                                 thesis="t", invalidation=[],
                                 track=CandidateTrack.MOMENTUM, opened_at=NOW)
    await journal.close_trade("do0", exit_credit=6.0,
                              reason=ExitReason.PROFIT_TARGET)
    assert len(await journal.open_trades()) == 1


# ======================================================================
# rejection log
# ======================================================================

@pytest.mark.asyncio
async def test_rejections_flush_and_aggregate(db):
    log = RejectionLog(db, "v2.5", tier=1)
    log.add("NVDA", GateStage.PRESCORE, "PRESCORE_FLOOR", observed=55,
            threshold=62)
    log.add("AMD", GateStage.PRESCORE, "PRESCORE_FLOOR", observed=51,
            threshold=62)
    log.add("SPY", GateStage.RISK, "RISK_QTY_ZERO", hard_gate=True)

    assert await log.flush() == 3
    assert log.buffer == []

    histogram = {r["gate_id"]: r["rejections"] for r in await log.histogram()}
    assert histogram["PRESCORE_FLOOR"] == 2
    assert histogram["RISK_QTY_ZERO"] == 1


@pytest.mark.asyncio
async def test_shadow_eligibility_follows_the_stage(db):
    log = RejectionLog(db, "v2.5")
    early = log.add("NVDA", GateStage.PRESCORE, "PRESCORE_FLOOR",
                    structure=_structure())
    late = log.add("NVDA", GateStage.RISK, "RISK_QTY_ZERO",
                   structure=_structure())
    assert not early.shadow_eligible    # no priced structure at prescore
    assert late.shadow_eligible
    assert late.shadow_structure_json is not None
    await log.flush()


@pytest.mark.asyncio
async def test_flush_is_idempotent(db):
    log = RejectionLog(db, "v2.5")
    log.add("NVDA", GateStage.RISK, "RISK_QTY_ZERO")
    assert await log.flush() == 1
    assert await log.flush() == 0


# ======================================================================
# shadow book and attribution
# ======================================================================

@pytest.mark.asyncio
async def test_no_modify_means_claude_had_no_effect(db):
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 6.40}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=2)
    await sb.create("d1", ShadowVariant.EXECUTED, _structure(1), qty=2)
    await sb.mark_all("d1", NOW)

    s = sb.compute("d1", NOW).snapshot
    assert s.claude_value_added == 0.0
    assert s.risk_constitution_value_added == 0.0
    assert s.executed_pnl == pytest.approx(240.0)


@pytest.mark.asyncio
async def test_claude_changed_structure_and_risk_cut_size(db):
    """The demo case: separate 'worse trade' from 'smaller trade'."""
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 6.40, "st_3": 5.95}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(3), qty=3)
    await sb.create("d1", ShadowVariant.EXECUTED, _structure(3), qty=1)
    await sb.mark_all("d1", NOW)

    s = sb.compute("d1", NOW).snapshot
    # GPT (6.40-5.20)*100 = 120/spread x3 = 360
    # Claude (5.95-5.20)*100 = 75/spread x3 = 225
    # Executed 75/spread x1 = 75
    assert s.gpt_original_pnl == pytest.approx(360.0)
    assert s.claude_modified_pnl == pytest.approx(225.0)
    assert s.executed_pnl == pytest.approx(75.0)

    assert s.claude_selection_effect == pytest.approx(-135.0)   # (75-120)*3
    assert s.claude_sizing_effect == pytest.approx(0.0)         # same qty
    assert s.risk_selection_effect == pytest.approx(0.0)        # same structure
    assert s.risk_sizing_effect == pytest.approx(-150.0)        # (1-3)*75

    assert (s.claude_selection_effect + s.claude_sizing_effect
            == pytest.approx(s.claude_value_added))
    assert s.total_governance_value_added == pytest.approx(-285.0)


@pytest.mark.asyncio
async def test_veto_measures_the_value_of_not_trading(db):
    """A losing GPT original plus a VETO is the Red Team's best outcome."""
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 3.70}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(1), qty=0)
    await sb.mark_all("d1", NOW)

    s = sb.compute("d1", NOW).snapshot
    assert s.gpt_original_pnl == pytest.approx(-450.0)
    assert s.claude_modified_qty == 0
    assert s.claude_modified_pnl == 0.0
    assert s.claude_value_added == pytest.approx(450.0)


@pytest.mark.asyncio
async def test_risk_saved_money_on_a_loser(db):
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 4.20}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=4)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(1), qty=4)
    await sb.create("d1", ShadowVariant.EXECUTED, _structure(1), qty=1)
    await sb.mark_all("d1", NOW)

    s = sb.compute("d1", NOW).snapshot
    assert s.risk_sizing_effect == pytest.approx(300.0)     # (1-4)*(-100)
    assert s.risk_constitution_value_added > 0


@pytest.mark.asyncio
async def test_every_variant_uses_one_method_and_one_timestamp(db):
    await seed_decision(db, "d1")
    marks = StubMarks({"st_1": 6.00, "st_2": 5.80})
    sb = ShadowBook(db, marks, method=MarkMethod.CONSERVATIVE)
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=2)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(2), qty=2)
    await sb.mark_all("d1", NOW)

    assert {m for _, m in marks.calls} == {"CONSERVATIVE"}
    rows = await db.fetchall(
        "SELECT DISTINCT marked_at, mark_method FROM shadow_marks")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_missing_mark_skips_rather_than_zeroing(db):
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 6.40}))      # st_2 has no mark
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=1)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(2), qty=1)
    marked = await sb.mark_all("d1", NOW)
    assert ShadowVariant.GPT_ORIGINAL in marked
    assert ShadowVariant.CLAUDE_MODIFIED not in marked


@pytest.mark.asyncio
async def test_attribution_persists_and_aggregates(db):
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 6.40, "st_3": 5.95}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(3), qty=3)
    await sb.create("d1", ShadowVariant.EXECUTED, _structure(3), qty=1)
    await sb.mark_all("d1", NOW)

    await sb.persist(sb.compute("d1", NOW))
    totals = await sb.portfolio_attribution()
    assert totals["n"] == 1
    assert totals["claude_total"] == pytest.approx(-135.0)
    assert totals["risk_sizing"] == pytest.approx(-150.0)

    view = await db.fetchone("SELECT * FROM v_attribution_totals")
    assert view["governance_total"] == pytest.approx(-285.0)


@pytest.mark.asyncio
async def test_compute_returns_none_without_a_gpt_original(db):
    assert ShadowBook(db, StubMarks({})).compute("nonexistent") is None


@pytest.mark.asyncio
async def test_veto_variant_is_stored_flat_not_dropped(db):
    """Dropping the vetoed variant would discard the Red Team's best case."""
    await seed_decision(db, "d1")
    sb = ShadowBook(db, StubMarks({"st_1": 3.70}))
    await sb.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(1), qty=3)
    await sb.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(1), qty=0)
    row = await db.fetchone(
        "SELECT * FROM shadow_trades WHERE variant='CLAUDE_MODIFIED'")
    assert row["qty"] == 0
    assert row["status"] == "FLAT"


# ======================================================================
# narrative
# ======================================================================

def test_narrative_reports_a_negative_result_plainly():
    s = AttributionSnapshot(
        decision_id="d1", as_of=NOW,
        gpt_original_pnl=360.0, claude_modified_pnl=225.0, executed_pnl=75.0,
        gpt_original_pnl_per_spread=120.0,
        claude_modified_pnl_per_spread=75.0, executed_pnl_per_spread=75.0,
        gpt_original_qty=3, claude_modified_qty=3, executed_qty=1,
        claude_selection_effect=-135.0, claude_sizing_effect=0.0,
        risk_selection_effect=0.0, risk_sizing_effect=-150.0,
        claude_value_added=-135.0, risk_constitution_value_added=-150.0)
    text = describe(s)
    assert "cost $135.00" in text
    assert "cost $150.00" in text
    assert "Governance overall cost $285.00" in text


def test_narrative_describes_a_veto():
    s = AttributionSnapshot(
        decision_id="d1", as_of=NOW,
        gpt_original_pnl=-450.0, claude_modified_pnl=0.0, executed_pnl=0.0,
        gpt_original_pnl_per_spread=-150.0,
        claude_modified_pnl_per_spread=0.0, executed_pnl_per_spread=0.0,
        gpt_original_qty=3, claude_modified_qty=0, executed_qty=0,
        claude_selection_effect=450.0, claude_sizing_effect=0.0,
        risk_selection_effect=0.0, risk_sizing_effect=0.0,
        claude_value_added=450.0, risk_constitution_value_added=0.0)
    text = describe(s)
    assert "VETO" in text
    assert "avoided a loss of $450.00" in text


# ======================================================================
# rejected shadows and GateValue
# ======================================================================

@pytest.mark.asyncio
async def test_rejected_shadow_closes_at_its_horizon(db):
    log = RejectionLog(db, "v2.5")
    rejection = log.add("NVDA", GateStage.RISK, "RISK_QTY_ZERO",
                        structure=_structure())
    await log.flush()

    rsb = RejectedShadowBook(db, StubMarks({"st_1": 6.00}))
    await rsb.create(rejection.rejection_id, "NVDA", _structure(),
                     horizon_end=NOW + timedelta(days=5), entry_timestamp=NOW)

    assert await rsb.mark_open(NOW + timedelta(days=1)) == 1
    row = await db.fetchone("SELECT * FROM rejected_shadows")
    assert row["status"] == "OPEN"
    assert row["final_pnl_per_spread"] is None

    await rsb.mark_open(NOW + timedelta(days=6))
    row = await db.fetchone("SELECT * FROM rejected_shadows")
    assert row["status"] == "CLOSED"
    assert row["final_pnl_per_spread"] == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_gate_value_is_computable_from_rejected_shadows(db):
    """A gate that blocked a profitable trade shows negative GateValue."""
    log = RejectionLog(db, "v2.5")
    rejection = log.add("NVDA", GateStage.RISK, "RISK_COST_TO_WIDTH",
                        structure=_structure())
    await log.flush()

    rsb = RejectedShadowBook(db, StubMarks({"st_1": 6.00}))
    await rsb.create(rejection.rejection_id, "NVDA", _structure(),
                     horizon_end=NOW, entry_timestamp=NOW)
    await rsb.mark_open(NOW + timedelta(days=1))

    rows = await log.gate_value()
    entry = next(r for r in rows if r["gate_id"] == "RISK_COST_TO_WIDTH")
    assert entry["avg_blocked_pnl_per_spread"] == pytest.approx(80.0)
    assert entry["gate_value"] == pytest.approx(-80.0)


@pytest.mark.asyncio
async def test_gate_that_blocked_a_loser_shows_positive_value(db):
    log = RejectionLog(db, "v2.5")
    rejection = log.add("NVDA", GateStage.RED_TEAM, "RED_TEAM_VETO",
                        structure=_structure())
    await log.flush()

    rsb = RejectedShadowBook(db, StubMarks({"st_1": 3.90}))
    await rsb.create(rejection.rejection_id, "NVDA", _structure(),
                     horizon_end=NOW, entry_timestamp=NOW)
    await rsb.mark_open(NOW + timedelta(days=1))

    entry = next(r for r in await log.gate_value()
                 if r["gate_id"] == "RED_TEAM_VETO")
    assert entry["gate_value"] == pytest.approx(130.0)   # blocked a -$130
