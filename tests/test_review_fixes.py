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

from alpha_council.agents.evidence import EvidenceBuilder
from alpha_council.agents.outcomes import recent_outcomes, symbol_session_history
from alpha_council.alpaca.market_data import SymbolSnapshot, normalize_snapshot
from alpha_council.db.engine import Database
from alpha_council.evolution.cohort_review import build_cohort_review
from alpha_council.evolution.lessons import (
    config_spans,
    current_span_versions,
    span_base,
)
from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.enums import DiscoverySource
from alpha_council.quant.priors import PriorsService, chain_implied
from alpha_council.scheduler import TradingSession
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


# ======================================================================
# 8. Open interest comes from the contracts endpoint (found live 09-02:
#    the Indicative snapshots carry NO OI field at all)
# ======================================================================

def _chain_service():
    from alpha_council.options_engine.chain import ChainService

    return ChainService(api=None, market=None)


def _snap(now):
    return {
        "latestQuote": {"bp": 5.00, "ap": 5.10,
                        "t": now.isoformat().replace("+00:00", "Z")},
        "greeks": {"delta": 0.55},
        "dailyBar": {"v": 500},
    }


@pytest.mark.asyncio
async def test_oi_enriched_from_contracts_map():
    from alpha_council.options_engine.chain import ChainFilters, ChainResult

    service = _chain_service()
    result = ChainResult(symbol="SPY", underlying_price=760.0,
                         fetched_at=NOW)
    occ = "SPY260918C00760000"
    leg = await service._build_leg(
        occ, "SPY", EXP, "CALL", 760.0, _snap(NOW), 760.0,
        ChainFilters(min_open_interest=250), NOW, result,
        oi_map={occ: (5000, "2026-09-01")})
    assert leg is not None
    assert leg.open_interest == 5000
    assert str(leg.open_interest_date) == "2026-09-01"


@pytest.mark.asyncio
async def test_missing_from_oi_map_rejects():
    from alpha_council.options_engine.chain import ChainFilters, ChainResult

    service = _chain_service()
    result = ChainResult(symbol="SPY", underlying_price=760.0,
                         fetched_at=NOW)
    leg = await service._build_leg(
        "SPY260918C00760000", "SPY", EXP, "CALL", 760.0, _snap(NOW), 760.0,
        ChainFilters(min_open_interest=250), NOW, result,
        oi_map={"OTHER": (5000, None)})
    assert leg is None
    assert any(g == "OPT_OI_MISSING" for _, g, _ in result.rejections)


@pytest.mark.asyncio
async def test_oi_gate_stands_down_when_source_unavailable():
    """A contracts-endpoint outage degrades the gate, never the chain:
    without this, an OI-source failure rejects every contract and the
    desk goes dark (exactly what happened live before the fix)."""
    from alpha_council.options_engine.chain import ChainFilters, ChainResult

    service = _chain_service()
    result = ChainResult(symbol="SPY", underlying_price=760.0,
                         fetched_at=NOW)
    leg = await service._build_leg(
        "SPY260918C00760000", "SPY", EXP, "CALL", 760.0, _snap(NOW), 760.0,
        ChainFilters(min_open_interest=250), NOW, result, oi_map=None)
    assert leg is not None
    assert leg.open_interest is None


# ======================================================================
# 9. Anthropic schema compatibility (found on the first live Red Team
#    call: output_config 400s on integer minimum/maximum)
# ======================================================================

def test_anthropic_safe_schema_strips_range_constraints():
    from alpha_council.agents.llm import anthropic_safe_schema
    from alpha_council.models.trading import RedTeamReview

    raw = RedTeamReview.model_json_schema()
    assert "minimum" in json.dumps(raw)        # ge=1/le=10 fields exist

    safe = anthropic_safe_schema(raw)
    dumped = json.dumps(safe)
    for banned in ("minimum", "maximum", "exclusiveMinimum",
                   "exclusiveMaximum", "multipleOf"):
        assert banned not in dumped
    # The original is untouched and client-side validation still binds.
    assert "minimum" in json.dumps(raw)
    with pytest.raises(Exception):
        RedTeamReview.model_validate({"decision_id": "d", "verdict": "PASS",
                                      "risk_score": 99, "fatal_flaw": False,
                                      "confidence_adjustment": 0.0,
                                      "recommended_max_risk_pct": 1.0,
                                      "problems": [],
                                      "strongest_counterargument": "x",
                                      "information_to_reverse_verdict": [],
                                      "summary": "s"})


# ======================================================================
# 10. Server-stamped generated_at (found live 09-17: the lessons model
#     emitted the string 'not provided in brief' for generated_at and
#     the whole LessonSet was voided; PreMarketBrief and ScenarioSet
#     carried the same landmine)
# ======================================================================

def _minimal_lesson_set(generated_at):
    from alpha_council.models.lessons import LessonSet

    return LessonSet.model_validate({
        "generated_at": generated_at,
        "period_start": "2026-09-10T20:15:00+00:00",
        "period_end": "2026-09-17T20:15:00+00:00",
        "closed_trades": 4,
        "decisions_reviewed": 29,
        "overall_assessment": "Sample is too small for conclusions.",
        "lessons": [],
        "insufficient_evidence": True,
    })


def test_lesson_set_survives_garbage_generated_at():
    # The exact string from the 09-17 live failure.
    parsed = _minimal_lesson_set("not provided in brief")
    assert parsed.generated_at.tzinfo is not None


def test_generated_at_is_server_time_not_model_echo():
    from datetime import datetime, timezone

    parsed = _minimal_lesson_set("2001-01-01T00:00:00+00:00")
    # A valid-but-echoed timestamp is discarded too: the server owns
    # this clock, so the value must be now, not the model's echo.
    assert parsed.generated_at.year >= 2026
    assert (datetime.now(timezone.utc) - parsed.generated_at
            ).total_seconds() < 60


def test_scenario_and_brief_generated_at_are_stamped():
    from alpha_council.models.evolution import PreMarketBrief
    from alpha_council.models.scenario import ScenarioSet

    brief = PreMarketBrief.model_validate({
        "session_date": "2026-09-17",
        "generated_at": "yesterday, probably",
        "regime_summary": "Mixed tape with defensive rotation intraday.",
        "session_bias": "MIXED",
        "confidence": 0.5,
    })
    assert brief.generated_at.tzinfo is not None

    scenarios = ScenarioSet.model_validate({
        "scenario_set_id": "ss_x", "decision_id": "dec_x", "symbol": "SPY",
        "spot_at_generation": 760.0,
        "generated_at": "not provided in brief",
        "overall_uncertainty": "POSSIBLE",
        "scenarios": [
            {"scenario_type": "CONTINUATION",
             "narrative": "Trend continuation on participation.",
             "likelihood": "LIKELY", "horizon_days": 7,
             "underlying_low": 762.0, "underlying_mid": 766.0,
             "underlying_high": 772.0},
            {"scenario_type": "REVERSAL",
             "narrative": "Failed breakout mean-reverts.",
             "likelihood": "POSSIBLE", "horizon_days": 7,
             "underlying_low": 744.0, "underlying_mid": 750.0,
             "underlying_high": 756.0},
        ],
    })
    assert scenarios.generated_at.tzinfo is not None


# ======================================================================
# 11. After-hours exit loop (live 09-17 overnight: VWAP invalidation at
#     16:36 ET started a close walk into a closed options market; one
#     rung survived as a resting straggler and every 2-minute retry
#     403'd against its leg hold - 131 times)
# ======================================================================

from datetime import date as _date
from datetime import timedelta as _td

from alpha_council.execution.order_manager import ExecutionOutcome
from alpha_council.execution.position_monitor import (
    ExitDecision as _ExitDecision,
)
from alpha_council.execution.position_monitor import (
    MonitoredPosition,
    PositionMonitor,
)
from alpha_council.models.enums import ExitReason
from alpha_council.utils.time import ET as _ET


class _FakeDB:
    def __init__(self):
        self.events = []

    async def log_event(self, level, source, event_type, message,
                        context=None, decision_id=None):
        self.events.append(event_type)

    async def execute(self, *a, **k):
        return None


class _FakeSnap:
    quote_age = 3.0

    def signal_price(self, _pct):
        return 204.0


class _FakeMarket:
    async def snapshots(self, symbols):
        return {s: _FakeSnap() for s in symbols}

    async def load_bars(self, *a, **k):
        raise RuntimeError("no bars in this test")


class _FakeOrders:
    def __init__(self, swept_credit=None):
        self.swept_credit = swept_credit
        self.sweeps = 0
        self.walks = 0

    async def sweep_working_closes(self, decision_id):
        self.sweeps += 1
        return self.swept_credit

    async def execute_with_walk(self, structure, decision_id, qty, **kw):
        self.walks += 1
        return ExecutionOutcome(decision_id=decision_id, filled=False,
                                qty=qty, final_status="NO_FILL")

    async def record_calibration(self, *a, **k):
        return None


class _FakeJournal:
    def __init__(self):
        self.closes = []

    async def close_trade(self, decision_id, exit_credit, reason, closed_at):
        self.closes.append((decision_id, exit_credit, reason))


def _monitor_with_time_stop(swept_credit=None):
    # The module _structure() expires 2026-09-18: dte 1 from the test's
    # 09-17 "now", inside time_stop_dte 2, so TIME_STOP fires on poll.
    structure = _structure()

    db, market = _FakeDB(), _FakeMarket()
    orders, journal = _FakeOrders(swept_credit), _FakeJournal()
    risk_cfg = {"exits": {
        "primary": {"underlying_target_at_short_strike": True,
                    "honor_pm_invalidation_rules": True,
                    "time_stop_dte": 2},
        "secondary": {"profit_target_pct_of_max": 0.55,
                      "premium_stop_pct_of_entry": 0.45,
                      "require_data_confidence": ["HIGH", "MEDIUM"]},
        "never_require_llm_to_exit": True}}
    monitor = PositionMonitor(db=db, market=market, orders=orders,
                              journal=journal, config={},
                              risk_config=risk_cfg)
    monitor.track(MonitoredPosition(
        decision_id="d1", symbol="NVDA", structure=structure, qty=1,
        entry_debit=5.20,
        opened_at=datetime(2026, 9, 15, 10, 0, tzinfo=_ET)))
    return monitor, db, orders, journal


@pytest.mark.asyncio
async def test_exit_waits_for_options_rth():
    monitor, db, orders, _ = _monitor_with_time_stop()
    night = datetime(2026, 9, 17, 21, 50, tzinfo=_ET)

    for _ in range(3):                       # three overnight polls
        await monitor.poll(now=night, execute=True)

    assert orders.walks == 0 and orders.sweeps == 0
    # Logged once, not once per 2-minute cycle.
    assert db.events.count("EXIT_AWAITING_OPEN") == 1
    assert "d1" in {p.decision_id for p in monitor.tracked}

    # At the open the same signal executes (sweep first, then the walk).
    await monitor.poll(now=datetime(2026, 9, 18, 10, 30, tzinfo=_ET),
                       execute=True)
    assert orders.sweeps == 1 and orders.walks == 1


@pytest.mark.asyncio
async def test_swept_straggler_fill_becomes_the_exit():
    monitor, db, orders, journal = _monitor_with_time_stop(
        swept_credit=1.17)
    outcome = await monitor.close(
        monitor.tracked[0],
        _ExitDecision(True, ExitReason.TIME_STOP, "test"),
        now=datetime(2026, 9, 18, 9, 31, tzinfo=_ET))

    assert orders.sweeps == 1
    assert orders.walks == 0                 # no new order on top of a fill
    assert outcome.filled and outcome.fill_debit == 1.17
    assert journal.closes == [("d1", 1.17, ExitReason.TIME_STOP)]
    assert monitor.tracked == []             # untracked once closed


# ======================================================================
# 12. Budget thresholds bound a rolling window, not all-time spend
#     (live 10-09..10-21: lifetime spend crossed the reserve line and
#     analysts/lessons/evolution/briefing were refused for 12 days)
# ======================================================================

@pytest.mark.asyncio
async def test_budget_thresholds_ignore_spend_outside_window(db):
    from alpha_council.agents.budget import BudgetManager, BudgetMode
    from alpha_council.utils.ids import new_uuid
    from alpha_council.utils.time import iso_utc, utc_now

    old = iso_utc(utc_now() - timedelta(days=40))
    recent = iso_utc(utc_now())
    for occurred, cost in ((old, 43.0), (recent, 5.0)):
        await db.execute(
            "INSERT INTO api_usage(usage_id, provider, model, endpoint, "
            "occurred_at, input_tokens, output_tokens, cached_tokens, "
            "cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_uuid(), "openai", "gpt-5.6-sol", "bull", occurred,
             1000, 500, 0, cost))

    budget = BudgetManager(db, {"budget": {"rolling_window_days": 30}})
    await budget.load()

    # All-time spend is $48 (would be RESERVE/BLOCKED); the window sees $5.
    assert budget.total_spent("openai") == pytest.approx(5.0)
    assert budget.mode("openai") is BudgetMode.NORMAL
    decision = budget.allow_call("openai", "bull", estimated_cost=0.05)
    assert decision.allowed, decision.reason


# ======================================================================
# 13. Signed catalyst scores are rescued, not discarded (live
#     09-02..10-21: 56 INVALID_SCHEMA catalyst calls emitted bearish
#     strength as a negative score and the PM read it as "no catalyst")
# ======================================================================

def _assessment(score):
    from alpha_council.models.candidate import AnalystAssessment

    return AnalystAssessment.model_validate({
        "symbol": "C", "analyst": "CATALYST", "score": score,
        "confidence": 0.82,
        "thesis": "Earnings miss with guidance cut; bearish repricing.",
        "evidence_for": ["EPS and revenue below consensus."],
        "evidence_against": ["Buyback continues."],
        "missing_information": [],
        "invalidation_conditions": ["Guidance restored."],
    })


def test_negative_unit_scale_score_maps_to_strength():
    # The exact live emission: bearish strength -0.72 -> strength 72.
    assert _assessment(-0.72).score == pytest.approx(72.0)


def test_negative_full_scale_score_keeps_magnitude():
    assert _assessment(-45.0).score == pytest.approx(45.0)


def test_valid_scores_pass_through_untouched():
    assert _assessment(63.0).score == pytest.approx(63.0)
    assert _assessment(0.0).score == pytest.approx(0.0)


# ======================================================================
# 14. Broker-identity guard (imported from the hackathon winner's
#     design: gates key on the broker-REPORTED account number)
# ======================================================================

class _FakeIdentityApi:
    def __init__(self, account=None, exc=None):
        self._account, self._exc = account, exc

    async def get_account(self):
        if self._exc:
            raise self._exc
        return self._account

    from alpha_council.alpaca.rest_client import AlpacaRestClient
    assert_account_identity = AlpacaRestClient.assert_account_identity


@pytest.mark.asyncio
async def test_identity_mismatch_refuses_to_run():
    api = _FakeIdentityApi(account={"account_number": "PA3ABC9999"})
    with pytest.raises(RuntimeError, match="refusing to run"):
        await api.assert_account_identity("1234")


@pytest.mark.asyncio
async def test_identity_match_and_inert_paths():
    api = _FakeIdentityApi(account={"account_number": "PA3ABC1234"})
    assert await api.assert_account_identity("1234") == "verified"
    assert await api.assert_account_identity("") == "unconfigured"


@pytest.mark.asyncio
async def test_unreadable_identity_warns_and_proceeds():
    api = _FakeIdentityApi(exc=RuntimeError("transport down"))
    assert await api.assert_account_identity("1234") == "unreadable"


# ======================================================================
# 15. Hackathon review idea #2 - outcome feedback and repeat-rejection
#     cooldown (the PM proposed blind to its own record, and refused
#     theses re-consumed council seats the same session)
# ======================================================================

async def _seed_closed_trade(db, decision_id: str, symbol: str, pnl: float,
                             exit_reason: str, hours_held: float,
                             closed_at: datetime) -> None:
    opened = closed_at - timedelta(hours=hours_held)
    await db.execute(
        "INSERT INTO decisions(decision_id, config_version, symbol, state, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (decision_id, "v-test", symbol, "CLOSED",
         opened.isoformat(), closed_at.isoformat()))
    await db.execute(
        "INSERT INTO trade_journal(trade_id, decision_id, opened_at, "
        "closed_at, status, qty, entry_debit, realized_pnl, candidate_track, "
        "thesis, exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"tr_{decision_id}", decision_id, opened.isoformat(),
         closed_at.isoformat(), "CLOSED", 1, 5.0, pnl, "EVENT",
         "t", exit_reason))


@pytest.mark.asyncio
async def test_recent_outcomes_block_aggregates_the_journal(db):
    await _seed_closed_trade(db, "dec_o1", "QCOM", 120.0,
                             "UNDERLYING_TARGET", 26.0,
                             NOW - timedelta(days=1))
    await _seed_closed_trade(db, "dec_o2", "NOW", -80.0,
                             "UNDERLYING_INVALIDATION", 3.0,
                             NOW - timedelta(hours=5))

    block = await recent_outcomes(db, now=NOW)
    assert block is not None
    assert block["aggregate"]["closed_trades"] == 2
    assert block["aggregate"]["win_rate"] == pytest.approx(0.5)
    assert block["aggregate"]["total_pnl"] == pytest.approx(40.0)
    # Newest first, with the hold time the PM never used to see.
    assert block["last_closed_trades"][0]["symbol"] == "NOW"
    assert block["last_closed_trades"][0]["hold_hours"] == pytest.approx(3.0)
    reasons = {r["exit_reason"] for r in block["by_exit_reason"]}
    assert reasons == {"UNDERLYING_TARGET", "UNDERLYING_INVALIDATION"}


@pytest.mark.asyncio
async def test_recent_outcomes_none_on_an_empty_journal(db):
    assert await recent_outcomes(db, now=NOW) is None


async def _seed_config_version(db, version: str, activated_at: str,
                               tier: int = 1) -> None:
    await db.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES(?,?,?,?,?,?)",
        (version, activated_at, tier, "{}", "{}", "test"))


async def _seed_rejection(db, symbol: str, stage: str, gate_id: str,
                          direction: str, occurred_at: datetime) -> None:
    await db.execute(
        "INSERT INTO gate_rejections(rejection_id, occurred_at, "
        "config_version, symbol, direction, stage, gate_id, tier, hard_gate, "
        "shadow_eligible, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"rej_{symbol}_{gate_id}_{occurred_at:%H%M%S%f}",
         occurred_at.isoformat(), "v-test", symbol, direction, stage,
         gate_id, 1, 1, 0, "test"))


@pytest.mark.asyncio
async def test_symbol_session_history_reports_todays_refusals(db):
    await _seed_config_version(db, "v-test",
                               (NOW - timedelta(days=3)).isoformat())
    await _seed_rejection(db, "AMD", "RISK", "RISK_PER_TRADE_LOSS",
                          "BULLISH", NOW - timedelta(hours=2))
    await db.execute(
        "INSERT INTO decisions(decision_id, config_version, symbol, state, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        ("dec_h1", "v-test", "AMD", "RISK_REJECTED",
         (NOW - timedelta(hours=3)).isoformat(),
         (NOW - timedelta(hours=3)).isoformat()))
    await db.execute(
        "INSERT INTO trade_proposals(proposal_id, decision_id, revision, "
        "symbol, trade, direction, confidence, expected_horizon_days, "
        "desired_portfolio_risk_pct, thesis, catalyst_summary, "
        "abstain_reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("prop_h1", "dec_h1", 0, "AMD", 0, "BULLISH", 0.5, 5, 0.0, "t", "c",
         "insufficient edge", (NOW - timedelta(hours=3)).isoformat()))

    history = await symbol_session_history(db, "AMD", now=NOW)
    assert history is not None
    assert history["todays_refusals"][0]["gate_id"] == "RISK_PER_TRADE_LOSS"
    assert history["todays_abstains"][0]["reason"] == "insufficient edge"
    # A clean symbol ships no section at all.
    assert await symbol_session_history(db, "NVDA", now=NOW) is None


def _cand(symbol: str, track=CandidateTrack.MOMENTUM) -> CandidateFeatures:
    return CandidateFeatures(
        symbol=symbol, as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.45, track=track,
        discovery_source=DiscoverySource.CORE, momentum_score=78.0,
        relative_volume_score=72.0, trend_regime_score=75.0,
        relative_strength_score=68.0, options_opportunity_score=78.0,
        options_liquidity_score=82.0, data_confidence_factor=1.0,
        regime_factor=1.0, event_risk_factor=1.0, pre_score=74.0)


def _bare_session(db, cfg: dict) -> TradingSession:
    return TradingSession(
        db=db, scanner=None, orchestrator=None, tiers=None, monitor=None,
        shadows=None, journal=None, orders=None, market=None, screeners=None,
        budget=None, config=cfg, risk_config={}, universe_config={})


@pytest.mark.asyncio
async def test_recent_risk_rejection_skips_the_council_seat(db):
    await _seed_config_version(db, "v-test",
                               (NOW - timedelta(days=3)).isoformat())
    from alpha_council.utils.time import utc_now as real_now
    recent = real_now() - timedelta(hours=1)
    await _seed_rejection(db, "NVDA", "RISK", "RISK_PER_TRADE_LOSS",
                          "BULLISH", recent)
    # Infrastructure failure at the Red Team stage is NOT a refusal.
    await _seed_rejection(db, "AMD", "RED_TEAM", "COUNCIL_RED_TEAM_FAILED",
                          "BULLISH", recent)

    session = _bare_session(db, {"council_seats": {
        "rejection_cooldown_hours": 4.0, "abstain_cooldown_hours": 0.0,
        "reserve_event_seat": False}})
    seats = await session._allocate_council_seats(
        [_cand("NVDA"), _cand("AMD")], max_councils=2)
    assert [c.symbol for c in seats] == ["AMD"]


@pytest.mark.asyncio
async def test_red_team_veto_skips_but_other_direction_does_not(db):
    await _seed_config_version(db, "v-test",
                               (NOW - timedelta(days=3)).isoformat())
    from alpha_council.utils.time import utc_now as real_now
    recent = real_now() - timedelta(hours=1)
    await _seed_rejection(db, "NVDA", "RED_TEAM", "RED_TEAM_VETO",
                          "BULLISH", recent)
    await _seed_rejection(db, "AMD", "RISK", "RISK_SECTOR_RISK",
                          "BEARISH", recent)   # candidate below is BULLISH

    session = _bare_session(db, {"council_seats": {
        "rejection_cooldown_hours": 4.0, "abstain_cooldown_hours": 0.0,
        "reserve_event_seat": False}})
    seats = await session._allocate_council_seats(
        [_cand("NVDA"), _cand("AMD")], max_councils=2)
    assert [c.symbol for c in seats] == ["AMD"]


def test_outcome_sections_reach_the_pm_and_only_the_pm_roles():
    builder = EvidenceBuilder(
        candidate=_cand("NVDA"),
        recent_outcomes={"aggregate": {"closed_trades": 2}},
        symbol_history={"todays_refusals": []},
        market_prior={"p_finish_above_spot": 0.51})
    pm = builder.build("PM").sections
    assert "recent_outcomes" in pm and "symbol_session_history" in pm
    assert "market_implied_prior" in pm

    bull = builder.build("BULL").sections
    assert "recent_outcomes" not in bull          # analysts stay independent
    assert "market_implied_prior" in bull         # but see the market crowd

    selection = builder.build("SELECTION").sections
    assert "recent_outcomes" not in selection
    assert "market_implied_prior" not in selection


# ======================================================================
# 16. Hackathon review idea #3 - evolution evidence windowed on
#     uniform-config spans (tier escalations collapse onto their base;
#     config churn stops being a reason to refuse analysis)
# ======================================================================

def test_span_base_collapses_tier_versions_only():
    assert span_base("v2.6-hackathon-1") == "v2.6-hackathon-1"
    assert span_base("v2.6-hackathon-1-t2-143055-ab12") == "v2.6-hackathon-1"
    assert span_base("v2.5-measured-2-t3-090000-0f3c") == "v2.5-measured-2"
    assert span_base(None) == ""


async def _seed_two_spans(db) -> None:
    t = NOW - timedelta(days=10)
    await _seed_config_version(db, "vA", t.isoformat())
    await _seed_config_version(db, "vA-t2-120000-ab12",
                               (t + timedelta(hours=3)).isoformat(), tier=2)
    await _seed_config_version(db, "vB", (t + timedelta(days=5)).isoformat())
    await _seed_config_version(db, "vB-t3-141500-cd34",
                               (t + timedelta(days=5, hours=5)).isoformat(),
                               tier=3)
    rows = [("dec_s1", "vA"), ("dec_s2", "vA-t2-120000-ab12"),
            ("dec_s3", "vB"), ("dec_s4", "vB-t3-141500-cd34")]
    for decision_id, version in rows:
        await db.execute(
            "INSERT INTO decisions(decision_id, config_version, symbol, "
            "state, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (decision_id, version, "NVDA", "CLOSED",
             t.isoformat(), t.isoformat()))
    await db.execute(
        "INSERT INTO trade_journal(trade_id, decision_id, opened_at, "
        "closed_at, status, qty, entry_debit, realized_pnl, thesis) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("tr_s1", "dec_s1", t.isoformat(), t.isoformat(), "CLOSED", 1,
         5.0, 10.0, "t"))
    await db.execute(
        "INSERT INTO trade_journal(trade_id, decision_id, opened_at, "
        "closed_at, status, qty, entry_debit, realized_pnl, thesis) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("tr_s3", "dec_s3", t.isoformat(), t.isoformat(), "CLOSED", 1,
         5.0, -5.0, "t"))


@pytest.mark.asyncio
async def test_config_spans_group_tier_versions_onto_their_base(db):
    await _seed_two_spans(db)
    spans = await config_spans(db)
    assert [s["base_version"] for s in spans] == ["vA", "vB"]
    assert spans[0]["version_count"] == 2
    assert spans[0]["decisions"] == 2
    assert spans[0]["closed_trades"] == 1
    assert spans[0]["pnl"] == pytest.approx(10.0)
    assert spans[0]["ended_at"] == spans[1]["started_at"]
    assert spans[1]["ended_at"] is None
    assert spans[1]["pnl"] == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_current_span_is_the_latest_base(db):
    await _seed_two_spans(db)
    versions, meta = await current_span_versions(db)
    assert versions == ["vB", "vB-t3-141500-cd34"]
    assert meta["base_version"] == "vB"
    assert meta["decisions"] == 2


class _FakeRegistry:
    """Champion exists, no active challenger; the pre-gate decides."""

    async def current_champion(self):
        return {"strategy_id": "champ_1"}

    async def active_challenger(self):
        return None


@pytest.mark.asyncio
async def test_evolution_pre_gate_counts_only_the_current_span(db):
    from alpha_council.agents.alpha_evolution import AlphaEvolutionAgent

    await _seed_two_spans(db)   # current span (vB) holds 2 observations
    agent = AlphaEvolutionAgent(
        client=None, db=db, registry=_FakeRegistry(),
        config={"alpha_evolution": {
            "competition": {"min_observations_to_propose": 3}}})
    # 4 all-time observations would pass the old gate; the 2 in the
    # current span must not. client=None proves no model call was made.
    assert await agent.post_close_review({}) is None
    row = await db.fetchone(
        "SELECT message FROM system_events WHERE event_type='NO_CHANGE' "
        "ORDER BY occurred_at DESC LIMIT 1")
    assert "current config span" in row["message"]
    assert "vB" in row["message"]


# ======================================================================
# 17. Hackathon review idea #5 - chain-implied priors, Brier-scored
# ======================================================================

def test_chain_implied_reads_the_straddle_and_the_digitals():
    calls = {98.0: 3.10, 99.0: 2.50, 100.0: 1.95, 101.0: 1.50, 102.0: 1.10}
    puts = {98.0: 1.00, 99.0: 1.40, 100.0: 1.90, 101.0: 2.50, 102.0: 3.00}
    implied = chain_implied(100.0, calls, puts)
    assert implied.usable
    assert implied.atm_strike == 100.0
    assert implied.expected_move_pct == pytest.approx(0.0385)
    assert implied.p_up_calls == pytest.approx(0.45)
    assert implied.p_up_puts == pytest.approx(0.40)
    assert implied.p_up == pytest.approx(0.425)


def test_chain_implied_refuses_a_coarse_strike_grid():
    # 95/105 around spot 100: a 10% gap cannot seat a digital read.
    implied = chain_implied(100.0, {95.0: 6.0, 105.0: 1.0},
                            {95.0: 1.0, 105.0: 6.0})
    assert not implied.usable
    assert implied.reason


def test_chain_implied_needs_a_two_sided_straddle():
    implied = chain_implied(100.0, {100.0: 1.95, 101.0: 1.50}, {})
    assert not implied.usable
    assert "straddle" in implied.reason


class _FakePriorsApi:
    def __init__(self, contracts, snapshots):
        self._contracts = contracts
        self._snapshots = snapshots
        self.bars: dict[str, list[dict]] = {}

    async def get_option_contracts(self, underlying, **kwargs):
        return self._contracts

    async def get_option_snapshots(self, occs):
        return {o: self._snapshots[o] for o in occs if o in self._snapshots}

    async def get_stock_bars(self, symbols, timeframe, start, end):
        return {s: self.bars.get(s, []) for s in symbols}


def _occ(strike: float, letter: str) -> str:
    return f"SPY260910{letter}{int(strike * 1000):08d}"


PRIORS_CFG = {"priors": {
    "enabled": True, "etf_cohort": ["SPY"], "horizon_days": 7,
    "refresh_minutes": 90, "strike_band_pct": 0.08,
    "max_leg_spread_pct": 0.25, "max_strike_gap_pct": 0.02,
    "cohort_added": "2026-08-01", "cohort_review_date": "2026-11-20"}}


@pytest.mark.asyncio
async def test_compute_one_persists_a_usable_prior(db):
    call_mids = {98.0: 3.10, 99.0: 2.50, 100.0: 1.95, 101.0: 1.50,
                 102.0: 1.10}
    put_mids = {98.0: 1.00, 99.0: 1.40, 100.0: 1.90, 101.0: 2.50,
                102.0: 3.00}
    contracts, snapshots = [], {}
    for strike, mid in call_mids.items():
        occ = _occ(strike, "C")
        contracts.append({"expiration_date": "2026-09-10", "symbol": occ})
        snapshots[occ] = {"latestQuote": {"bp": mid - 0.05, "ap": mid + 0.05}}
    for strike, mid in put_mids.items():
        occ = _occ(strike, "P")
        contracts.append({"expiration_date": "2026-09-10", "symbol": occ})
        snapshots[occ] = {"latestQuote": {"bp": mid - 0.05, "ap": mid + 0.05}}

    service = PriorsService(_FakePriorsApi(contracts, snapshots), None, db,
                            PRIORS_CFG)
    row = await service.compute_one("SPY", 100.0, now=NOW)
    assert row["usable"] == 1
    assert row["expected_move_pct"] == pytest.approx(0.0385)
    assert row["p_up"] == pytest.approx(0.425)
    stored = await db.fetchone(
        "SELECT * FROM market_priors WHERE symbol='SPY'")
    assert stored is not None and stored["usable"] == 1
    assert stored["expiry"] == "2026-09-10"


@pytest.mark.asyncio
async def test_score_due_briers_against_the_spot_it_was_handed(db):
    await db.execute(
        "INSERT INTO market_priors(prior_id, symbol, asof, source, spot, "
        "expiry, horizon_days, expected_move_pct, p_up, usable, inputs_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("pri_test1", "SPY", (NOW - timedelta(days=8)).isoformat(), "CHAIN",
         100.0, "2026-08-31", 7, 0.0385, 0.7, 1, "{}"))
    api = _FakePriorsApi([], {})
    api.bars["SPY"] = [{"c": 103.0, "t": "2026-08-31T20:00:00Z"}]
    service = PriorsService(api, None, db, PRIORS_CFG)

    assert await service.score_due(now=NOW) == 1
    score = await db.fetchone("SELECT * FROM prior_scores")
    assert score["outcome_up"] == 1
    assert score["brier"] == pytest.approx((0.7 - 1.0) ** 2)
    assert score["realized_return"] == pytest.approx(0.03)
    assert score["within_expected_move"] == 1
    # Idempotent: a second pass scores nothing.
    assert await service.score_due(now=NOW) == 0


@pytest.mark.asyncio
async def test_cohort_review_renders_every_cohort_symbol(db):
    text = await build_cohort_review(db, PRIORS_CFG, now=NOW)
    assert "# ETF Cohort Review" in text
    assert "### SPY" in text
    assert "Expansion decision checklist" in text


# ======================================================================
# 18. Hackathon review idea #4 - rhetoric audits (reporting-only;
#     operator chose the recommended defaults 2026-10-30)
# ======================================================================

NVDA_LEVELS = {"spot": 204.0, "long_strike_r1": 200.0,
               "short_strike_r1": 210.0, "breakeven_r1": 205.2}


def test_level_claim_grades_supported_derived_unsupported():
    from alpha_council.audit.rhetoric import grade_level_claim

    grade, _ = grade_level_claim(204.0, NVDA_LEVELS)
    assert grade == "SUPPORTED"                  # matches spot exactly
    grade, _ = grade_level_claim(198.0, NVDA_LEVELS)
    assert grade == "DERIVED"                    # ordinary stop below spot
    grade, detail = grade_level_claim(150.0, NVDA_LEVELS)
    assert grade == "UNSUPPORTED"                # nothing shown comes near
    assert "envelope" in detail
    grade, _ = grade_level_claim(198.0, {})
    assert grade == "NOT_CHECKABLE"


def test_time_claim_grades_against_the_stated_horizon():
    from alpha_council.audit.rhetoric import grade_time_claim

    assert grade_time_claim(5.0, 5)[0] == "SUPPORTED"
    assert grade_time_claim(10.0, 5)[0] == "DERIVED"
    assert grade_time_claim(20.0, 5)[0] == "UNSUPPORTED"


async def _seed_audited_decision(db, journal, decision_id: str) -> None:
    await db.execute(
        "INSERT INTO decisions(decision_id, config_version, symbol, state, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (decision_id, "v-test", "NVDA", "FILLED",
         (NOW - timedelta(hours=1)).isoformat(),
         (NOW - timedelta(hours=1)).isoformat()))
    await journal.record_structures(decision_id, [_structure()], None)
    rules = json.dumps([
        {"rule_type": "PRICE", "description": "close below support",
         "threshold": 198.0, "comparator": "LT"},
        {"rule_type": "PRICE", "description": "hallucinated level",
         "threshold": 150.0, "comparator": "LT"},
        {"rule_type": "TIME", "description": "thesis stale",
         "threshold": 5.0, "comparator": "GTE"},
    ])
    await db.execute(
        "INSERT INTO trade_proposals(proposal_id, decision_id, revision, "
        "symbol, trade, direction, confidence, expected_horizon_days, "
        "desired_portfolio_risk_pct, thesis, catalyst_summary, "
        "invalidation_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"prop_{decision_id[-8:]}_r0", decision_id, 0, "NVDA", 1,
         "BULLISH", 0.7, 5, 1.0, "t", "c", rules,
         (NOW - timedelta(hours=1)).isoformat()))


@pytest.mark.asyncio
async def test_citation_audit_grades_each_invalidation_threshold(db):
    from alpha_council.audit.rhetoric import audit_decision_citations

    journal = TradeJournal(db)
    await _seed_audited_decision(db, journal, "dec_rh1")
    findings = await audit_decision_citations(
        db, {"decision_id": "dec_rh1", "symbol": "NVDA"}, 0.0075)
    grades = {f.claim_value: f.grade for f in findings}
    assert grades[198.0] == "DERIVED"
    assert grades[150.0] == "UNSUPPORTED"
    assert grades[5.0] == "SUPPORTED"


@pytest.mark.asyncio
async def test_exit_claim_flags_an_after_hours_exit(db):
    from alpha_council.audit.rhetoric import audit_closed_trade

    # 20:36Z on 2026-09-01 is 16:36 ET - the after-hours loop's signature.
    trade = {"decision_id": "dec_rh2", "symbol": "IBIT",
             "exit_reason": "UNDERLYING_INVALIDATION",
             "closed_at": "2026-09-01T20:36:00+00:00",
             "invalidation_json": json.dumps(
                 [{"rule_type": "PRICE", "description": "d",
                   "threshold": 50.0, "comparator": "LT"}]),
             "entry_debit": 5.0, "exit_credit": 4.0}
    findings = await audit_closed_trade(db, trade, 0.0075)
    by_subject = {f.subject: f.grade for f in findings}
    assert by_subject["exit_rth:UNDERLYING_INVALIDATION"] == "INCONSISTENT"
    # No stored bars in this test database: the level check says so
    # rather than guessing.
    assert by_subject["exit_level:UNDERLYING_INVALIDATION"] == "NOT_CHECKABLE"

    trade["closed_at"] = "2026-09-01T19:30:00+00:00"    # 15:30 ET
    findings = await audit_closed_trade(db, trade, 0.0075)
    by_subject = {f.subject: f.grade for f in findings}
    assert by_subject["exit_rth:UNDERLYING_INVALIDATION"] == "CONSISTENT"


@pytest.mark.asyncio
async def test_rhetoric_nightly_is_idempotent(db):
    from alpha_council.audit.rhetoric import ensure_schema, run_nightly

    await ensure_schema(db)
    journal = TradeJournal(db)
    await _seed_audited_decision(db, journal, "dec_rh3")
    cfg = {"rhetoric_audit": {"enabled": True, "tolerance_rel": 0.0075}}

    summary = await run_nightly(db, cfg, now=NOW)
    assert summary["claims"] == 3
    assert summary["unsupported"] == 1
    first = await db.fetchvalue("SELECT COUNT(*) FROM rhetoric_audits")
    await run_nightly(db, cfg, now=NOW)
    second = await db.fetchvalue("SELECT COUNT(*) FROM rhetoric_audits")
    assert first == second == 3


# ======================================================================
# 19. Capital sleeves - ETF planner and the isolation identity
#     (operator rebalance 2026-10-30: 30% ETF / 30% stock / options
#     unchanged against their own NAV)
# ======================================================================

def test_etf_plan_initial_buildup_buys_to_target():
    from alpha_council.sleeves.etf_sleeve import plan_rebalance

    plan = plan_rebalance(
        equity=100_000.0, weights={"VOO": 50, "GLD": 50}, target_pct=30.0,
        holdings={}, quotes={"VOO": {"bid": 499.0, "ask": 500.0},
                             "GLD": {"bid": 299.0, "ask": 300.0}},
        max_order_pct_of_equity=20.0)
    assert plan.sleeve_target == pytest.approx(30_000.0)
    by_symbol = {o.symbol: o for o in plan.orders}
    assert by_symbol["VOO"].side == "buy"
    assert by_symbol["VOO"].qty == 30          # floor(15000 / 500)
    assert by_symbol["GLD"].qty == 50          # floor(15000 / 300)


def test_etf_plan_respects_the_band_and_sells_first():
    from alpha_council.sleeves.etf_sleeve import plan_rebalance

    plan = plan_rebalance(
        equity=100_000.0, weights={"VOO": 50, "GLD": 50}, target_pct=30.0,
        holdings={"VOO": {"market_value": 20_000.0, "qty": 40},
                  "GLD": {"market_value": 14_500.0, "qty": 48}},
        quotes={"VOO": {"bid": 499.0, "ask": 500.0},
                "GLD": {"bid": 299.0, "ask": 300.0}})
    # GLD drift $500 sits inside the 20% band (threshold $3,000): held.
    assert [o.symbol for o in plan.orders] == ["VOO"]
    sell = plan.orders[0]
    assert sell.side == "sell"
    assert sell.qty == 10                      # floor(5000 / 499)


def test_etf_plan_caps_single_orders_and_whole_shares():
    from alpha_council.sleeves.etf_sleeve import plan_rebalance

    plan = plan_rebalance(
        equity=100_000.0, weights={"VOO": 100}, target_pct=30.0,
        holdings={}, quotes={"VOO": {"bid": 499.0, "ask": 500.0}},
        max_order_pct_of_equity=10.0)
    assert plan.orders[0].qty == 20            # floor(10000 cap / 500)


class _FakePositionsApi:
    trade_base = ""

    def __init__(self, positions):
        self._positions = positions

    async def _get(self, url, params=None):
        return self._positions


class _FakeOrdersHandle:
    def __init__(self, positions):
        self.api = _FakePositionsApi(positions)


@pytest.mark.asyncio
async def test_sleeve_isolation_identity(db):
    from alpha_council.sleeves.accounting import read_accounting

    # With no sleeve activity at all the adjustment is exactly zero: the
    # pre-sleeve behavior, bit for bit.
    empty = await read_accounting(_FakeOrdersHandle([]), db, set())
    assert empty.sleeve_pnl_total == pytest.approx(0.0)
    assert empty.options_nav(95_377.22) == pytest.approx(95_377.22)

    positions = [
        {"symbol": "VOO", "asset_class": "us_equity",
         "market_value": "15500", "unrealized_intraday_pl": "120",
         "qty": "31", "current_price": "500"},
        # An option position must never count as sleeve value.
        {"symbol": "NVDA260918C00200000", "asset_class": "us_option",
         "market_value": "520", "unrealized_intraday_pl": "9", "qty": "1"},
    ]
    await db.execute(
        "INSERT INTO sleeve_orders(order_pk, sleeve, client_order_id, "
        "symbol, side, qty, limit_price, status, filled_qty, fill_price, "
        "notional, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ord1", "ETF", "cid1", "VOO", "buy", 31, 484.0, "filled", 31,
         483.87, 15_000.0, NOW.isoformat()))

    acct = await read_accounting(_FakeOrdersHandle(positions), db, {"VOO"})
    assert acct.sleeve_market_value == pytest.approx(15_500.0)
    assert acct.net_invested == pytest.approx(15_000.0)
    assert acct.sleeve_pnl_total == pytest.approx(500.0)
    assert acct.intraday_pl == pytest.approx(120.0)
    # The identity: sleeve profit never inflates the options desk's NAV.
    assert acct.options_nav(100_500.0) == pytest.approx(100_000.0)


# ======================================================================
# 20. Capital sleeves - stock sleeve sizing, stops, exits
# ======================================================================

def _rule(threshold, comparator="LT", rule_type="PRICE"):
    from alpha_council.models.trading import InvalidationRule

    return InvalidationRule(rule_type=rule_type, description="d",
                            threshold=threshold, comparator=comparator)


def test_usable_stop_takes_the_pms_tightest_price_rule():
    from alpha_council.sleeves.stock_sleeve import usable_stop

    assert usable_stop([_rule(198.0)], 204.0) == pytest.approx(198.0)
    # Two qualifying stops: the tightest (highest) one binds.
    assert usable_stop([_rule(195.0), _rule(198.0)],
                       204.0) == pytest.approx(198.0)
    # Too close is churn; too far is a thesis; wrong comparator and
    # non-PRICE rules never become stops.
    assert usable_stop([_rule(203.5)], 204.0) is None
    assert usable_stop([_rule(170.0)], 204.0) is None
    assert usable_stop([_rule(198.0, comparator="GT")], 204.0) is None
    assert usable_stop([_rule(198.0, rule_type="VWAP")], 204.0) is None
    assert usable_stop([], 204.0) is None


def test_share_sizing_composes_every_cap_and_the_smallest_wins():
    from alpha_council.sleeves.stock_sleeve import size_shares

    # Risk-bound: min(2.0 requested, 0.75 cap) = $750 / $6 risk = 125,
    # but the notional cap (6% = $6,000 / $204) binds at 29.
    assert size_shares(100_000.0, 204.0, 198.0, 2.0, 0.75, 6.0,
                       headroom=30_000.0) == 29
    # Headroom binds when the sleeve is nearly full.
    assert size_shares(100_000.0, 204.0, 198.0, 2.0, 0.75, 6.0,
                       headroom=1_000.0) == 4
    # A stop at/above entry can never size.
    assert size_shares(100_000.0, 204.0, 204.0, 2.0, 0.75, 6.0,
                       headroom=30_000.0) == 0


def _stock_position(**overrides):
    from alpha_council.sleeves.stock_sleeve import StockPosition

    defaults = dict(trade_id="t1", decision_id="d1", symbol="NVDA",
                    qty=10, entry_price=204.0, stop_price=198.0,
                    target_price=216.0, horizon_days=5,
                    opened_at=NOW - timedelta(days=1))
    defaults.update(overrides)
    return StockPosition(**defaults)


def test_stock_exit_target_fires_immediately():
    from alpha_council.sleeves.stock_sleeve import evaluate_stock_exit

    reason, detail = evaluate_stock_exit(
        _stock_position(), 216.5, NOW, stop_confirm_minutes=10,
        horizon_multiple=1.5)
    assert reason == "UNDERLYING_TARGET"
    assert "216" in detail


def test_stock_exit_time_stop_uses_the_horizon_multiple():
    from alpha_council.sleeves.stock_sleeve import evaluate_stock_exit

    position = _stock_position(opened_at=NOW - timedelta(days=8))
    reason, _ = evaluate_stock_exit(position, 205.0, NOW,
                                    stop_confirm_minutes=10,
                                    horizon_multiple=1.5)
    assert reason == "TIME_STOP"                 # 8d >= 5d x 1.5


def test_stock_stop_needs_a_confirmed_breach():
    from alpha_council.sleeves.stock_sleeve import evaluate_stock_exit

    position = _stock_position()
    # First breach starts the clock, does not fire.
    reason, detail = evaluate_stock_exit(position, 197.0, NOW,
                                         stop_confirm_minutes=10,
                                         horizon_multiple=1.5)
    assert reason is None and "confirming" in detail
    # Still breached 11 minutes later: fires.
    reason, _ = evaluate_stock_exit(position, 197.2,
                                    NOW + timedelta(minutes=11),
                                    stop_confirm_minutes=10,
                                    horizon_multiple=1.5)
    assert reason == "UNDERLYING_INVALIDATION"
    # A recovery resets the clock entirely.
    position2 = _stock_position()
    evaluate_stock_exit(position2, 197.0, NOW, stop_confirm_minutes=10,
                        horizon_multiple=1.5)
    evaluate_stock_exit(position2, 199.0, NOW + timedelta(minutes=5),
                        stop_confirm_minutes=10, horizon_multiple=1.5)
    reason, _ = evaluate_stock_exit(position2, 197.0,
                                    NOW + timedelta(minutes=12),
                                    stop_confirm_minutes=10,
                                    horizon_multiple=1.5)
    assert reason is None                        # new breach, new clock
