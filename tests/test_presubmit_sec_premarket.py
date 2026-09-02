"""
Alpha Council - tests for the §17.4 pre-submit refresh, the SEC EDGAR
collector, and the Pre-Market Strategist.

Run:
    uv run pytest tests/test_presubmit_sec_premarket.py -v
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from alpha_council.db.engine import Database
from alpha_council.execution.presubmit import PreSubmitRefresher
from alpha_council.intelligence.sec import (
    SECIntelligence,
    classify_form,
    item_strength,
)
from alpha_council.models.enums import StrategyType
from alpha_council.models.evolution import PreMarketBrief
from alpha_council.models.trading import OptionLeg, OptionStructure

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)

SCORING = {
    "equity": {"pre_submit_max_lag_seconds": 5.0},
    "options": {"fresh_quote_seconds": 60, "tick_size": 0.01,
                "indicative_buffer_min": 0.02, "indicative_buffer_pct": 0.05,
                "structures_returned": 5},
    "rate_limits": {"sec_requests_per_second": 100},
    "catalyst_weights": {"materiality": 0.30, "freshness": 0.20,
                         "source_reliability": 0.20,
                         "market_confirmation": 0.15, "surprise": 0.15},
    "source_base_reliability": {"government": 100},
}

TIER_CFG = {
    "long_delta": [0.52, 0.72], "short_delta": [0.22, 0.42],
    "long_delta_target": 0.60, "short_delta_target": 0.33,
    "max_cost_to_width": 0.55, "max_leg_spread_pct": 0.15,
}


def _leg(strike: float, delta: float, side: str = "BUY") -> OptionLeg:
    return OptionLeg(
        symbol=f"NVDA260918C{int(strike * 1000):08d}",
        underlying="NVDA", expiration=EXP, option_type="CALL",
        strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure() -> OptionStructure:
    limit, width = 5.20, 10.0
    return OptionStructure(
        structure_id="st_orig", symbol="NVDA",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=2,
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
# §17.4 pre-submit refresh
# ======================================================================

class StubQuote:
    def __init__(self, mid: float | None):
        self._mid = mid

    def midpoint(self) -> float | None:
        return self._mid


class StubMarket:
    def __init__(self, quote):
        self._quote = quote

    async def fresh_quote(self, symbol, max_age):
        return self._quote


class StubApi:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def get_option_snapshots(self, symbols):
        return self._snapshots


def _snap(bid: float, ask: float, delta: float = 0.6) -> dict:
    return {"latestQuote": {"bp": bid, "ap": ask,
                            "t": "2026-09-01T15:00:00Z"},
            "greeks": {"delta": delta}}


@pytest.mark.asyncio
async def test_stale_underlying_blocks_submission():
    refresher = PreSubmitRefresher(StubApi({}), StubMarket(None), SCORING)
    result = await refresher.refresh(_structure(), TIER_CFG)
    assert not result.ok
    assert result.gate_id == "EXEC_STALE_PRESUBMIT"


@pytest.mark.asyncio
async def test_fresh_quotes_reprice_and_keep_identity():
    structure = _structure()
    api = StubApi({
        structure.long_leg.symbol: _snap(5.30, 5.50, 0.61),
        structure.short_leg.symbol: _snap(2.10, 2.24, 0.34),
    })
    refresher = PreSubmitRefresher(api, StubMarket(StubQuote(204.5)),
                                   SCORING)
    result = await refresher.refresh(structure, TIER_CFG)
    assert result.ok
    repriced = result.structure
    # Identity survives; prices are current.
    assert repriced.structure_id == "st_orig"
    assert repriced.rank == 2
    assert repriced.adjusted_mid_debit == pytest.approx(5.40 - 2.17, abs=0.01)
    assert repriced.natural_debit == pytest.approx(5.50 - 2.10, abs=0.01)
    assert repriced.initial_limit_debit <= repriced.natural_debit
    assert result.underlying_price == pytest.approx(204.5)


@pytest.mark.asyncio
async def test_crossed_leg_quote_fails_reprice():
    structure = _structure()
    api = StubApi({
        structure.long_leg.symbol: _snap(5.50, 5.30),  # crossed
        structure.short_leg.symbol: _snap(2.10, 2.24),
    })
    refresher = PreSubmitRefresher(api, StubMarket(StubQuote(204.5)),
                                   SCORING)
    result = await refresher.refresh(structure, TIER_CFG)
    assert not result.ok
    assert result.gate_id == "EXEC_REPRICE_FAILED"


@pytest.mark.asyncio
async def test_degraded_structure_is_rejected_not_submitted():
    """A spread whose current cost/width breaches the tier gate does not
    ride through on its stale approval."""
    structure = _structure()
    api = StubApi({
        # Long got much more expensive: debit near 6.0 on a 10 width is
        # fine, so widen the spread% instead to trip the leg filter.
        structure.long_leg.symbol: _snap(4.00, 5.60, 0.61),
        structure.short_leg.symbol: _snap(2.10, 2.24, 0.34),
    })
    refresher = PreSubmitRefresher(api, StubMarket(StubQuote(204.5)),
                                   SCORING)
    result = await refresher.refresh(structure, TIER_CFG)
    assert not result.ok
    assert result.gate_id == "EXEC_REPRICE_FAILED"


# ======================================================================
# SEC EDGAR collector
# ======================================================================

def test_classify_priority_forms():
    assert classify_form("8-K")[0] == "8-K"
    assert classify_form("10-Q")[0] == "10-Q"
    assert classify_form("SC 13D")[0] == "SC 13D"
    assert classify_form("424B5")[0] == "424B5"
    assert classify_form("11-K") is None      # not a priority form
    assert classify_form("") is None


def test_item_strength_ranks_8k_items():
    assert item_strength("2.02,9.01") == 1.0      # earnings results
    assert item_strength("7.01") == 0.15          # Reg FD only: routine PR
    assert item_strength("") == 0.4               # unknown -> conservative


def test_routine_filings_stay_below_the_material_floor():
    """Measured on the first live sweep: 493 routine 424B2 supplements
    averaged catalyst 57 under the original bands and would have flooded
    the EVENT track. Routine paper is context, not catalyst."""
    from alpha_council.models.enums import Direction
    from alpha_council.models.intelligence import IntelligenceEvent
    from alpha_council.quant.scoring import summarize_intel

    routine = IntelligenceEvent(
        event_id="e1", item_id="i1", symbol="JPM",
        event_type="sec_424b2", direction=Direction.NEUTRAL,
        direction_confidence=0.0, source_reliability_score=100.0,
        freshness_score=100.0, novelty_score=100.0,
        corroboration_score=100.0,
        materiality_score=27.0,          # 424B band midpoint
        surprise_score=50.0, market_confirmation_score=50.0,
        catalyst_score=63.0,             # fresh + reliable floors it high
        created_at=NOW)
    summary = summarize_intel([routine])
    assert not summary.has_material_catalyst

    results_8k = routine.model_copy(update={
        "event_type": "sec_8-k", "materiality_score": 95.0,
        "catalyst_score": 83.0})
    summary = summarize_intel([results_8k])
    assert summary.has_material_catalyst


class StubSEC(SECIntelligence):
    """Overrides transport with canned payloads."""

    def __init__(self, db, payloads):
        super().__init__(db, SCORING, "Test test@example.com",
                         cooldown_seconds=0.0)
        self._payloads = payloads

    async def _get_json(self, url):
        for key, payload in self._payloads.items():
            if key in url:
                return payload
        raise RuntimeError(f"no stub for {url}")


def _submissions(now: datetime) -> dict:
    accepted = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"filings": {"recent": {
        "form": ["8-K", "11-K", "10-Q"],
        "accessionNumber": ["0001-26-000001", "0001-26-000002",
                            "0001-26-000003"],
        "acceptanceDateTime": [accepted, accepted, old],
        "items": ["2.02,9.01", "", ""],
        "primaryDocument": ["doc1.htm", "doc2.htm", "doc3.htm"],
    }}}


@pytest.mark.asyncio
async def test_sec_collect_scores_priority_filings_and_dedupes(db):
    payloads = {
        "company_tickers": {"0": {"ticker": "NVDA", "cik_str": 1045810}},
        "submissions/CIK": _submissions(NOW),
    }
    sec = StubSEC(db, payloads)
    events = await sec.collect(["NVDA"], lookback_hours=24,
                               price_returns={"NVDA": 0.02}, now=NOW)
    assert "NVDA" in events
    assert len(events["NVDA"]) == 1               # 8-K in window only
    event = events["NVDA"][0]
    assert event.event_type == "sec_8-k"
    assert str(event.direction) == "BULLISH"      # from the +2% tape
    assert event.materiality_score >= 88          # results 8-K, top of band
    assert event.source_reliability_score == 100

    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM intelligence_events WHERE symbol='NVDA'")
    assert row["n"] == 1

    # Second sweep: the accession is known, nothing duplicates.
    again = await sec.collect(["NVDA"], lookback_hours=24,
                              price_returns={"NVDA": 0.02}, now=NOW)
    assert again == {}
    assert sec.stats.skipped_known == 1


@pytest.mark.asyncio
async def test_sec_collector_fails_open(db):
    sec = StubSEC(db, {})          # every request raises
    events = await sec.collect(["NVDA"], now=NOW)
    assert events == {}            # degraded, never raised


# ======================================================================
# event calendar - earnings blackouts span into the next session (§16.4)
# ======================================================================

def test_earnings_blackout_covers_the_gap_open():
    """The spec blocks from T-30m through the first 10 minutes of the
    FOLLOWING session. The live calendar must encode that, or the system
    buys the reporter's gap open the morning after."""
    from zoneinfo import ZoneInfo

    from alpha_council.risk.constitution import load_blackouts
    from alpha_council.settings import load_yaml

    ET = ZoneInfo("America/New_York")
    windows = load_blackouts(load_yaml("event_calendar"))
    avgo = [w for w in windows if "AVGO" in w.symbols]
    assert avgo, "AVGO earnings window missing from event_calendar.yaml"
    window = avgo[0]

    pre = datetime(2026, 9, 2, 15, 55, tzinfo=ET)       # T-25m
    gap_open = datetime(2026, 9, 3, 9, 35, tzinfo=ET)   # next morning
    released = datetime(2026, 9, 3, 9, 45, tzinfo=ET)   # past 09:40
    assert window.blocks(pre, "AVGO")
    assert window.blocks(gap_open, "AVGO")
    assert not window.blocks(released, "AVGO")
    # Scoped: other symbols trade freely through AVGO's window.
    assert not window.blocks(gap_open, "NVDA")


# ======================================================================
# pre-market brief model
# ======================================================================

def test_premarket_brief_context_rendering():
    brief = PreMarketBrief(
        session_date="2026-09-02", generated_at=NOW,
        regime_summary="Benchmarks modestly higher overnight on light "
                       "volume.",
        session_bias="RISK_ON",
        important_themes=["semis strong"],
        risk_windows=["ISM at 10:00 ET"],
        confidence=0.55)
    text = brief.as_context()
    assert "RISK_ON" in text
    assert "context only" in text
    assert "ISM at 10:00 ET" in text
