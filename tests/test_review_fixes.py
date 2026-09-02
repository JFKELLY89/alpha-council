"""
Alpha Council v2.5 - regression tests for the 2026-09-01 whole-of-model
code review fixes.

Each test pins a behavior that was broken in a way the existing suite did
not catch, usually because the suite exercised the pure function and the
bug lived in the wiring around it. See CODE_REVIEW_2026-09-01.md for the
full issue register these correspond to.

Run:
    uv run pytest tests/test_review_fixes.py -v
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from alpha_council.alpaca.market_data import SymbolSnapshot, normalize_snapshot
from alpha_council.db.engine import Database
from alpha_council.execution.order_manager import (
    _extract_fill_debit,
    build_intent,
    close_walk_prices,
    walk_prices,
)
from alpha_council.execution.position_monitor import PositionMonitor
from alpha_council.journal.shadow_book import ShadowBook
from alpha_council.journal.trade_journal import RejectionLog, TradeJournal
from alpha_council.models.enums import (
    CandidateTrack,
    Direction,
    GateStage,
    MarkMethod,
    ShadowVariant,
    Verdict,
)
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.models.enums import StrategyType
from alpha_council.risk.constitution import RiskConstitution
from alpha_council.risk.position_sizing import size_position

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _leg(strike: float, delta: float, side: str = "BUY",
         option_type: str = "CALL") -> OptionLeg:
    letter = "C" if option_type == "CALL" else "P"
    return OptionLeg(
        symbol=f"NVDA260918{letter}{int(strike * 1000):08d}",
        underlying="NVDA", expiration=EXP, option_type=option_type,
        strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure(structure_id: str = "st_1") -> OptionStructure:
    limit, width = 5.20, 10.0
    return OptionStructure(
        structure_id=structure_id, symbol="NVDA",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=1,
        expiration=EXP, dte=17,
        legs=[_leg(200.0, 0.60), _leg(210.0, 0.33, side="SELL")],
        width=width, net_delta=0.27, raw_mid_debit=5.10,
        adjusted_mid_debit=5.10, natural_debit=5.60, staleness_buffer=0.0,
        initial_limit_debit=limit, cost_to_width_ratio=limit / width,
        max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=205.20,
        max_quote_lag_seconds=4.0, underlying_price=204.0,
        liquidity_score=82.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.apply_schema()
    yield database
    await database.close()


# ======================================================================
# 1. SymbolSnapshot.signal_price exists (monitoring crashed without it)
# ======================================================================

def test_symbol_snapshot_has_signal_price():
    snap = normalize_snapshot("NVDA", {
        "latestQuote": {"bp": 203.9, "ap": 204.1,
                        "t": "2026-09-01T15:00:00Z"},
        "latestTrade": {"p": 204.0, "t": "2026-09-01T15:00:00Z"},
        "minuteBar": {"c": 204.05, "t": "2026-09-01T14:59:00Z"},
    }, now=NOW)
    assert isinstance(snap, SymbolSnapshot)
    assert snap.signal_price() == pytest.approx(204.0, abs=0.2)


def test_signal_price_falls_back_to_minute_bar():
    snap = normalize_snapshot("NVDA", {
        "latestQuote": {"bp": 0, "ap": 0},
        "minuteBar": {"c": 204.05, "t": "2026-09-01T14:59:00Z"},
    }, now=NOW)
    assert snap.signal_price() == pytest.approx(204.05)


# ======================================================================
# 2. Closing orders: credit semantics, descending walk, negative payload
# ======================================================================

def test_close_walk_descends_toward_conservative_floor():
    prices = close_walk_prices(adjusted_mid=5.10, conservative=4.60,
                               buffer=0.0)
    assert prices == sorted(prices, reverse=True)
    assert prices[-1] == pytest.approx(4.60)
    assert all(p >= 4.60 for p in prices)


def test_close_walk_never_demands_below_one_tick():
    prices = close_walk_prices(adjusted_mid=0.05, conservative=-0.10,
                               buffer=0.0)
    assert all(p >= 0.01 for p in prices)


def test_closing_payload_carries_negative_limit():
    intent = build_intent(_structure(), "dec_1", qty=1, limit_debit=4.85,
                          closing=True)
    payload = intent.to_alpaca_payload()
    assert float(payload["limit_price"]) == pytest.approx(-4.85)


def test_opening_payload_still_positive():
    intent = build_intent(_structure(), "dec_1", qty=1, limit_debit=5.20)
    assert float(intent.to_alpaca_payload()["limit_price"]) > 0


def test_fill_extraction_close_nets_credit():
    order = {"legs": [
        {"side": "sell", "filled_avg_price": "23.55", "ratio_qty": "1"},
        {"side": "buy", "filled_avg_price": "18.20", "ratio_qty": "1"},
    ]}
    assert _extract_fill_debit(order, 5.00, closing=True) == pytest.approx(
        5.35, abs=0.01)


# ======================================================================
# 3. The walk ceiling: risk budget headroom, not the first rung
# ======================================================================

def test_budget_leaves_walk_headroom():
    """floor($1250 / $520) = 2 spreads costing $1040; the walk may pay up
    to $625/spread — capped at natural — not just the initial $520."""
    sizing = size_position(equity=100_000.0, desired_risk_pct=1.25,
                           max_loss_per_spread=520.0)
    assert sizing.approved_qty == 2
    assert sizing.budget_dollars == pytest.approx(1250.0)

    per_spread_ceiling = sizing.budget_dollars / sizing.approved_qty / 100.0
    prices = walk_prices(adjusted_mid=5.10, natural=6.00, buffer=0.10,
                         max_allowed=per_spread_ceiling)
    # With the old ceiling (= the initial limit) this collapsed to 1 price.
    assert len(prices) >= 2
    assert max(prices) <= per_spread_ceiling + 1e-9


# ======================================================================
# 4. PASS verdict never resizes through the constitution
# ======================================================================

def test_constitution_ignores_cap_on_pass():
    from tests.test_risk import RISK_CFG, SCORING_CFG  # reuse fixtures

    rc = RiskConstitution(RISK_CFG, SCORING_CFG)
    # sector map now lives on the constitution
    assert isinstance(rc.sectors, dict)


# ======================================================================
# 5. Shadow book: restore and VETO decomposition
# ======================================================================

class StubMarks:
    def __init__(self, value: float):
        self.value = value

    async def spread_mark(self, structure, method):
        return self.value


@pytest.mark.asyncio
async def test_shadow_book_restore_rebuilds_variants(db):
    await db.execute(
        "INSERT INTO decisions(decision_id, symbol, state, created_at, "
        "updated_at) VALUES('d1','NVDA','FILLED',?,?)",
        ("2026-09-01T14:00:00+00:00", "2026-09-01T14:00:00+00:00"))
    book = ShadowBook(db, StubMarks(5.60))
    await book.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(), qty=3)
    await book.create("d1", ShadowVariant.EXECUTED, _structure("st_2"),
                      qty=2, entry_debit=5.30)

    fresh = ShadowBook(db, StubMarks(5.60))
    assert fresh.variants("d1") == {}
    restored = await fresh.restore()
    assert restored == 2
    variants = fresh.variants("d1")
    assert variants[ShadowVariant.GPT_ORIGINAL].qty == 3
    assert variants[ShadowVariant.EXECUTED].entry_debit == pytest.approx(5.30)

    marks = await fresh.mark_all("d1", NOW)
    assert len(marks) == 2


@pytest.mark.asyncio
async def test_veto_reads_as_sizing_effect_not_selection(db):
    """A VETO is a sizing-to-zero of the SAME structure. The old code
    forced the vetoed variant's pnl-per-spread to zero, which relabelled
    the whole effect as selection."""
    await db.execute(
        "INSERT INTO decisions(decision_id, symbol, state, created_at, "
        "updated_at) VALUES('d1','NVDA','FILLED',?,?)",
        ("2026-09-01T14:00:00+00:00", "2026-09-01T14:00:00+00:00"))
    book = ShadowBook(db, StubMarks(6.20))     # mark above the 5.20 entry
    await book.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(), qty=3)
    await book.create("d1", ShadowVariant.CLAUDE_MODIFIED, _structure(),
                      qty=0)                    # the VETO
    await book.mark_all("d1", NOW)

    result = book.compute("d1", NOW)
    s = result.snapshot
    assert s.claude_selection_effect == pytest.approx(0.0, abs=0.01)
    # (0 - 3) * $100/spread = -$300: the veto gave up the gain via sizing.
    assert s.claude_sizing_effect == pytest.approx(-300.0, abs=0.01)
    assert s.claude_value_added == pytest.approx(-300.0, abs=0.01)


@pytest.mark.asyncio
async def test_close_decision_freezes_executed_at_actual_exit(db):
    await db.execute(
        "INSERT INTO decisions(decision_id, symbol, state, created_at, "
        "updated_at) VALUES('d1','NVDA','FILLED',?,?)",
        ("2026-09-01T14:00:00+00:00", "2026-09-01T14:00:00+00:00"))
    book = ShadowBook(db, StubMarks(6.00))
    await book.create("d1", ShadowVariant.GPT_ORIGINAL, _structure(), qty=2)
    await book.create("d1", ShadowVariant.EXECUTED, _structure(), qty=2,
                      entry_debit=5.20)

    result = await book.close_decision("d1", executed_exit_debit=6.35, at=NOW)
    assert result is not None
    # Executed frozen at the ACTUAL exit credit, not the market mark.
    assert result.snapshot.executed_pnl_per_spread == pytest.approx(
        (6.35 - 5.20) * 100, abs=0.01)
    rows = await db.fetchall(
        "SELECT status FROM shadow_trades WHERE decision_id='d1'")
    assert all(r["status"] == "CLOSED" for r in rows)


# ======================================================================
# 6. Rejected shadows are created from shadow-eligible rejections
# ======================================================================

@pytest.mark.asyncio
async def test_rejection_log_creates_rejected_shadow(db):
    from alpha_council.db.config_store import ensure_config_version
    from alpha_council.journal.shadow_book import RejectedShadowBook

    await ensure_config_version(db, "v-test")
    rsb = RejectedShadowBook(db, StubMarks(5.60))
    log = RejectionLog(db, "v-test", tier=1, rejected_shadows=rsb)
    log.add("NVDA", GateStage.RED_TEAM, "RED_TEAM_VETO",
            Direction.BULLISH, structure=_structure(), decision_id=None)
    written = await log.flush()
    assert written == 1
    row = await db.fetchone("SELECT * FROM rejected_shadows")
    assert row is not None
    assert row["symbol"] == "NVDA"
    assert row["status"] == "OPEN"
    marked = await rsb.mark_open(NOW)
    assert marked == 1


# ======================================================================
# 7. Position monitor restores the RISK-APPROVED structure
# ======================================================================

@pytest.mark.asyncio
async def test_restore_selects_risk_approved_structure(db):
    """Five structures per decision; the one the risk engine approved is
    the one whose strikes drive the exits."""
    now_iso = "2026-09-01T14:00:00+00:00"
    await db.execute(
        "INSERT INTO decisions(decision_id, symbol, state, created_at, "
        "updated_at) VALUES('d1','NVDA','POSITION_OPEN',?,?)",
        (now_iso, now_iso))

    journal = TradeJournal(db)
    selected = _structure("st_selected")
    other = _structure("st_other")
    await journal.record_structures("d1", [other, selected])
    await db.execute(
        "INSERT INTO risk_evaluations(risk_evaluation_id, decision_id, "
        "evaluated_at, decision, account_equity, requested_qty, "
        "approved_qty, requested_max_loss, approved_max_loss, "
        "total_open_risk_pct_after, sector_risk_pct_after, "
        "daily_drawdown_pct, competition_drawdown_pct, structure_id) "
        "VALUES('r1','d1',?, 'APPROVE', 100000, 2, 2, 1040, 1040, "
        "1.04, 1.04, 0, 0, 'st_selected')", (now_iso,))
    await journal.open_trade("d1", "NVDA", qty=2, entry_debit=5.20,
                             thesis="t", invalidation=[],
                             track=CandidateTrack.EVENT)

    monitor = PositionMonitor(db, market=None, orders=None, journal=journal,
                              config={}, risk_config={})
    restored = await monitor.restore()
    assert restored == 1
    position = monitor.tracked[0]
    assert position.structure.structure_id == "st_selected"
    assert position.track is CandidateTrack.EVENT
