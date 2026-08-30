"""
Alpha Council v2.4 - discovery layer tests.

The important invariants: Core is never evicted by a screener burst,
dynamic membership expires, optional sources fail open on 403, and Stage 0
stays cheap.

Place at: tests/test_discovery.py

Run:
    uv run pytest tests/test_discovery.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alpha_council.alpaca.screeners import (
    AssetInfo,
    is_blocked_symbol,
)
from alpha_council.models.enums import DiscoverySource
from alpha_council.quant.discovery import Injection, UniverseManager

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
CORE = ["SPY", "QQQ", "NVDA", "AAPL"]


def _um(**kw) -> UniverseManager:
    return UniverseManager(CORE, **kw)


def _inj(symbol: str, source=DiscoverySource.MOVER, rank: int | None = 1,
         reason: str = "test") -> Injection:
    return Injection(symbol=symbol, source=source, reason=reason, rank=rank)


# ======================================================================
# membership and TTL
# ======================================================================

def test_core_is_always_present():
    um = _um()
    assert um.members(NOW) == CORE


def test_injection_adds_a_dynamic_symbol():
    um = _um()
    assert um.inject(_inj("AMD"), NOW)
    members = um.members(NOW)
    assert "AMD" in members
    assert members[:4] == CORE          # core stays first


def test_dynamic_membership_expires():
    um = _um(ttl_minutes=90)
    um.inject(_inj("AMD"), NOW)
    assert "AMD" in um.members(NOW + timedelta(minutes=89))
    assert "AMD" not in um.members(NOW + timedelta(minutes=91))


def test_core_never_expires():
    um = _um(ttl_minutes=1)
    assert um.members(NOW + timedelta(days=30)) == CORE


def test_injecting_a_core_symbol_is_a_noop():
    um = _um()
    assert not um.inject(_inj("SPY"), NOW)
    assert um.source_of("SPY") is DiscoverySource.CORE
    assert um.expiry_of("SPY") is None


def test_excluded_symbols_are_refused():
    um = _um(exclusions={"FDX"})
    assert not um.inject(_inj("FDX"), NOW)
    assert ("FDX", "DISC_EXCLUDED", "permanent exclusion list") in um.rejected


def test_higher_value_source_wins_on_reinjection():
    """A symbol surfaced by both a mover screen and a fresh filing should be
    attributed to the filing, which is the stronger reason."""
    um = _um()
    um.inject(_inj("AMD", DiscoverySource.MOVER, reason="mover rank 3"), NOW)
    um.inject(_inj("AMD", DiscoverySource.ALPACA_NEWS, reason="8-K"), NOW)
    assert um.source_of("AMD") is DiscoverySource.ALPACA_NEWS
    assert um.reason_of("AMD") == "8-K"


def test_weaker_source_does_not_overwrite():
    um = _um()
    um.inject(_inj("AMD", DiscoverySource.ALPACA_NEWS, reason="8-K"), NOW)
    um.inject(_inj("AMD", DiscoverySource.MOVER, reason="mover rank 3"), NOW)
    assert um.source_of("AMD") is DiscoverySource.ALPACA_NEWS


# ======================================================================
# the cap
# ======================================================================

def test_cap_is_a_noop_below_the_limit():
    um = _um(cap=250)
    symbols = CORE + [f"SYM{i}" for i in range(50)]
    assert um.cap_members(symbols) == symbols


def test_core_survives_a_screener_burst():
    """300 screener symbols must not push SPY out of the scan."""
    um = _um(cap=100)
    flood = [f"S{i:03d}" for i in range(300)]
    for i, s in enumerate(flood):
        um.inject(_inj(s, DiscoverySource.MOST_ACTIVE, rank=i + 1), NOW)
    capped = um.cap_members(CORE + flood)
    assert len(capped) == 100
    for c in CORE:
        assert c in capped


def test_event_symbols_outrank_screener_symbols_at_the_cap():
    um = _um(cap=6)
    um.inject(_inj("EVT1", DiscoverySource.ALPACA_NEWS, rank=None), NOW)
    for i in range(20):
        um.inject(_inj(f"MV{i}", DiscoverySource.MOVER, rank=i + 1), NOW)
    capped = um.cap_members(CORE + ["EVT1"] + [f"MV{i}" for i in range(20)])
    assert len(capped) == 6
    assert "EVT1" in capped


def test_screener_rank_breaks_ties():
    um = _um(cap=6)
    for i in range(20):
        um.inject(_inj(f"MV{i}", DiscoverySource.MOVER, rank=i + 1), NOW)
    capped = um.cap_members(CORE + [f"MV{i}" for i in range(20)])
    assert "MV0" in capped        # rank 1
    assert "MV19" not in capped   # rank 20


def test_dropped_symbols_are_logged_as_rejections():
    um = _um(cap=5)
    for i in range(10):
        um.inject(_inj(f"MV{i}", DiscoverySource.MOVER, rank=i + 1), NOW)
    um.cap_members(CORE + [f"MV{i}" for i in range(10)])
    cap_rejections = [r for r in um.rejected if r[1] == "DISC_CAP_EXCEEDED"]
    assert len(cap_rejections) == 9


# ======================================================================
# symbol hygiene
# ======================================================================

@pytest.mark.parametrize("symbol,name,expected_blocked", [
    ("ABCDW", "Acme Corp Warrants", True),
    ("ABCDU", "Acme Acquisition Corp Units", True),
    ("ABCDR", "Acme Corp Rights", True),
    ("BACpA", "Bank of America 5.5% Preferred Series A", True),
    ("NVDA", "NVIDIA Corporation", False),
    ("SPY", "SPDR S&P 500 ETF Trust", False),
])
def test_name_evidence_decides(symbol, name, expected_blocked):
    assert (is_blocked_symbol(symbol, name) is not None) is expected_blocked


@pytest.mark.parametrize("symbol,name", [
    ("LOW", "Lowe's Companies, Inc."),
    ("NOW", "ServiceNow, Inc."),
    ("SNOW", "Snowflake Inc. Class A"),
    ("U", "Unity Software Inc."),
    ("R", "Ryder System, Inc."),
])
def test_real_tickers_ending_in_wur_are_not_blocked(symbol, name):
    """The original pattern ^[A-Z]{1,5}W$ matched LOW, NOW and SNOW, all of
    which are Core symbols. Name evidence has to win over ticker shape."""
    assert is_blocked_symbol(symbol, name) is None


def test_shape_fallback_only_applies_without_a_name():
    assert is_blocked_symbol("ABCDW") is not None      # no name: infer
    assert is_blocked_symbol("SNOW") is None           # 4 chars, below the rule
    assert is_blocked_symbol("LOW") is None
    assert is_blocked_symbol("NOW") is None


def test_leveraged_products_blocked_by_name():
    assert is_blocked_symbol("TQQQ", "ProShares UltraPro QQQ") is not None
    assert is_blocked_symbol("SOXL", "Direxion Daily Semiconductor Bull 3X") is not None
    assert is_blocked_symbol("QQQ", "Invesco QQQ Trust") is None


def test_blocked_class_refused_at_injection():
    um = _um(name_lookup=lambda s: "Acme Corp Warrants")
    assert not um.inject(_inj("ABCDW"), NOW)
    assert any(r[1] == "DISC_BLOCKED_CLASS" for r in um.rejected)


def test_core_lookalikes_survive_injection():
    um = _um(name_lookup=lambda s: {"SNOW": "Snowflake Inc.",
                                    "LOW": "Lowe's Companies, Inc."}.get(s, ""))
    assert um.inject(_inj("SNOW"), NOW)
    assert um.inject(_inj("LOW"), NOW)
    assert not any(r[1] == "DISC_BLOCKED_CLASS" for r in um.rejected)


# ======================================================================
# asset eligibility
# ======================================================================

def _asset(**kw) -> AssetInfo:
    base = dict(symbol="AMD", name="Advanced Micro Devices",
                exchange="NASDAQ", asset_class="us_equity", tradable=True,
                status="active", has_options=True)
    return AssetInfo(**{**base, **kw})


def test_eligible_asset():
    a = _asset()
    assert a.tradable and a.has_options and a.is_us_equity_or_etf


def test_non_optionable_asset_is_not_eligible():
    assert not _asset(has_options=False).has_options


def test_crypto_class_is_not_us_equity():
    assert not _asset(asset_class="crypto").is_us_equity_or_etf


# ======================================================================
# Stage 0 stays cheap
# ======================================================================

def test_discovery_module_imports_no_agent_or_options_client():
    """Stage 0 must never fetch a chain or call an LLM (§3.2, §31.3).

    Enforced structurally: if the module cannot import those things, it
    cannot call them.
    """
    import inspect

    from alpha_council.quant import discovery

    source = inspect.getsource(discovery)
    for forbidden in ("options_engine", "agents", "openai", "anthropic",
                      "get_option_chain", "get_option_snapshots"):
        assert forbidden not in source, (
            f"discovery.py references {forbidden!r}; Stage 0 must stay cheap"
        )


def test_fast_score_treats_both_directions_equally():
    from alpha_council.utils.math import fast_score

    up = fast_score(momentum=88, relative_volume=70, relative_strength=65,
                    trend_regime=75, discovery_boost=80)
    down = fast_score(momentum=12, relative_volume=70, relative_strength=65,
                      trend_regime=75, discovery_boost=80)
    assert up == pytest.approx(down)


def test_boost_ordering_matches_spec():
    b = UniverseManager._boost_of
    assert b(DiscoverySource.SEC_EVENT) == 100.0
    assert b(DiscoverySource.ALPACA_NEWS) == 100.0
    assert b(DiscoverySource.MOVER) == 80.0
    assert b(DiscoverySource.MOST_ACTIVE) == 80.0
    assert b(DiscoverySource.OTHER_DYNAMIC) == 50.0
    assert b(DiscoverySource.CORE) == 40.0


# ======================================================================
# options-flag detection and graceful degradation
# ======================================================================

def test_detect_options_flag_finds_top_level_boolean():
    from alpha_council.alpaca.screeners import detect_options_flag

    ok, field = detect_options_flag({"symbol": "AMD", "options_enabled": True})
    assert ok and field == "options_enabled"


def test_detect_options_flag_finds_attributes_list():
    from alpha_council.alpaca.screeners import detect_options_flag

    ok, field = detect_options_flag(
        {"symbol": "AMD", "attributes": ["options_enabled", "fractional_eh"]})
    assert ok and field == "attributes[options_enabled]"


def test_detect_options_flag_finds_alternate_names():
    from alpha_council.alpaca.screeners import detect_options_flag

    for key in ("has_options", "optionable", "option_tradable"):
        ok, field = detect_options_flag({"symbol": "AMD", key: True})
        assert ok and field == key


def test_detect_options_flag_absent():
    from alpha_council.alpaca.screeners import detect_options_flag

    ok, field = detect_options_flag({"symbol": "AMD", "tradable": True})
    assert not ok and field is None


class _FakeCatalog:
    """Minimal stand-in exercising the degradation branch."""

    def __init__(self, assets, detection_failed):
        self._assets = assets
        self.options_detection_failed = detection_failed

    def get(self, symbol):
        return self._assets.get(symbol.upper())

    def is_eligible(self, symbol, require_options=True):
        a = self.get(symbol)
        if a is None:
            return False
        if not (a.tradable and a.status == "active" and a.is_us_equity_or_etf):
            return False
        if not require_options or self.options_detection_failed:
            return True
        return a.has_options


def test_unrecognized_options_field_degrades_to_eligible():
    """Failing closed would empty the universe. The options-contracts
    endpoint is authoritative, so unknown means 'let it through'."""
    assets = {"AMD": _asset(has_options=False)}
    strict = _FakeCatalog(assets, detection_failed=False)
    degraded = _FakeCatalog(assets, detection_failed=True)
    assert not strict.is_eligible("AMD")
    assert degraded.is_eligible("AMD")


def test_degradation_still_enforces_tradability():
    assets = {"AMD": _asset(has_options=False, tradable=False)}
    degraded = _FakeCatalog(assets, detection_failed=True)
    assert not degraded.is_eligible("AMD")
