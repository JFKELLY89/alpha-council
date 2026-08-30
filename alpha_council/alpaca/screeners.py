"""
Alpha Council v2.4 - optional discovery screeners and the asset catalog.

The most-active and movers screeners run on real-time SIP data and are very
likely unavailable to a Basic-plan account. That is fine: they are OPTIONAL
discovery sources. A 403 disables the source for the session, logs once, and
the scan continues. They must never become execution dependencies (§9.2).

The asset catalog exists because eligibility needs `has_options` for up to
250 symbols. Querying /v2/assets/{symbol} per symbol would be 250 requests
against a 200/minute ceiling. One bulk fetch per session is 1 request.

Place at: alpha_council/alpaca/screeners.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from alpha_council.alpaca.rest_client import (
    AlpacaError,
    AlpacaRateLimited,
    AlpacaRestClient,
)
from alpha_council.db.engine import Database
from alpha_council.models.enums import DiscoveryDisableReason, DiscoverySource
from alpha_council.utils.time import iso_utc, to_et, utc_now


OPTIONS_FLAG_CANDIDATES = (
    "options_enabled", "option_enabled", "has_options", "optionable",
    "options_tradable", "option_tradable",
)


def detect_options_flag(asset: dict[str, Any]) -> tuple[bool, str | None]:
    """Find options eligibility wherever this API version happens to put it.

    Returns (enabled, field_name). Checks top-level booleans, then the
    attributes list, then a nested dict. Reporting which field matched lets
    the diagnostic confirm the shape instead of guessing a fourth time.
    """
    for key in OPTIONS_FLAG_CANDIDATES:
        if key in asset and bool(asset[key]):
            return True, key

    attrs = asset.get("attributes")
    if isinstance(attrs, list):
        for key in OPTIONS_FLAG_CANDIDATES:
            if key in attrs:
                return True, f"attributes[{key}]"
    elif isinstance(attrs, dict):
        for key in OPTIONS_FLAG_CANDIDATES:
            if bool(attrs.get(key)):
                return True, f"attributes.{key}"
    return False, None


@dataclass(slots=True)
class AssetInfo:
    symbol: str
    name: str
    exchange: str
    asset_class: str
    tradable: bool
    status: str
    has_options: bool
    fractionable: bool = False
    easy_to_borrow: bool = False

    @property
    def is_us_equity_or_etf(self) -> bool:
        return self.asset_class == "us_equity"


class AssetCatalog:
    """Session-scoped cache of tradable, option-enabled US equities."""

    def __init__(self, api: AlpacaRestClient):
        self.api = api
        self._assets: dict[str, AssetInfo] = {}
        self.loaded_at: datetime | None = None
        self.load_error: str | None = None
        self.options_field_found: str | None = None
        self.options_detection_failed: bool = False

    async def load(self, force: bool = False) -> int:
        if self._assets and not force:
            return len(self._assets)
        try:
            payload = await self.api._get(
                f"{self.api.trade_base}/v2/assets",
                {"status": "active", "asset_class": "us_equity"},
            )
        except AlpacaError as exc:
            self.load_error = str(exc)
            return 0

        rows = payload if isinstance(payload, list) else payload.get("assets", [])
        for a in rows:
            sym = (a.get("symbol") or "").upper()
            if not sym:
                continue
            has_opts, field = detect_options_flag(a)
            if has_opts and self.options_field_found is None:
                self.options_field_found = field
            self._assets[sym] = AssetInfo(
                symbol=sym,
                name=a.get("name", ""),
                exchange=a.get("exchange", ""),
                asset_class=a.get("class", "us_equity"),
                tradable=bool(a.get("tradable")),
                status=a.get("status", ""),
                has_options=has_opts,
                fractionable=bool(a.get("fractionable")),
                easy_to_borrow=bool(a.get("easy_to_borrow")),
            )
        self.loaded_at = utc_now()

        # If not one asset reports options eligibility, the field name in this
        # API version is one we do not recognize. Failing closed would make
        # every symbol ineligible and empty the universe, so the catalog
        # degrades to "unknown" and defers to the options-contracts endpoint,
        # which is authoritative anyway.
        self.options_detection_failed = (
            len(self._assets) > 0 and self.options_enabled_count == 0
        )
        return len(self._assets)

    def get(self, symbol: str) -> AssetInfo | None:
        return self._assets.get(symbol.upper())

    def is_eligible(self, symbol: str, require_options: bool = True) -> bool:
        a = self.get(symbol)
        if a is None:
            return False
        if not (a.tradable and a.status == "active" and a.is_us_equity_or_etf):
            return False
        if not require_options or self.options_detection_failed:
            return True
        return a.has_options

    def optionable_symbols(self) -> set[str]:
        return {s for s, a in self._assets.items()
                if a.tradable and a.has_options and a.is_us_equity_or_etf}

    @property
    def size(self) -> int:
        return len(self._assets)

    @property
    def options_enabled_count(self) -> int:
        return sum(1 for a in self._assets.values() if a.has_options)


@dataclass(slots=True)
class ScreenerResult:
    source: DiscoverySource
    symbols: list[tuple[str, int]] = field(default_factory=list)  # (symbol, rank)
    available: bool = True
    disable_reason: DiscoveryDisableReason | None = None
    error: str | None = None
    fetched_at: datetime | None = None

    @property
    def count(self) -> int:
        return len(self.symbols)


class ScreenerService:
    """Optional discovery sources. Every failure path returns, never raises.

    That is the whole contract: a screener outage degrades breadth, not the
    trading system.
    """

    MOST_ACTIVES_URL = "/v1beta1/screener/stocks/most-actives"
    MOVERS_URL = "/v1beta1/screener/stocks/movers"

    def __init__(self, api: AlpacaRestClient, db: Database | None = None):
        self.api = api
        self.db = db
        self._disabled: dict[DiscoverySource, DiscoveryDisableReason] = {}
        self._probed: set[DiscoverySource] = set()
        self.contributions: dict[DiscoverySource, int] = {}

    # ---- state ----------------------------------------------------

    def is_enabled(self, source: DiscoverySource) -> bool:
        return source not in self._disabled

    async def _disable(self, source: DiscoverySource,
                       reason: DiscoveryDisableReason, detail: str = "") -> None:
        if source in self._disabled:
            return
        self._disabled[source] = reason
        if self.db is not None:
            await self.db.log_event(
                "WARN", "screeners", "DISCOVERY_SOURCE_FORBIDDEN"
                if reason is DiscoveryDisableReason.FORBIDDEN_403
                else "DISCOVERY_SOURCE_DISABLED",
                f"{source} disabled for the session: {reason}",
                {"source": str(source), "reason": str(reason), "detail": detail[:300]},
            )
            await self.db.execute(
                "INSERT OR REPLACE INTO discovery_source_status("
                "status_id, session_date, source, enabled, probed_at, "
                "disabled_at, disable_reason, symbols_contributed, "
                "consecutive_errors) VALUES(?,?,?,0,?,?,?,?,0)",
                (f"{to_et(utc_now()).date()}_{source}",
                 str(to_et(utc_now()).date()), str(source),
                 iso_utc(), iso_utc(), str(reason),
                 self.contributions.get(source, 0)),
            )

    # ---- fetch ----------------------------------------------------

    async def most_actives(self, top: int = 100) -> ScreenerResult:
        source = DiscoverySource.MOST_ACTIVE
        if not self.is_enabled(source):
            return ScreenerResult(source=source, available=False,
                                  disable_reason=self._disabled[source])
        try:
            payload = await self.api._get(
                f"{self.api.data_base}{self.MOST_ACTIVES_URL}",
                {"by": "volume", "top": top},
            )
        except AlpacaError as exc:
            return await self._handle_error(source, exc)

        rows = payload.get("most_actives", []) or []
        symbols = [((r.get("symbol") or "").upper(), i + 1)
                   for i, r in enumerate(rows) if r.get("symbol")]
        self.contributions[source] = len(symbols)
        return ScreenerResult(source=source, symbols=symbols,
                              fetched_at=utc_now())

    async def movers(self, top: int = 50) -> ScreenerResult:
        source = DiscoverySource.MOVER
        if not self.is_enabled(source):
            return ScreenerResult(source=source, available=False,
                                  disable_reason=self._disabled[source])
        try:
            payload = await self.api._get(
                f"{self.api.data_base}{self.MOVERS_URL}", {"top": top})
        except AlpacaError as exc:
            return await self._handle_error(source, exc)

        symbols: list[tuple[str, int]] = []
        for key in ("gainers", "losers"):
            for i, r in enumerate(payload.get(key, []) or []):
                sym = (r.get("symbol") or "").upper()
                if sym:
                    symbols.append((sym, i + 1))
        self.contributions[source] = len(symbols)
        return ScreenerResult(source=source, symbols=symbols,
                              fetched_at=utc_now())

    async def _handle_error(self, source: DiscoverySource,
                            exc: AlpacaError) -> ScreenerResult:
        if isinstance(exc, AlpacaRateLimited):
            return ScreenerResult(source=source, available=False,
                                  error="rate limited")
        if exc.status in (401, 403):
            await self._disable(source, DiscoveryDisableReason.FORBIDDEN_403,
                                exc.body)
            return ScreenerResult(
                source=source, available=False,
                disable_reason=DiscoveryDisableReason.FORBIDDEN_403,
                error=f"HTTP {exc.status}")
        return ScreenerResult(source=source, available=False,
                              error=f"HTTP {exc.status}")

    async def probe_entitlements(self) -> dict[str, Any]:
        """Called once per session. Establishes which screeners are usable."""
        active = await self.most_actives(top=5)
        mover = await self.movers(top=5)
        return {
            "most_active": {
                "available": active.available,
                "count": active.count,
                "reason": str(active.disable_reason) if active.disable_reason else None,
                "error": active.error,
            },
            "movers": {
                "available": mover.available,
                "count": mover.count,
                "reason": str(mover.disable_reason) if mover.disable_reason else None,
                "error": mover.error,
            },
        }

    def status_summary(self) -> dict[str, Any]:
        return {
            "enabled": [str(s) for s in
                        (DiscoverySource.MOST_ACTIVE, DiscoverySource.MOVER)
                        if self.is_enabled(s)],
            "disabled": {str(s): str(r) for s, r in self._disabled.items()},
            "contributions": {str(s): n for s, n in self.contributions.items()},
        }


# ----------------------------------------------------------------------
# symbol hygiene
# ----------------------------------------------------------------------

# Symbol shape alone is NOT a reliable warrant/unit/rights signal.
# The original pattern ^[A-Z]{1,5}W$ matched LOW, NOW, and SNOW, all of
# which are Core symbols. Name evidence is authoritative; the shape rule is
# only a fallback for symbols whose name the catalog does not carry, and it
# requires the 5-character Nasdaq convention.
INSTRUMENT_NAME_KEYWORDS = (
    (" WARRANT", "warrants"),
    ("WARRANTS", "warrants"),
    (" UNIT", "SPAC units"),
    ("UNITS,", "SPAC units"),
    (" RIGHT", "rights"),
    ("RIGHTS", "rights"),
    ("DEPOSITARY SHARE", "depositary shares"),
    ("PREFERRED", "preferred shares"),
    ("%", "preferred/debt instrument"),
)

FALLBACK_SUFFIX_PATTERNS = [
    (re.compile(r"^[A-Z]{4}W$"), "warrants"),
    (re.compile(r"^[A-Z]{4}U$"), "SPAC units"),
    (re.compile(r"^[A-Z]{4}R$"), "rights"),
]

LEVERAGED_KEYWORDS = ("2X", "3X", "ULTRA", "ULTRASHORT", "INVERSE",
                      "LEVERAGED", "DAILY BULL", "DAILY BEAR")


def is_blocked_symbol(symbol: str, name: str = "",
                      patterns: Iterable[tuple[Any, str]] | None = None
                      ) -> str | None:
    """Reason the symbol is excluded from dynamic discovery, or None.

    Screeners surface warrants, SPAC units, preferred shares and leveraged
    ETFs on volume spikes. None of those belong in a directional
    debit-spread strategy.

    Name evidence comes first because ticker shape is ambiguous: SNOW, LOW
    and NOW all end in W and are ordinary common stock.
    """
    sym = symbol.upper()
    upper_name = (name or "").upper()

    if upper_name:
        for kw, reason in INSTRUMENT_NAME_KEYWORDS:
            if kw in upper_name:
                return reason
        for kw in LEVERAGED_KEYWORDS:
            if kw in upper_name:
                return f"leveraged/inverse product ({kw})"
        # A name was available and cleared every check: trust it and stop.
        return None

    for pattern, reason in (patterns or FALLBACK_SUFFIX_PATTERNS):
        if pattern.match(sym):
            return f"{reason} (inferred from symbol shape; no name available)"
    return None