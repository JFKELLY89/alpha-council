"""
Alpha Council v2.4 §10.1 - SEC EDGAR intelligence (active-universe polling).

Polls the submissions JSON for Core and currently-active dynamic symbols,
turns priority filings into scored IntelligenceEvents, and persists them
through the same tables the news pipeline uses. The EVENT track treats a
fresh 8-K exactly like a fresh headline: evidence to reason about, never
an order.

Boundaries this module keeps:

  FAIR ACCESS. One shared client, a declared User-Agent with a real
  contact address (settings enforce it), and requests spaced to the
  configured ceiling (default 5/s). EDGAR blocks abusers; being blocked
  during the competition would cost the whole collector.

  BUDGETED BREADTH. One submissions request per symbol per sweep, at most
  `max_symbols_per_cycle` per sweep with a rotating cursor, and a
  per-symbol cooldown. 250 symbols at 90-second cadence would be abuse;
  a rotating 40-symbol window per scan covers the active set every few
  scans without it.

  DIRECTION COMES FROM THE TAPE. A filing's headline is a form code; no
  wording heuristic applies. Direction is resolved purely from the
  symbol's price response, through the same resolve_direction the news
  scorer uses, so a 13D the market sells is BEARISH evidence no matter
  how bullish the narrative around it.

  DEGRADATION IS LOUD AND HARMLESS. Any failure returns {} and logs
  collector degradation (§25). SEC being down never costs a scan.

The global current-filings feed (§10.1 "off-core discovery", a SHOULD) is
deliberately not built: config ships sec_global_injection_enabled: false
and the spec licenses skipping it when a robust feed is not verified.

Place at: alpha_council/intelligence/sec.py
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

import httpx

from alpha_council.db.engine import Database
from alpha_council.intelligence.news import (
    market_confirmation,
    novelty_score,
    resolve_direction,
)
from alpha_council.models.enums import Direction, SourceTier
from alpha_council.models.intelligence import IntelligenceEvent, IntelligenceItem
from alpha_council.utils.ids import content_hash, new_uuid
from alpha_council.utils.math import clip, freshness_score
from alpha_council.utils.time import iso_utc, utc_now

SOURCE_ID = "sec_edgar"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Priority forms (§10.1) -> (materiality band, freshness half-life minutes).
# 424B* is matched by prefix below.
#
# Bands calibrated against the FIRST live sweep (2026-09-01 22:22 ET, 518
# filings): banks file dozens of routine 424B2 structured-note supplements
# and insiders file Form 4s daily; both initially scored above the 55
# material-catalyst floor and would have polluted the EVENT track. Routine
# paper is context, not catalyst — bands now keep it under the floor while
# a results 8-K or a 13D still clears it decisively.
PRIORITY_FORMS: dict[str, tuple[tuple[int, int], int]] = {
    "8-K":   ((40, 95), 240),
    "10-Q":  ((50, 70), 240),
    "10-K":  ((50, 70), 240),
    "6-K":   ((45, 65), 240),
    "4":     ((15, 40), 240),
    "13D":   ((70, 90), 240),
    "13G":   ((40, 60), 240),
    "SC 13D": ((70, 90), 240),
    "SC 13G": ((40, 60), 240),
    "13D/A": ((50, 70), 240),
    "13G/A": ((30, 50), 240),
    "SC 13D/A": ((50, 70), 240),
    "SC 13G/A": ((30, 50), 240),
    "S-3":   ((30, 50), 240),
}
PREFIX_FORMS: tuple[tuple[str, tuple[tuple[int, int], int]], ...] = (
    ("424B", ((15, 35), 240)),
)

# 8-K item codes -> where inside the band the filing lands. Routine
# disclosure items (Reg FD, "other events") sit near the bottom; the
# items that move stocks sit at the top.
ITEM_WEIGHT: dict[str, float] = {
    "1.01": 0.85,  # material definitive agreement
    "1.02": 0.85,  # termination of agreement
    "1.03": 1.0,   # bankruptcy
    "2.01": 0.85,  # completed acquisition/disposition
    "2.02": 1.0,   # results of operations
    "2.05": 0.85,  # exit/disposal costs
    "2.06": 0.85,  # material impairments
    "3.01": 0.9,   # delisting notice
    "4.01": 0.75,  # accountant change
    "4.02": 1.0,   # non-reliance on prior financials
    "5.02": 0.7,   # officer/director changes
    "7.01": 0.15,  # Reg FD - usually a routine press release
    "8.01": 0.2,   # other events
}

# Banks file 424B supplements in bulk; past this many per symbol per sweep
# the rest are pure table noise and are skipped (logged in stats).
MAX_424B_PER_SYMBOL = 5


def classify_form(form: str) -> tuple[str, tuple[int, int], int] | None:
    """(normalized form, materiality band, half-life) or None if ignored."""
    f = (form or "").strip().upper()
    if f in PRIORITY_FORMS:
        band, hl = PRIORITY_FORMS[f]
        return f, band, hl
    for prefix, (band, hl) in PREFIX_FORMS:
        if f.startswith(prefix):
            return f, band, hl
    return None


def item_strength(items: str) -> float:
    """Strongest 8-K item weight present, 0.4 when none are recognized."""
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    weights = [ITEM_WEIGHT.get(c, 0.0) for c in codes]
    best = max(weights, default=0.0)
    return best if best > 0 else 0.4


@dataclass(slots=True)
class SECStats:
    symbols_polled: int = 0
    filings_seen: int = 0
    filings_priority: int = 0
    events: int = 0
    skipped_known: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "symbols_polled": self.symbols_polled,
            "filings_seen": self.filings_seen,
            "filings_priority": self.filings_priority,
            "events": self.events,
            "skipped_known": self.skipped_known,
            "errors": self.errors,
        }


class SECIntelligence:
    """Active-universe EDGAR polling. Fails open, always."""

    def __init__(self, db: Database, config: dict[str, Any],
                 user_agent: str,
                 max_symbols_per_cycle: int = 40,
                 cooldown_seconds: float = 600.0):
        self.db = db
        self.config = config
        self.user_agent = user_agent
        self.max_symbols_per_cycle = max_symbols_per_cycle
        self.cooldown_seconds = cooldown_seconds

        rps = float(config.get("rate_limits", {}).get(
            "sec_requests_per_second", 5))
        self._min_interval = 1.0 / max(rps, 0.5)
        self._last_request = 0.0
        self._req_lock = asyncio.Lock()

        self._client: httpx.AsyncClient | None = None
        self._cik_by_ticker: dict[str, int] = {}
        self._cursor = 0
        self._last_polled: dict[str, float] = {}
        self.stats = SECStats()
        self.degraded: str | None = None

    # ---- transport --------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"User-Agent": self.user_agent,
                         "Accept-Encoding": "gzip, deflate"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str) -> Any:
        """Rate-spaced GET. Raises on any non-200."""
        client = await self._ensure_client()
        async with self._req_lock:
            wait = self._min_interval - (_time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = _time.monotonic()
        r = await client.get(url)
        if r.status_code != 200:
            raise httpx.HTTPStatusError(
                f"HTTP {r.status_code} {url}", request=r.request, response=r)
        return r.json()

    # ---- CIK map ----------------------------------------------------

    async def load_cik_map(self, force: bool = False) -> int:
        """Ticker -> CIK, cached in system_state for 24 hours."""
        if self._cik_by_ticker and not force:
            return len(self._cik_by_ticker)

        cached = await self.db.get_state("sec_cik_map")
        if cached and not force:
            fetched = cached.get("fetched_at", "")
            if fetched and (utc_now() - datetime.fromisoformat(fetched)
                            ).total_seconds() < 86400:
                self._cik_by_ticker = {k: int(v) for k, v in
                                       cached.get("map", {}).items()}
                if self._cik_by_ticker:
                    return len(self._cik_by_ticker)

        try:
            payload = await self._get_json(TICKER_MAP_URL)
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            self.degraded = f"cik map fetch failed: {exc}"[:200]
            await self.db.log_event("WARN", "sec", "SEC_CIK_MAP_FAILED",
                                    self.degraded)
            return len(self._cik_by_ticker)

        mapping: dict[str, int] = {}
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            ticker = str(row.get("ticker", "")).upper().strip()
            cik = row.get("cik_str") or row.get("cik")
            if ticker and cik:
                mapping[ticker] = int(cik)
        if mapping:
            self._cik_by_ticker = mapping
            await self.db.set_state("sec_cik_map", {
                "fetched_at": iso_utc(), "map": mapping})
        return len(self._cik_by_ticker)

    # ---- collection -------------------------------------------------

    def _pick_symbols(self, symbols: Sequence[str]) -> list[str]:
        """Rotating window over the mapped active set, honoring cooldowns."""
        now = _time.monotonic()
        mapped = [s.upper() for s in symbols
                  if s.upper() in self._cik_by_ticker]
        eligible = [s for s in mapped
                    if now - self._last_polled.get(s, 0.0)
                    >= self.cooldown_seconds]
        if not eligible:
            return []
        start = self._cursor % len(eligible)
        window = (eligible[start:] + eligible[:start])[
            : self.max_symbols_per_cycle]
        self._cursor += len(window)
        return window

    async def collect(self, symbols: Sequence[str], lookback_hours: int = 24,
                      price_returns: dict[str, float] | None = None,
                      now: datetime | None = None
                      ) -> dict[str, list[IntelligenceEvent]]:
        """Poll EDGAR for the given symbols. Returns events keyed by symbol."""
        now = now or utc_now()
        price_returns = price_returns or {}
        self.stats = SECStats()
        self.degraded = None

        if not await self.load_cik_map():
            return {}

        await self._ensure_registry()
        cutoff = now - timedelta(hours=lookback_hours)
        events: dict[str, list[IntelligenceEvent]] = {}

        for symbol in self._pick_symbols(symbols):
            self._last_polled[symbol] = _time.monotonic()
            self.stats.symbols_polled += 1
            try:
                filings = await self._recent_filings(symbol, cutoff, now)
            except Exception as exc:  # noqa: BLE001 - one symbol never
                self.stats.errors += 1                # costs the sweep
                await self.db.log_event(
                    "WARN", "sec", "SEC_POLL_FAILED",
                    f"{symbol}: {exc}"[:200])
                continue

            for filing in filings:
                event = await self._to_event(symbol, filing,
                                             price_returns.get(symbol), now)
                if event is not None:
                    events.setdefault(symbol, []).append(event)
                    self.stats.events += 1

        if events:
            await self._persist_events(events)
        return events

    async def _recent_filings(self, symbol: str, cutoff: datetime,
                              now: datetime) -> list[dict[str, Any]]:
        """Priority filings accepted after the cutoff, newest first."""
        cik = self._cik_by_ticker[symbol]
        payload = await self._get_json(SUBMISSIONS_URL.format(cik=cik))
        recent = (payload.get("filings") or {}).get("recent") or {}

        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        accepted = recent.get("acceptanceDateTime") or []
        items_list = recent.get("items") or []
        docs = recent.get("primaryDocument") or []

        out: list[dict[str, Any]] = []
        b424_count = 0
        for i, form in enumerate(forms):
            self.stats.filings_seen += 1
            classified = classify_form(form)
            if classified is None:
                continue
            if classified[0].startswith("424B"):
                b424_count += 1
                if b424_count > MAX_424B_PER_SYMBOL:
                    continue
            ts_raw = accepted[i] if i < len(accepted) else None
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                from datetime import timezone

                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff or ts > now + timedelta(minutes=5):
                continue
            self.stats.filings_priority += 1
            out.append({
                "form": classified[0], "band": classified[1],
                "half_life": classified[2],
                "accession": accessions[i] if i < len(accessions) else "",
                "accepted_at": ts,
                "items": items_list[i] if i < len(items_list) else "",
                "document": docs[i] if i < len(docs) else "",
                "cik": cik,
            })
        return out

    async def _to_event(self, symbol: str, filing: dict[str, Any],
                        price_return: float | None,
                        now: datetime) -> IntelligenceEvent | None:
        """Score one filing. Returns None when it is already persisted."""
        accession = filing["accession"] or f"{symbol}_{filing['accepted_at']}"
        native_id = f"{SOURCE_ID}:{accession}"

        existing = await self.db.fetchone(
            "SELECT item_id FROM intelligence_items "
            "WHERE source_id=? AND source_native_id=?",
            (SOURCE_ID, native_id))
        if existing:
            self.stats.skipped_known += 1
            return None

        form = filing["form"]
        title = f"{symbol} filed {form}" + (
            f" (items {filing['items']})" if filing.get("items") else "")
        url = (f"https://www.sec.gov/Archives/edgar/data/{filing['cik']}/"
               f"{accession.replace('-', '')}/{filing['document']}"
               if filing.get("document") else None)

        item = IntelligenceItem(
            item_id=f"sec_{new_uuid()[:12]}",
            source_id=SOURCE_ID,
            source_native_id=native_id,
            source_tier=SourceTier.TIER_1_PRIMARY,
            retrieved_at=now,
            published_at=filing["accepted_at"],
            url=url,
            title=title,
            content_hash=content_hash(native_id),
            symbols=[symbol],
            raw={"form": form, "items": filing.get("items", "")},
        )
        await self._persist_item(item)

        low, high = filing["band"]
        strength = item_strength(filing.get("items", "")) if form == "8-K" \
            else 0.6
        materiality = low + (high - low) * strength

        age_minutes = (now - filing["accepted_at"]).total_seconds() / 60
        fresh = freshness_score(age_minutes, filing["half_life"])
        novelty = novelty_score(filing["accepted_at"], [item], now)
        direction, confidence, _ = resolve_direction(
            Direction.NEUTRAL, 0.0, price_return)
        confirmation = market_confirmation(direction, price_return)
        reliability = float(self.config.get(
            "source_base_reliability", {}).get("government", 100))

        weights = self.config.get("catalyst_weights", {
            "materiality": 0.30, "freshness": 0.20,
            "source_reliability": 0.20, "market_confirmation": 0.15,
            "surprise": 0.15})
        catalyst = clip(
            weights["materiality"] * materiality
            + weights["freshness"] * fresh
            + weights["source_reliability"] * reliability
            + weights["market_confirmation"] * confirmation
            + weights["surprise"] * 50.0)

        return IntelligenceEvent(
            event_id=f"evt_{new_uuid()[:12]}",
            item_id=item.item_id, symbol=symbol,
            event_type=f"sec_{form.lower().replace(' ', '_').replace('/', '_')}",
            direction=direction, direction_confidence=confidence,
            source_reliability_score=reliability,
            freshness_score=fresh, novelty_score=novelty,
            # A filing is self-corroborating: the primary source IS the
            # record. Same rule the news scorer applies to Tier-1 items.
            corroboration_score=100.0 if novelty >= 70 else 60.0,
            materiality_score=clip(materiality),
            surprise_score=50.0,
            market_confirmation_score=confirmation,
            catalyst_score=catalyst,
            provisional=age_minutes < 5,
            extracted_facts=[title],
            evidence_urls=[url] if url else [],
            created_at=now)

    # ---- persistence -------------------------------------------------

    async def _ensure_registry(self) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO source_registry(source_id, name, domain, "
            "source_type, tier, base_reliability, collector, enabled, "
            "config_json, created_at) VALUES(?,?,?,?,?,?,?,1,'{}',?)",
            (SOURCE_ID, "SEC EDGAR", "sec.gov", "regulatory_filings",
             "TIER_1_PRIMARY", 100.0, "sec_edgar", iso_utc()))

    async def _persist_item(self, item: IntelligenceItem) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO intelligence_items("
            "item_id, source_id, source_native_id, source_tier, "
            "retrieved_at, published_at, url, title, content_hash, "
            "ingest_status, raw_json) VALUES(?,?,?,?,?,?,?,?,?,'SCORED','{}')",
            (item.item_id, item.source_id, item.source_native_id,
             str(item.source_tier), iso_utc(item.retrieved_at),
             iso_utc(item.published_at) if item.published_at else None,
             item.url, item.title, item.content_hash))

    async def _persist_events(
            self, events: dict[str, list[IntelligenceEvent]]) -> None:
        import json

        rows = []
        for symbol_events in events.values():
            for e in symbol_events:
                rows.append((
                    e.event_id, e.item_id, e.symbol, e.event_type,
                    str(e.direction), e.direction_confidence,
                    e.source_reliability_score, e.freshness_score,
                    e.novelty_score, e.corroboration_score,
                    e.materiality_score, e.surprise_score,
                    e.market_confirmation_score, e.catalyst_score,
                    1 if e.provisional else 0,
                    json.dumps(e.extracted_facts),
                    json.dumps(e.evidence_urls),
                    iso_utc(e.created_at)))
        if rows:
            await self.db.executemany(
                "INSERT OR IGNORE INTO intelligence_events("
                "event_id, item_id, symbol, event_type, direction, "
                "direction_confidence, source_reliability_score, "
                "freshness_score, novelty_score, corroboration_score, "
                "materiality_score, surprise_score, "
                "market_confirmation_score, catalyst_score, provisional, "
                "extracted_facts_json, evidence_urls_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
